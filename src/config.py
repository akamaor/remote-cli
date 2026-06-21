import os
from dataclasses import dataclass
from typing import FrozenSet

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    allowed_user_ids: FrozenSet[int]
    log_dir: str
    command_timeout: int
    max_output_lines: int
    max_output_bytes: int


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
        command_timeout = int(os.environ.get("COMMAND_TIMEOUT", "10"))
        max_output_lines = int(os.environ.get("MAX_OUTPUT_LINES", "50"))
        max_output_bytes = int(os.environ.get("MAX_OUTPUT_BYTES", "3800"))
    except ValueError:
        raise ValueError("COMMAND_TIMEOUT, MAX_OUTPUT_LINES, MAX_OUTPUT_BYTES must be integers")

    if command_timeout < 1 or command_timeout > 300:
        raise ValueError("COMMAND_TIMEOUT must be between 1 and 300 seconds")

    return Config(
        telegram_bot_token=token,
        allowed_user_ids=allowed_ids,
        log_dir=log_dir,
        command_timeout=command_timeout,
        max_output_lines=max_output_lines,
        max_output_bytes=max_output_bytes,
    )
