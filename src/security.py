import shlex
from typing import FrozenSet

# Commands that require an interactive TTY or block indefinitely on stdin.
# This is a UX guard — the execution timeout is the hard backstop.
# PATH-stripped base name is checked (e.g. /usr/bin/vim → vim).
INTERACTIVE_COMMANDS: FrozenSet[str] = frozenset([
    # Shells
    "bash", "sh", "zsh", "fish", "dash", "ksh", "csh", "tcsh",
    # Editors
    "nano", "vim", "vi", "nvim", "emacs", "pico", "joe",
    # Pagers
    "less", "more", "man",
    # Process monitors
    "top", "htop", "iotop", "iftop", "atop", "glances", "nmon",
    # Continuous streaming (user likely wants -f, which blocks forever)
    "watch",
    # Remote access
    "ssh", "sftp", "ftp", "telnet", "nc", "netcat", "ncat",
    # Databases (interactive REPL)
    "mysql", "psql", "sqlite3", "mongosh", "mongo", "redis-cli",
    # Multiplexers
    "screen", "tmux",
    # Interpreters
    "python", "python3", "python2", "ipython", "irb", "pry",
    "node", "deno", "lua", "php", "perl",
    # Privilege escalation (handled by sudo allowlist — block direct calls)
    "su", "passwd",
    # Interactive system tools
    "gdisk", "fdisk", "parted", "cfdisk",
])


def is_authorized(user_id: int, allowed_ids: FrozenSet[int]) -> bool:
    return user_id in allowed_ids


def is_interactive_command(raw_command: str) -> tuple[bool, str]:
    """
    Returns (is_blocked, base_cmd_name).
    Parses only the first token of the command to extract the binary name.
    """
    raw_command = raw_command.strip()
    if not raw_command:
        return False, ""

    try:
        tokens = shlex.split(raw_command)
    except ValueError:
        return False, ""

    if not tokens:
        return False, ""

    # Strip full path to get the bare binary name
    base = tokens[0].split("/")[-1].lower()
    return base in INTERACTIVE_COMMANDS, base
