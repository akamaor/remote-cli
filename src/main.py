"""
Entry point.  Run with:
    python -m src.main
"""

import logging
import sys

from .config import load_config
from .logger_setup import setup_loggers
from .telegram_bot import build_bot


def main() -> None:
    try:
        config = load_config()
    except ValueError as exc:
        print(f"[FATAL] Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    app_logger, audit_logger = setup_loggers(config.log_dir)

    app_logger.info(
        "remote-cli starting | allowed_user_ids=%s | timeout=%ds",
        sorted(config.allowed_user_ids),
        config.command_timeout,
    )

    bot = build_bot(config, app_logger, audit_logger)

    app_logger.info("Long-polling started (outbound only — no inbound port required)")

    try:
        bot.infinity_polling(
            timeout=20,
            long_polling_timeout=15,
            logger_level=logging.WARNING,   # suppress pyTelegramBotAPI debug noise
            allowed_updates=["message"],
        )
    except KeyboardInterrupt:
        app_logger.info("Shutting down (KeyboardInterrupt)")
    except Exception as exc:
        app_logger.exception("Fatal error in polling loop: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
