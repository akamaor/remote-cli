import shlex
from typing import FrozenSet

# No commands are blocked — use /rc_shell for fully interactive sessions.
# The execution timeout is the only hard backstop for hanging processes.
INTERACTIVE_COMMANDS: FrozenSet[str] = frozenset()


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
