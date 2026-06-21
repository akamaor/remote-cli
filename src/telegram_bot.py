"""
Telegram long-polling integration.

Design notes:
- Outbound long-polling only — no inbound ports required.
- Unauthorized user IDs are silently dropped (no reply = no bot fingerprinting).
- Private DMs only — group chats rejected.
- Session state tracks cwd across messages so cd works naturally.
- Shortcut commands (/ls, /df, /ps …) are registered with the Bot API so they
  appear in Telegram's / command picker.
- All output is HTML-escaped before sending.
"""

import html
import logging
import os
import platform
import time

import telebot
from telebot.types import BotCommand, Message

from .config import Config
from .executor import execute, ExecutionResult
from .security import is_authorized, is_interactive_command

# ---------------------------------------------------------------------------
# Shortcut commands — appear in the Telegram "/" command picker.
# Format: "command_name": ("shell command to run", "description shown in picker")
# No pipes — shell=False is enforced. Use output truncation for long results.
# ---------------------------------------------------------------------------
_SHORTCUTS: dict = {
    "ls":       ("ls -la",                                                          "List files in current directory"),
    "df":       ("df -h",                                                           "Disk space usage"),
    "free":     ("free -h",                                                         "Memory usage"),
    "uptime":   ("uptime",                                                          "Uptime and load average"),
    "ps":       ("ps aux --sort=-%cpu",                                             "Processes sorted by CPU"),
    "ip":       ("ip -br addr",                                                     "Network interfaces and IPs"),
    "services": ("systemctl list-units --type=service --state=running --no-pager",  "Running systemd services"),
    "disk":     ("df -h --output=source,size,used,avail,pcent,target",              "Disk usage (detailed)"),
    "whoami":   ("id",                                                              "Current user and groups"),
    "netstat":  ("ss -tulnp",                                                       "Open ports and listeners"),
}

_HELP_TEXT = """<b>Secure Remote CLI</b>

Type any shell command as a plain message to execute it.

<b>Navigation</b>
<code>cd /path</code>  — change directory (persists across messages)
<code>cd ..</code>     — go up one level
<code>cd</code>        — back to /
<code>pwd</code>       — show current path

<b>Admin commands</b>
Prefix with <code>sudo</code> for elevated access
e.g. <code>sudo chmod 755 /var/www/html</code>

<b>Shortcut commands</b>
/ls · /df · /disk · /free · /uptime · /ps · /ip · /services · /netstat · /whoami

<b>Slash commands</b>
/ping    — latency and host info
/pwd     — current directory
/help    — this message
"""

