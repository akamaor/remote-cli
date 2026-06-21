import os
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class ExecutionResult:
    command: str
    exit_code: Optional[int]
    output: str                  # Truncated stdout+stderr combined — safe to send to chat
    elapsed_seconds: float
    timed_out: bool = False
    error_msg: str = ""          # Structured error text (parse failure, not found, etc.)


def execute(
    command: str,
    timeout: int,
    max_output_lines: int,
    max_output_bytes: int,
    cwd: str = "/",
) -> ExecutionResult:
    t0 = time.monotonic()

    # ---- Safe command parsing (shell=False enforced) ----
    try:
        args = shlex.split(command)
    except ValueError as exc:
        return ExecutionResult(
            command=command,
            exit_code=None,
            output="",
            elapsed_seconds=time.monotonic() - t0,
            error_msg=f"Command parse error: {exc}",
        )

    if not args:
        return ExecutionResult(
            command=command,
            exit_code=None,
            output="",
            elapsed_seconds=time.monotonic() - t0,
            error_msg="Empty command",
        )

    # ---- Spawn process ----
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # merge stderr into stdout for single output stream
            stdin=subprocess.DEVNULL,   # explicitly cut stdin so no prompt can hang
            shell=False,                # no shell injection surface
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,                    # honour the session working directory
            preexec_fn=os.setsid,       # new process group → clean SIGKILL of entire tree
        )
    except FileNotFoundError:
        return ExecutionResult(
            command=command,
            exit_code=127,
            output="",
            elapsed_seconds=time.monotonic() - t0,
            error_msg=f"Command not found: {args[0]!r}",
        )
    except PermissionError:
        return ExecutionResult(
            command=command,
            exit_code=126,
            output="",
            elapsed_seconds=time.monotonic() - t0,
            error_msg=f"Permission denied: {args[0]!r}",
        )
    except NotADirectoryError:
        return ExecutionResult(
            command=command,
            exit_code=None,
            output="",
            elapsed_seconds=time.monotonic() - t0,
            error_msg=f"Working directory no longer exists: {cwd!r}",
        )
    except OSError as exc:
        return ExecutionResult(
            command=command,
            exit_code=None,
            output="",
            elapsed_seconds=time.monotonic() - t0,
            error_msg=f"OS error launching process: {exc}",
        )

    # ---- Collect output with hard timeout ----
    try:
        stdout, _ = proc.communicate(timeout=timeout)
        elapsed = time.monotonic() - t0
        return ExecutionResult(
            command=command,
            exit_code=proc.returncode,
            output=_truncate(stdout, max_output_lines, max_output_bytes),
            elapsed_seconds=elapsed,
        )

    except subprocess.TimeoutExpired:
        # SIGKILL the entire process group — no gentle SIGTERM, this is a hard failsafe
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        proc.wait()
        return ExecutionResult(
            command=command,
            exit_code=None,
            output="",
            elapsed_seconds=time.monotonic() - t0,
            timed_out=True,
            error_msg=f"Exceeded {timeout}s timeout — process group killed (SIGKILL).",
        )


def _truncate(text: str, max_lines: int, max_bytes: int) -> str:
    """Keep only the tail of the output to fit chat message limits."""
    lines = text.splitlines()
    total_lines = len(lines)

    if total_lines > max_lines:
        dropped = total_lines - max_lines
        lines = lines[-max_lines:]
        header = f"[... {dropped} earlier lines omitted ...]\n"
    else:
        header = ""

    result = header + "\n".join(lines)

    # Byte-level cap (Telegram hard limit is ~4096 bytes per message)
    encoded = result.encode("utf-8")
    if len(encoded) > max_bytes:
        result = "[... truncated to fit message limit ...]\n" + encoded[-max_bytes:].decode(
            "utf-8", errors="replace"
        )

    return result
