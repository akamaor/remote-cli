"""
PTY-backed interactive shell session.

Gives a persistent bash process where Telegram messages become stdin.
Works for Python/node REPLs, apt confirm prompts, menu-driven scripts, etc.

ANSI escape codes are stripped before output is sent to Telegram.
"""

import os
import pty
import re
import select
import signal
import subprocess
import time

# Strip ANSI CSI/OSC sequences and bare carriage returns
_ANSI = re.compile(
    r'\x1b(?:'
    r'\[[0-9;?]*[A-Za-z]'   # CSI: colors, cursor, erase
    r'|\][^\x07\x1b]*(?:\x07|\x1b\\)'  # OSC: window title, etc.
    r'|[()][AB012]'          # G0/G1 charset designators
    r'|[=>M78]'              # simple two-char sequences
    r')'
    r'|\r(?!\n)'             # bare CR not followed by LF
)


class InteractiveShell:
    """Persistent bash session over a PTY."""

    QUIET_THRESHOLD = 0.5   # seconds of silence → command done
    MAX_TIMEOUT     = 10.0  # absolute ceiling per send()

    def __init__(self, cwd: str = "/"):
        self.cwd = cwd
        master_fd, slave_fd = pty.openpty()
        self._fd = master_fd
        self.proc = subprocess.Popen(
            ["/bin/bash", "--norc", "--noprofile"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            preexec_fn=os.setsid,
            close_fds=True,
        )
        os.close(slave_fd)
        # Drain startup noise, then suppress PS1/PS2 so prompts don't appear
        self._drain(timeout=0.3)
        os.write(self._fd, b"export PS1=''\nexport PS2=''\n")
        self._drain(timeout=0.5)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send(self, line: str) -> str:
        """Write one line to bash stdin and return cleaned output."""
        if not self.is_alive():
            return "(session has ended)"
        try:
            os.write(self._fd, (line + "\n").encode("utf-8", errors="replace"))
        except OSError:
            return "(session has ended)"
        raw = self._read_until_quiet(timeout=self.MAX_TIMEOUT)
        return _ANSI.sub("", raw).strip()

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def exit_code(self):
        return self.proc.poll()

    def close(self) -> None:
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

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _drain(self, timeout: float = 0.3) -> None:
        self._read_until_quiet(timeout=timeout)

    def _read_until_quiet(self, timeout: float = 4.0) -> str:
        chunks = []
        deadline  = time.monotonic() + timeout
        last_data = time.monotonic()

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            wait = min(0.1, remaining)
            try:
                r, _, _ = select.select([self._fd], [], [], wait)
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
                if time.monotonic() - last_data >= self.QUIET_THRESHOLD:
                    break

        return b"".join(chunks).decode("utf-8", errors="replace")