# Commands registered with the Telegram Bot API (appear in the / picker)
_BOT_COMMANDS = [
    BotCommand("help",     "Help & usage guide"),
    BotCommand("ping",     "Latency and host check"),
    BotCommand("pwd",      "Show current directory"),
    BotCommand("ls",       "List files in current directory"),
    BotCommand("df",       "Disk space usage"),
    BotCommand("disk",     "Disk usage (detailed)"),
    BotCommand("free",     "Memory usage"),
    BotCommand("uptime",   "Uptime and load average"),
    BotCommand("ps",       "Processes sorted by CPU"),
    BotCommand("ip",       "Network interfaces and IPs"),
    BotCommand("services", "Running systemd services"),
    BotCommand("netstat",  "Open ports and listeners"),
    BotCommand("whoami",   "Current user and groups"),
]


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def build_bot(
    config: Config,
    app_logger: logging.Logger,
    audit_logger: logging.Logger,
) -> telebot.TeleBot:
    bot = telebot.TeleBot(config.telegram_bot_token, parse_mode=None)

    # Session state — persists for the lifetime of this process.
    # Single authorised user, so no concurrency concern.
    session = {"cwd": "/"}

    # ---- /ping ----
    @bot.message_handler(commands=["ping"])
    def handle_ping(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return
        latency_ms = (time.time() - message.date) * 1000
        bot.reply_to(
            message,
            f"<b>Pong</b>  |  {latency_ms:.0f} ms  |  "
            f"<code>{html.escape(platform.node())}</code>  |  "
            f"cwd: <code>{html.escape(session['cwd'])}</code>",
            parse_mode="HTML",
        )

    # ---- /pwd ----
    @bot.message_handler(commands=["pwd"])
    def handle_pwd(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return
        bot.reply_to(
            message,
            f"<code>📁 {html.escape(session['cwd'])}</code>",
            parse_mode="HTML",
        )

    # ---- /help, /start ----
    @bot.message_handler(commands=["help", "start"])
    def handle_help(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return
        bot.reply_to(message, _HELP_TEXT, parse_mode="HTML")

    # ---- Shortcut commands (/ls, /df, /ps, …) ----
    for _cmd_name, (_shell_cmd, _) in _SHORTCUTS.items():
        _handler = _make_shortcut_handler(
            bot, _shell_cmd, session, config, app_logger, audit_logger
        )
        bot.message_handler(commands=[_cmd_name])(_handler)

    # ---- All other text → shell execution ----
    @bot.message_handler(func=lambda m: True, content_types=["text"])
    def handle_command(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return

        user_id = message.from_user.id
        raw = (message.text or "").strip()

        if not raw:
            bot.reply_to(message, "Send a shell command, or /help for usage.")
            return

        # Handle cd (shell builtin — cannot be exec'd as a subprocess)
        first_token = raw.split()[0].split("/")[-1].lower() if raw.split() else ""
        if first_token == "cd":
            new_cwd, err = _resolve_cd(raw, session["cwd"])
            session["cwd"] = new_cwd
            reply = (
                f"<code>{html.escape(err)}\n📁 {html.escape(session['cwd'])}</code>"
                if err else
                f"<code>📁 {html.escape(session['cwd'])}</code>"
            )
            bot.reply_to(message, reply, parse_mode="HTML")
            audit_logger.info("CD | user_id=%d | new_cwd=%r", user_id, session["cwd"])
            return

        # Block interactive commands (vim, ssh, top, …)
        blocked, blocked_cmd = is_interactive_command(raw)
        if blocked:
            bot.reply_to(
                message,
                f"<b>[BLOCKED]</b> <code>{html.escape(blocked_cmd)}</code> requires an "
                "interactive terminal and cannot run remotely.",
                parse_mode="HTML",
            )
            audit_logger.info("BLOCKED_INTERACTIVE | user_id=%d | cmd=%r", user_id, raw[:200])
            return

        app_logger.info("EXECUTING | user_id=%d | cwd=%r | cmd=%r", user_id, session["cwd"], raw[:200])
        bot.send_chat_action(message.chat.id, "typing")

        result = execute(
            command=raw,
            timeout=config.command_timeout,
            max_output_lines=config.max_output_lines,
            max_output_bytes=config.max_output_bytes,
            cwd=session["cwd"],
        )

        if result.error_msg and "Working directory no longer exists" in result.error_msg:
            session["cwd"] = "/"

        _audit(audit_logger, user_id, result)
        _send_reply(bot, message, result, config.command_timeout, session["cwd"], app_logger)

    # ---- Register command menu with Telegram ----
    try:
        bot.set_my_commands(_BOT_COMMANDS)
        app_logger.info("Bot command menu registered (%d shortcuts)", len(_BOT_COMMANDS))
    except Exception as exc:
        app_logger.warning("Could not register bot command menu: %s", exc)

    return bot


# ---------------------------------------------------------------------------
# Shortcut handler factory — avoids closure-in-loop bugs
# ---------------------------------------------------------------------------

def _make_shortcut_handler(bot, shell_cmd, session, config, app_logger, audit_logger):
    def handler(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return
        user_id = message.from_user.id
        app_logger.info("SHORTCUT | user_id=%d | cmd=%r | cwd=%r", user_id, shell_cmd, session["cwd"])
        bot.send_chat_action(message.chat.id, "typing")
        result = execute(
            command=shell_cmd,
            timeout=config.command_timeout,
            max_output_lines=config.max_output_lines,
            max_output_bytes=config.max_output_bytes,
            cwd=session["cwd"],
        )
        _audit(audit_logger, user_id, result)
        _send_reply(bot, message, result, config.command_timeout, session["cwd"], app_logger)
    return handler


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_cd(raw: str, current_cwd: str) -> tuple:
    """Returns (new_cwd, error_msg). error_msg is '' on success."""
    parts = raw.strip().split(None, 1)
    target = parts[1].strip() if len(parts) > 1 else "/"

    if target in ("~", ""):
        target = "/"
    elif target.startswith("~/"):
        target = "/" + target[2:]

    if not os.path.isabs(target):
        target = os.path.normpath(os.path.join(current_cwd, target))
    else:
        target = os.path.normpath(target)

    original = parts[1] if len(parts) > 1 else "~"

    if not os.path.exists(target):
        return current_cwd, f"cd: {original}: No such file or directory"
    if not os.path.isdir(target):
        return current_cwd, f"cd: {original}: Not a directory"
    if not os.access(target, os.X_OK):
        return current_cwd, f"cd: {original}: Permission denied"

    return target, ""


def _check_access(message: Message, config: Config, audit_logger: logging.Logger) -> bool:
    user_id  = message.from_user.id
    chat_id  = message.chat.id
    username = message.from_user.username or "no_username"

    if message.chat.type != "private":
        audit_logger.warning(
            "REJECTED_GROUPCHAT | user_id=%d | chat_id=%d | chat_type=%s | username=%s",
            user_id, chat_id, message.chat.type, username,
        )
        return False

    if not is_authorized(user_id, config.allowed_user_ids):
        audit_logger.warning(
            "REJECTED_UNAUTHORIZED | user_id=%d | chat_id=%d | username=%s | text=%r",
            user_id, chat_id, username, (message.text or "")[:200],
        )
        return False

    return True


def _audit(audit_logger: logging.Logger, user_id: int, result: ExecutionResult) -> None:
    """Audit trail — stdout content intentionally excluded."""
    if result.timed_out:
        audit_logger.warning(
            "TIMEOUT | user_id=%d | cmd=%r | elapsed=%.2fs",
            user_id, result.command, result.elapsed_seconds,
        )
    elif result.error_msg:
        audit_logger.error(
            "EXEC_ERROR | user_id=%d | cmd=%r | exit=%s | elapsed=%.2fs | err=%s",
            user_id, result.command, result.exit_code, result.elapsed_seconds, result.error_msg,
        )
    else:
        audit_logger.info(
            "EXECUTED | user_id=%d | cmd=%r | exit=%s | elapsed=%.2fs",
            user_id, result.command, result.exit_code, result.elapsed_seconds,
        )


def _format_reply(result: ExecutionResult, timeout: int, cwd: str = "/") -> str:
    parts = [f"<code>📁 {html.escape(cwd)}</code>"]

    if result.timed_out:
        parts.append(f"<b>TIMEOUT</b> — exceeded {timeout}s, process killed (SIGKILL).")
    elif result.error_msg:
        parts.append(f"<b>ERROR:</b> {html.escape(result.error_msg)}")

    if result.output.strip():
        parts.append(f"<pre>{html.escape(result.output)}</pre>")
    elif not result.timed_out and not result.error_msg:
        parts.append("<i>(no output)</i>")

    if result.exit_code is not None:
        status = "OK" if result.exit_code == 0 else f"FAIL ({result.exit_code})"
        parts.append(f"<code>{html.escape(status)}  {result.elapsed_seconds:.2f}s</code>")

    return "\n".join(parts)


def _send_reply(bot, message, result, timeout, cwd, app_logger) -> None:
    reply = _format_reply(result, timeout, cwd)
    try:
        bot.reply_to(message, reply, parse_mode="HTML")
    except Exception as exc:
        app_logger.warning("HTML reply failed, sending plain text: %s", exc)
        bot.reply_to(message, _strip_html(reply))


def _strip_html(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text)
