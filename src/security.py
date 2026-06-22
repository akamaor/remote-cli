import re
import shlex
from typing import FrozenSet, Optional

INTERACTIVE_COMMANDS: FrozenSet[str] = frozenset()

# (compiled_pattern, human-readable reason)
_DANGEROUS: list = [
    (re.compile(r'\brm\s+(?:-\S+\s+)*-[^\s]*r', re.I),  "recursive delete"),
    (re.compile(r'\bmkfs\b',                      re.I),  "filesystem format"),
    (re.compile(r'\bdd\b.*\bof=/dev/[sh]d',       re.I),  "raw disk write"),
    (re.compile(r'\bdd\b.*\bof=/dev/nvme',         re.I),  "raw NVMe write"),
    (re.compile(r'\bshutdown\b',                   re.I),  "system shutdown"),
    (re.compile(r'\breboot\b',                     re.I),  "system reboot"),
    (re.compile(r'\bhalt\b',                       re.I),  "system halt"),
    (re.compile(r'\bpoweroff\b',                   re.I),  "system power-off"),
    (re.compile(r'\bfdisk\b',                      re.I),  "partition editor"),
    (re.compile(r'\bparted\b',                     re.I),  "partition editor"),
    (re.compile(r'\bwipefs\b',                     re.I),  "wipe filesystem signatures"),
    (re.compile(r'>\s*/dev/[sh]d'),                         "write to block device"),
    (re.compile(r'>\s*/dev/nvme'),                          "write to NVMe device"),
    (re.compile(r'\bdpkg\b.*--purge',              re.I),  "purge installed package"),
    (re.compile(r'\bapt(?:-get)?\b.*--purge',      re.I),  "purge installed package"),
]


def is_authorized(user_id: int, allowed_ids: FrozenSet[int]) -> bool:
    return user_id in allowed_ids


def is_interactive_command(raw_command: str) -> tuple:
    """Returns (is_blocked, base_cmd_name)."""
    raw_command = raw_command.strip()
    if not raw_command:
        return False, ""
    try:
        tokens = shlex.split(raw_command)
    except ValueError:
        return False, ""
    if not tokens:
        return False, ""
    base = tokens[0].split("/")[-1].lower()
    return base in INTERACTIVE_COMMANDS, base


def get_dangerous_reason(command: str) -> Optional[str]:
    """Return a human-readable reason if the command is dangerous, else None."""
    for pattern, reason in _DANGEROUS:
        if pattern.search(command):
            return reason
    return None
