import os
from dataclasses import dataclass
from typing import FrozenSet

from dotenv import load_dotenv

load_dotenv()

_VALID_FORMATS = {"minimal", "standard", "verbose", "compact", "styled", "rich"}


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    allowed_user_ids: FrozenSet[int]
    log_dir: str
    command_timeout: int
    max_output_lines: int
    max_output_bytes: int
    output_format: str   # minimal | standard | compact | verbose | styled | rich
    home_dir: str        # what ~ expands to in cd commands


def load_config() -> Config:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set")

    raw_ids = os.environ.get("ALLOWED_TELEGRAM_USER_IDS", "").strip()
    if not raw_ids:
        raise ValueError("ALLOWED_TELEGRAM_USER_IDS is not set")

    try:
        allowed_ids: FrozenSet[int] = frozenset(
            int(uid.strip()) for uid in raw_ids.split(",") if uid.strip()
        )
    except ValueError:
        raise ValueError("ALLOWED_TELEGRAM_USER_IDS must be comma-separated integers")

    if not allowed_ids:
        raise ValueError("ALLOWED_TELEGRAM_USER_IDS must contain at least one ID")

    log_dir = os.environ.get("LOG_DIR", "/var/log/remote-cli").strip()

    try:
        command_timeout  = int(os.environ.get("COMMAND_TIMEOUT",  "10"))
        max_output_lines = int(os.environ.get("MAX_OUTPUT_LINES", "50"))
        max_output_bytes = int(os.environ.get("MAX_OUTPUT_BYTES", "3800"))
    except ValueError:
        raise ValueError("COMMAND_TIMEOUT, MAX_OUTPUT_LINES, MAX_OUTPUT_BYTES must be integers")

    if command_timeout < 1 or command_timeout > 3600:
        raise ValueError("COMMAND_TIMEOUT must be between 1 and 3600 seconds")

    output_format = os.environ.get("OUTPUT_FORMAT", "standard").strip().lower()
    if output_format not in _VALID_FORMATS:
        raise ValueError(f"OUTPUT_FORMAT must be one of: {', '.join(sorted(_VALID_FORMATS))}")

    home_dir = os.environ.get("HOME_DIR", "/root").strip() or "/root"

    return Config(
        telegram_bot_token=token,
        allowed_user_ids=allowed_ids,
        log_dir=log_dir,
        command_timeout=command_timeout,
        max_output_lines=max_output_lines,
        max_output_bytes=max_output_bytes,
        output_format=output_format,
        home_dir=home_dir,
    )
