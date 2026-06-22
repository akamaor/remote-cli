"""
Persistent PTY-backed bash shell.

Every Telegram message is written directly to bash stdin via a PTY.
Shell state (cwd, env vars, aliases, venv, conda) persists across messages.
All shell syntax works: |, &&, ||, ;, >, >>, $(...), &

ANSI escape codes are stripped before output is returned to Telegram.
"""

import os
import pty
import re
import select
import signal
import subprocess
import threading
import time
from typing import Optional

_ANSI = re.compile(
    r'\x1b(?:'
    r'\[[0-9;?]*[A-Za-z]'
    r'|\][^\x07\x1b]*(?:\x07|\x1b\\)'
    r'|[()][AB012]'
    r'|[=>M78]'
    r')'
    r'|\r(?!\n)'
)

_QUIET_THRESHOLD = 0.5   # seconds of output silence → command done
_POLL_INTERVAL   = 0.05  # PTY read poll interval
_DRAIN_QUIET     = 0.3   # quiet threshold used during drain


class PtyShell:
    """Persistent bash session backed by a PTY."""

    def __init__(self, shell: str = "/bin/bash", cwd: str = "/", extra_env: Optional[dict] = None,
                 shell_cmd: Optional[list] = None):
        self._shell = shell
        self._started_at = time.monotonic()
        self._last_cmd_at: Optional[float] = None
        self._history: list = []
        self._interrupt = threading.Event()
        self._send_lock = threading.Lock()

        env = {**os.environ}
        if extra_env:
            env.update(extra_env)

        master_fd, slave_fd = pty.openpty()
        self._fd = master_fd

        self.proc = subprocess.Popen(
            shell_cmd if shell_cmd else [shell],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            preexec_fn=os.setsid,
            close_fds=True,
            env=env,
        )
        os.close(slave_fd)

        # Wait for .bashrc to complete (conda/nvm can take 0.3–0.8s).
        # Use a 1s quiet threshold so we don't exit before bash finishes.
        self._raw_drain(6.0, quiet=1.0)
        # Disable PTY local echo and suppress prompts.
        os.write(self._fd, b"stty -echo; export PS1=''; export PS2=''; export PS4=''\n")
        # Drain the echo of the init line and the empty prompt that follows.
        self._raw_drain(1.5, quiet=0.3)

    # ── Public API ────────────────────────────────────────────────────

    def send_line(self, line: str, timeout: int = 120) -> str:
        """
        Write a line to bash and return the cleaned output.
        Blocks until output goes quiet, timeout is reached, or Ctrl+C fires.
        Thread-safe: serialises concurrent callers via an internal lock.
        """
        if not self.is_alive():
            return "(shell has ended)"

        with self._send_lock:
            self._interrupt.clear()
            # Quick drain of stale output left by a previous interrupt
            self._raw_drain(0.05)

            self._history.append(line)
            if len(self._history) > 100:
                self._history.pop(0)
            self._last_cmd_at = time.monotonic()

            try:
                os.write(self._fd, (line.rstrip('\n') + '\n').encode('utf-8', errors='replace'))
            except OSError:
                return "(session has ended)"

            raw, interrupted = self._collect_output(max_wait=float(timeout))
            cleaned = _ANSI.sub('', raw)

            # Strip command echo: if PTY local echo is still on the first
            # output line will be the verbatim command we typed.  Strip it.
            cmd_echo = line.strip()
            lines = cleaned.splitlines()
            if lines and lines[0].strip() == cmd_echo:
                lines = lines[1:]
            cleaned = '\n'.join(lines).strip()

            if interrupted:
                return (cleaned + "\n^C") if cleaned else "^C"
            return cleaned

    def send_ctrl_c(self) -> None:
        """
        Send SIGINT to the foreground process and interrupt any pending
        send_line().  Never blocks — safe to call from any thread.
        """
        self._interrupt.set()
        try:
            os.write(self._fd, b'\x03')
        except OSError:
            pass

    def send_ctrl_d(self) -> None:
        """Send EOF (Ctrl+D) to the shell."""
        try:
            os.write(self._fd, b'\x04')
        except OSError:
            pass

    def get_cwd(self) -> str:
        """
        Read the shell's actual CWD from /proc.
        Tracks every cd command automatically — no special-casing needed.
        """
        try:
            return os.readlink(f'/proc/{self.proc.pid}/cwd')
        except OSError:
            return 'unknown'

    def status(self) -> dict:
        return {
            'alive': self.is_alive(),
            'pid': self.proc.pid,
            'shell': self._shell,
            'cwd': self.get_cwd(),
            'uptime_s': time.monotonic() - self._started_at,
            'last_cmd_at': self._last_cmd_at,
        }

    def get_history(self) -> list:
        return list(self._history)

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def close(self) -> None:
        self._interrupt.set()
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        try:
            self.proc.wait(timeout=2)
        except Exception:
            pass

    # ── Internals ─────────────────────────────────────────────────────

    def _raw_drain(self, max_timeout: float, quiet: float = _DRAIN_QUIET) -> None:
        """Read and discard output until quiet for `quiet` seconds or max_timeout expires."""
        deadline = time.monotonic() + max_timeout
        last_data = time.monotonic()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                r, _, _ = select.select([self._fd], [], [], min(0.1, remaining))
            except (ValueError, OSError):
                break
            if r:
                try:
                    data = os.read(self._fd, 4096)
                    if data:
                        last_data = time.monotonic()
                    else:
                        break
                except OSError:
                    break
            else:
                if time.monotonic() - last_data >= quiet:
                    break

    def _collect_output(self, max_wait: float = 120.0) -> tuple:
        """
        Read until output is quiet for QUIET_THRESHOLD, or interrupted, or
        max_wait expires.  Returns (raw_str, was_interrupted).
        """
        chunks: list = []
        deadline  = time.monotonic() + max_wait
        last_data = time.monotonic()
        interrupted = False

        while True:
            if self._interrupt.is_set():
                interrupted = True
                # Extra drain to capture the ^C echo from the terminal
                time.sleep(0.2)
                while True:
                    try:
                        r, _, _ = select.select([self._fd], [], [], 0.1)
                    except (ValueError, OSError):
                        break
                    if r:
                        try:
                            data = os.read(self._fd, 4096)
                            if data:
                                chunks.append(data)
                            else:
                                break
                        except OSError:
                            break
                    else:
                        break
                break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            try:
                r, _, _ = select.select([self._fd], [], [], min(_POLL_INTERVAL, remaining))
            except (ValueError, OSError):
                break

            if r:
                try:
                    data = os.read(self._fd, 4096)
                except OSError:
                    break
                if data:
                    chunks.append(data)
                    last_data = time.monotonic()
                else:
                    break
            else:
                if time.monotonic() - last_data >= _QUIET_THRESHOLD:
                    break

        return b''.join(chunks).decode('utf-8', errors='replace'), interrupted
