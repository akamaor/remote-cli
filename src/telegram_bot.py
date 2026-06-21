"""
Telegram long-polling integration.

Design notes:
- Uses outbound long-polling exclusively — no inbound webhook ports required.
  This is compatible with a WireGuard-locked host with zero open inbound ports.
- All messages from non-allowlisted user IDs are silently dropped and audit-logged.
- Only private chats are accepted; group/supergroup/channel messages are rejected.
- Command output is sent as HTML <pre> blocks with proper escaping so arbitrary
  shell output cannot break the message formatting or inject Telegram markup.
"""

import html
import logging
import platform
import time

import telebot
from telebot.types import Message

from .config import Config
from .executor import execute, ExecutionResult
from .security import is_authorized, is_interactive_command

_HELP_TEXT = """<b>Secure Remote CLI</b>

Send any shell command as a plain message.

<b>Limits</b>
• Timeout: configurable (default 10 s)
• Output: last 50 lines / ~3.8 KB
• Interactive commands (vim, ssh, top…) are blocked

<b>Commands</b>
/ping  — latency check
/help  — this message

<b>Tips</b>
• Prefix admin commands with <code>sudo</code> (allowlisted via visudo)
• Use <code>2&gt;&amp;1</code> to capture stderr: <code>cmd arg 2&gt;&amp;1</code>
• Pipe through <code>tail -n 20</code> for long output
"""


def build_bot(
    config: Config,
    app_logger: logging.Logger,
    audit_logger: logging.Logger,
) -> telebot.TeleBot:
    bot = telebot.TeleBot(config.telegram_bot_token, parse_mode=None)

    # ---- /ping ----
    @bot.message_handler(commands=["ping"])
    def handle_ping(message: Message) -> None:
        user_id = message.from_user.id
        if not _check_access(message, config, audit_logger):
            return
        latency_ms = (time.time() - message.date) * 1000
        bot.reply_to(
            message,
            f"<b>Pong</b>  |  API latency: {latency_ms:.0f} ms  |  "
            f"Host: <code>{html.escape(platform.node())}</code>",
            parse_mode="HTML",
        )

    # ---- /help, /start ----
    @bot.message_handler(commands=["help", "start"])
    def handle_help(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return
        bot.reply_to(message, _HELP_TEXT, parse_mode="HTML")

    # ---- All other text messages → shell execution ----
    @bot.message_handler(func=lambda m: True, content_types=["text"])
    def handle_command(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return

        user_id = message.from_user.id
        raw = (message.text or "").strip()

        if not raw:
            bot.reply_to(message, "Send a shell command to execute, or /help for usage.")
            return

        # -- Block interactive commands --
        blocked, base_cmd = is_interactive_command(raw)
        if blocked:
            reply = (
                f"<b>[BLOCKED]</b> <code>{html.escape(base_cmd)}</code> requires an "
                "interactive terminal and cannot run remotely."
            )
            bot.reply_to(message, reply, parse_mode="HTML")
            audit_logger.info(
                "BLOCKED_INTERACTIVE | user_id=%d | cmd=%r",
                user_id,
                raw[:200],
            )
            return

        app_logger.info("EXECUTING | user_id=%d | cmd=%r", user_id, raw[:200])
        bot.send_chat_action(message.chat.id, "typing")

        result = execute(
            command=raw,
            timeout=config.command_timeout,
            max_output_lines=config.max_output_lines,
            max_output_bytes=config.max_output_bytes,
        )

        _audit(audit_logger, user_id, result)

        reply = _format_reply(result, config.command_timeout)
        try:
            bot.reply_to(message, reply, parse_mode="HTML")
        except Exception as exc:
            app_logger.warning("Failed to send HTML reply, falling back to plain text: %s", exc)
            bot.reply_to(message, _strip_html(reply))

    return bot


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_access(
    message: Message,
    config: Config,
    audit_logger: logging.Logger,
) -> bool:
    """
    Returns True if the message is authorized, False if it should be dropped.
    Unauthorized messages are audit-logged and silently ignored — no reply is sent.
    Replying would confirm the bot's existence to probing attackers.
    """
    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    username = message.from_user.username or "no_username"

    # Only accept private DMs — reject group/supergroup/channel
    if chat_type != "private":
        audit_logger.warning(
            "REJECTED_GROUPCHAT | user_id=%d | chat_id=%d | chat_type=%s | username=%s",
            user_id,
            chat_id,
            chat_type,
            username,
        )
        return False

    if not is_authorized(user_id, config.allowed_user_ids):
        audit_logger.warning(
            "REJECTED_UNAUTHORIZED | user_id=%d | chat_id=%d | username=%s | text=%r",
            user_id,
            chat_id,
            username,
            (message.text or "")[:200],
        )
        return False

    return True


def _audit(
    audit_logger: logging.Logger,
    user_id: int,
    result: ExecutionResult,
) -> None:
    """Log command outcomes to the audit trail — stdout content is intentionally excluded."""
    if result.timed_out:
        audit_logger.warning(
            "TIMEOUT | user_id=%d | cmd=%r | elapsed=%.2fs",
            user_id,
            result.command,
            result.elapsed_seconds,
        )
    elif result.error_msg:
        audit_logger.error(
            "EXEC_ERROR | user_id=%d | cmd=%r | exit=%s | elapsed=%.2fs | err=%s",
            user_id,
            result.command,
            result.exit_code,
            result.elapsed_seconds,
            result.error_msg,
        )
    else:
        audit_logger.info(
            "EXECUTED | user_id=%d | cmd=%r | exit=%s | elapsed=%.2fs",
            user_id,
            result.command,
            result.exit_code,
            result.elapsed_seconds,
        )


def _format_reply(result: ExecutionResult, timeout: int) -> str:
    parts = []

    if result.timed_out:
        parts.append(
            f"<b>TIMEOUT</b> — command exceeded {timeout}s and was forcefully killed (SIGKILL)."
        )
    elif result.error_msg:
        parts.append(f"<b>ERROR:</b> {html.escape(result.error_msg)}")

    if result.output.strip():
        # html.escape prevents any shell output from injecting Telegram HTML tags
        parts.append(f"<pre>{html.escape(result.output)}</pre>")
    elif not result.timed_out and not result.error_msg:
        parts.append("<i>(no output)</i>")

    if result.exit_code is not None:
        status = "OK" if result.exit_code == 0 else f"FAIL ({result.exit_code})"
        parts.append(
            f"Exit: <code>{html.escape(status)}</code>  |  "
            f"Time: <code>{result.elapsed_seconds:.2f}s</code>"
        )

    return "\n".join(parts)


def _strip_html(text: str) -> str:
    """Naive HTML tag stripper for plain-text fallback."""
    import re
    return re.sub(r"<[^>]+>", "", text)
