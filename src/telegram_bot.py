"""
Telegram long-polling integration.

Design notes:
- Outbound long-polling only — no inbound ports required.
- Unauthorized user IDs are silently dropped (no reply = no bot fingerprinting).
- Private DMs only — group chats rejected.
- Session tracks cwd and output format across messages.
- /format lets you switch display style on the fly.
- cd ~ uses HOME_DIR from .env so you land in your actual home directory.
"""

import html
import logging
import os
import platform
import time

import telebot
from telebot.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import Config
from .executor import execute, ExecutionResult
from .interactive import InteractiveShell
from .security import is_authorized, is_interactive_command

# ---------------------------------------------------------------------------
# Output formats
# ---------------------------------------------------------------------------
_FORMATS = {
    "minimal":  "Raw output only — cleanest for copy-paste",
    "standard": "📁 cwd header + output + exit code  (default)",
    "compact":  "📁 cwd and status on one header line, then output",
    "verbose":  "🖥 hostname + 📁 cwd + echoed command + output + timing",
    "styled":   "🟢/🔴 emoji status + bold text — modern look",
    "rich":     "━━ border header with hostname, cwd, and command",
}

# ---------------------------------------------------------------------------
# Shortcut commands — appear in the Telegram "/" command picker.
# "name": ("shell command", "picker description")
# shell=False is enforced — no pipes or redirections here.
# ---------------------------------------------------------------------------
_SHORTCUTS: dict = {
    # System
    "sysinfo":     ("uname -a",                                                         "OS and kernel version"),
    "hostname":    ("hostname -f",                                                      "Full hostname"),
    "cpu":         ("lscpu",                                                            "CPU architecture and details"),
    "uptime":      ("uptime",                                                           "Uptime and load average"),
    "whoami":      ("id",                                                               "Current user and groups"),
    "env":         ("env",                                                              "Environment variables"),
    # Filesystem
    "ls":          ("ls -la",                                                           "List files in current directory"),
    "df":          ("df -h",                                                            "Disk space summary"),
    "disk":        ("df -h --output=source,size,used,avail,pcent,target",               "Disk usage full table"),
    "du":          ("du -sh /home /var /tmp /opt /root",                                "Directory sizes"),
    "inodes":      ("df -i",                                                            "Inode usage per filesystem"),
    # Memory / CPU
    "free":        ("free -h",                                                          "RAM and swap usage"),
    "ps":          ("ps aux --sort=-%cpu",                                              "All processes sorted by CPU"),
    "top5cpu":     ("ps axo pid,user,%cpu,%mem,comm --sort=-%cpu",                      "Top processes by CPU"),
    "top5mem":     ("ps axo pid,user,%cpu,%mem,comm --sort=-%mem",                      "Top processes by memory"),
    "vmstat":      ("vmstat -s",                                                        "Virtual memory statistics"),
    # Network
    "ip":          ("ip -br addr",                                                      "Network interfaces and IPs"),
    "routes":      ("ip route",                                                         "Routing table"),
    "netstat":     ("ss -tulnp",                                                        "Listening ports and services"),
    "connections": ("ss -tp",                                                           "Active TCP connections"),
    "dns":         ("cat /etc/resolv.conf",                                             "DNS resolver config"),
    # Services
    "services":    ("sudo systemctl list-units --type=service --state=running --no-pager", "Running systemd services"),
    "failed":      ("sudo systemctl --failed --no-pager",                                "Failed systemd services"),
    "timers":      ("sudo systemctl list-timers --no-pager",                             "Scheduled systemd timers"),
    "rc_status":   ("sudo systemctl status remote-cli --no-pager",                      "Remote CLI service status"),
    # Logs
    "logs":        ("sudo journalctl -n 40 --no-pager",                                 "Last 40 journal entries"),
    "errors":      ("sudo journalctl -p err -n 20 --no-pager",                          "Last 20 error-level events"),
    "auth":        ("sudo journalctl -u sshd -n 20 --no-pager",                         "Last 20 SSH auth events"),
    "rc_logs":     ("sudo journalctl -u remote-cli -n 30 --no-pager",                   "Last 30 bot log entries"),
    # Users / Security
    "who":         ("who",                                                              "Currently logged-in users"),
    "last":        ("last -n 10",                                                       "Last 10 logins"),
    "users":       ("cut -d: -f1 /etc/passwd",                                         "All local user accounts"),
    "sudoers":     ("cat /etc/sudoers.d/chatcli",                                       "Current bot sudo allowlist"),
    # Packages
    "updates":     ("apt list --upgradable",                                            "Available package updates"),
    "installed":   ("dpkg -l",                                                          "All installed packages"),
}

_HELP_TEXT = """<b>Secure Remote CLI</b>

Type any shell command as a plain message to execute it.

<b>Navigation</b>
<code>cd /path</code>  — change directory (persists across messages)
<code>cd ..</code>     — up one level  |  <code>cd ~</code> — your home dir  |  <code>cd</code> — home dir

<b>Admin commands</b>
Prefix with <code>sudo</code> for elevated access
e.g. <code>sudo chmod 755 /etc/myfile</code>

<b>─── Remote CLI controls  (rc_ prefix) ───</b>
/rc_style    — change output style  (tap buttons to pick)
/rc_shell    — start interactive bash session (stdin enabled)
/rc_exit     — close interactive shell
/rc_ping     — latency + host check
/rc_pwd      — current working directory
/rc_status   — remote-cli service status
/rc_logs     — last 30 bot log entries
/rc_help     — this help message

<code>/rc_style minimal</code>   raw output only
<code>/rc_style standard</code>  cwd + output + exit code  (default)
<code>/rc_style compact</code>   single header, then output
<code>/rc_style verbose</code>   hostname + cwd + echoed command + timing
<code>/rc_style styled</code>    🟢/🔴 emoji status + bold text
<code>/rc_style rich</code>      ━━ border header with hostname and cwd

<b>─── System shortcuts ───</b>
<b>System</b>  /sysinfo /hostname /cpu /uptime /whoami /env
<b>Filesystem</b>  /ls /df /disk /du /inodes
<b>Memory &amp; CPU</b>  /free /ps /top5cpu /top5mem /vmstat
<b>Network</b>  /ip /routes /netstat /connections /dns
<b>Services</b>  /services /failed /timers
<b>Logs</b>  /logs /errors /auth
<b>Users</b>  /who /last /users /sudoers
<b>Packages</b>  /updates /installed
"""

# Commands registered with Telegram (appear in the / picker)
_BOT_COMMANDS = [
    # ── Remote CLI controls (rc_ prefix) ──
    BotCommand("rc_help",     "Help and usage guide"),
    BotCommand("rc_ping",     "Latency and host check"),
    BotCommand("rc_pwd",      "Show current working directory"),
    BotCommand("rc_style",    "Change output display style"),
    BotCommand("rc_shell",    "Start interactive bash session (stdin enabled)"),
    BotCommand("rc_exit",     "Close interactive shell session"),
    BotCommand("rc_status",   "Remote CLI service status"),
    BotCommand("rc_logs",     "Last 30 remote CLI log entries"),
    # ── System shortcuts ──
    BotCommand("sysinfo",     "OS and kernel version"),
    BotCommand("hostname",    "Full hostname"),
    BotCommand("cpu",         "CPU architecture and details"),
    BotCommand("uptime",      "Uptime and load average"),
    BotCommand("whoami",      "Current user and groups"),
    BotCommand("env",         "Environment variables"),
    # Filesystem
    BotCommand("ls",          "List files in current directory"),
    BotCommand("df",          "Disk space summary"),
    BotCommand("disk",        "Disk usage full table"),
    BotCommand("du",          "Directory sizes"),
    BotCommand("inodes",      "Inode usage per filesystem"),
    # Memory / CPU
    BotCommand("free",        "RAM and swap usage"),
    BotCommand("ps",          "All processes sorted by CPU"),
    BotCommand("top5cpu",     "Top processes by CPU"),
    BotCommand("top5mem",     "Top processes by memory"),
    BotCommand("vmstat",      "Virtual memory statistics"),
    # Network
    BotCommand("ip",          "Network interfaces and IPs"),
    BotCommand("routes",      "Routing table"),
    BotCommand("netstat",     "Listening ports and services"),
    BotCommand("connections", "Active TCP connections"),
    BotCommand("dns",         "DNS resolver config"),
    # Services
    BotCommand("services",    "Running systemd services"),
    BotCommand("failed",      "Failed systemd services"),
    BotCommand("timers",      "Scheduled systemd timers"),
    # Logs
    BotCommand("logs",        "Last 40 journal entries"),
    BotCommand("errors",      "Last 20 error-level events"),
    BotCommand("auth",        "Last 20 SSH auth events"),
    # Users / Security
    BotCommand("who",         "Currently logged-in users"),
    BotCommand("last",        "Last 10 logins"),
    BotCommand("users",       "All local user accounts"),
    BotCommand("sudoers",     "Current bot sudo allowlist"),
    # Packages
    BotCommand("updates",     "Available package updates"),
    BotCommand("installed",   "All installed packages"),
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
    # Single authorised user, no concurrency concern.
    _start_cwd = config.home_dir if os.access(config.home_dir, os.X_OK) else "/"
    session = {
        "cwd":     _start_cwd,            # start in home dir if accessible, else /
        "fmt":     config.output_format,  # changeable at runtime with /rc_style
        "shell":   None,                  # InteractiveShell instance, or None
        "history": [],                    # per-session command history (max 100)
    }

    # ---- /rc_ping ----
    @bot.message_handler(commands=["rc_ping"])
    def handle_ping(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return
        latency_ms = (time.time() - message.date) * 1000
        bot.reply_to(
            message,
            f"<b>Pong</b>  |  {latency_ms:.0f} ms  |  "
            f"<code>{html.escape(platform.node())}</code>  |  "
            f"📁 <code>{html.escape(session['cwd'])}</code>  |  "
            f"fmt: <code>{session['fmt']}</code>",
            parse_mode="HTML",
        )

    # ---- /rc_pwd ----
    @bot.message_handler(commands=["rc_pwd"])
    def handle_pwd(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return
        bot.reply_to(
            message,
            f"<code>📁 {html.escape(session['cwd'])}</code>",
            parse_mode="HTML",
        )

    # ---- /rc_style [name] ----
    @bot.message_handler(commands=["rc_style"])
    def handle_format(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return

        parts = (message.text or "").strip().split()

        # /format <name> — switch directly via text command
        if len(parts) > 1:
            chosen = parts[1].lower()
            if chosen not in _FORMATS:
                options = "  ".join(f"<code>{k}</code>" for k in _FORMATS)
                bot.reply_to(
                    message,
                    f"Unknown format <code>{html.escape(chosen)}</code>\nOptions: {options}",
                    parse_mode="HTML",
                )
                return
            session["fmt"] = chosen
            bot.reply_to(
                message,
                f"Format set to <code>{chosen}</code>  —  {_FORMATS[chosen]}\n\n"
                f"{_format_example(chosen)}",
                parse_mode="HTML",
            )
            return

        # /format — show picker with inline buttons
        bot.reply_to(
            message,
            _format_picker_text(session["fmt"]),
            parse_mode="HTML",
            reply_markup=_format_keyboard(session["fmt"]),
        )

    # ---- Inline keyboard callbacks (format picker buttons) ----
    @bot.callback_query_handler(func=lambda call: call.data.startswith("fmt:"))
    def handle_format_callback(call) -> None:
        user_id = call.from_user.id
        if not is_authorized(user_id, config.allowed_user_ids):
            bot.answer_callback_query(call.id, "Not authorised.")
            return

        chosen = call.data[4:]  # strip "fmt:" prefix
        if chosen not in _FORMATS:
            bot.answer_callback_query(call.id, "Unknown format.")
            return

        session["fmt"] = chosen
        app_logger.info("FORMAT_CHANGE | user_id=%d | fmt=%s", user_id, chosen)

        # Update the picker message in-place with the new selection highlighted
        try:
            bot.edit_message_text(
                _format_picker_text(chosen),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode="HTML",
                reply_markup=_format_keyboard(chosen),
            )
        except Exception:
            pass  # message unchanged — silently ignore (e.g. same format re-tapped)

        bot.answer_callback_query(call.id, f"✅ {chosen}")

    # ---- /rc_shell [exit|kill] ----
    @bot.message_handler(commands=["rc_shell"])
    def handle_shell(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return
        parts = (message.text or "").strip().split(None, 1)
        sub = parts[1].lower() if len(parts) > 1 else ""

        if sub in ("exit", "stop", "close", "quit"):
            _close_shell(session, bot, message)
            return

        if session["shell"] is not None and session["shell"].is_alive():
            bot.reply_to(
                message,
                "🖥 Shell session already active.\n"
                "Type commands normally — they go to bash.\n"
                "Send <code>exit</code> or /exit to end.",
                parse_mode="HTML",
            )
            return

        # Start a new shell
        try:
            session["shell"] = InteractiveShell(cwd=session["cwd"])
        except Exception as exc:
            bot.reply_to(message, f"❌ Could not start shell: {html.escape(str(exc))}", parse_mode="HTML")
            return

        app_logger.info("SHELL_START | user_id=%d | cwd=%r", message.from_user.id, session["cwd"])
        bot.reply_to(
            message,
            "🖥 <b>Interactive shell started</b>\n\n"
            "Type any command — stdin flows directly to <code>bash</code>.\n"
            "Responses to prompts (y/n, REPLs, etc.) work too.\n\n"
            f"📁 <code>{html.escape(session['cwd'])}</code>\n\n"
            "Send <code>exit</code> or /rc_exit to close the session.",
            parse_mode="HTML",
        )

    # ---- /rc_exit — close interactive shell ----
    @bot.message_handler(commands=["rc_exit"])
    def handle_exit(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return
        _close_shell(session, bot, message)

    # ---- /rc_help, /start ----
    @bot.message_handler(commands=["rc_help", "start"])
    def handle_help(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return
        bot.reply_to(message, _HELP_TEXT, parse_mode="HTML")

    # ---- Shortcut commands (/ls, /df, /ps, …) ----
    for _cmd_name, (_shell_cmd, _) in _SHORTCUTS.items():
        _handler = _make_shortcut_handler(
            bot, _shell_cmd, _cmd_name, session, config, app_logger, audit_logger
        )
        bot.message_handler(commands=[_cmd_name])(_handler)

    # ---- All other text → interactive shell or one-shot execution ----
    @bot.message_handler(func=lambda m: True, content_types=["text"])
    def handle_command(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return

        user_id = message.from_user.id
        raw = (message.text or "").strip()

        if not raw:
            bot.reply_to(message, "Send a shell command, or /help for usage.")
            return

        # ── Interactive shell mode ────────────────────────────────────
        sh = session.get("shell")
        if sh is not None:
            if not sh.is_alive():
                session["shell"] = None
                bot.reply_to(message, "⚠️ Shell session ended unexpectedly. Switched to normal mode.")
            else:
                # "exit" typed as plain text closes the session cleanly
                if raw.lower() in ("exit", "exit()", "quit", "quit()"):
                    _close_shell(session, bot, message)
                    return
                bot.send_chat_action(message.chat.id, "typing")
                app_logger.info("SHELL_INPUT | user_id=%d | input=%r", user_id, raw[:200])
                output = sh.send(raw)
                if not sh.is_alive():
                    session["shell"] = None
                    footer = "\n\n<i>Shell session ended.</i>"
                else:
                    footer = ""
                reply = _format_shell_output(output, session["fmt"], raw) + footer
                try:
                    bot.reply_to(message, reply, parse_mode="HTML")
                except Exception:
                    bot.reply_to(message, output or "(no output)")
                return

        # ── Normal one-shot mode ──────────────────────────────────────
        # Normalize first word to lowercase so "Ls", "CD", "History" all work
        _p = raw.split(None, 1)
        raw = _p[0].lower() + (" " + _p[1] if len(_p) > 1 else "")

        first_token = raw.split()[0].split("/")[-1].lower() if raw.split() else ""

        # Handle cd (shell builtin — cannot be exec'd as a subprocess)
        if first_token == "cd":
            new_cwd, err = _resolve_cd(raw, session["cwd"], config.home_dir)
            session["cwd"] = new_cwd
            if err:
                reply = f"<code>{html.escape(err)}</code>\n<code>📁 {html.escape(session['cwd'])}</code>"
            else:
                reply = f"<code>📁 {html.escape(session['cwd'])}</code>"
            bot.reply_to(message, reply, parse_mode="HTML")
            audit_logger.info("CD | user_id=%d | new_cwd=%r", user_id, session["cwd"])
            return

        # Handle history (shell builtin — show per-session command log)
        if first_token == "history":
            hist = session["history"]
            if not hist:
                bot.reply_to(message, "<i>No commands in session history yet.</i>", parse_mode="HTML")
            else:
                lines = "\n".join(f"{i+1:>4}  {html.escape(cmd)}" for i, cmd in enumerate(hist))
                bot.reply_to(message, f"<pre>{lines}</pre>", parse_mode="HTML")
            return

        # Detect shell operators — shell=False can't handle them; suggest bash -c
        if any(op in raw for op in ("|", ">>", "&&", "||", ";", "$(")):
            if not raw.strip().lower().startswith("bash ") and not raw.strip().lower().startswith("sh "):
                bot.reply_to(
                    message,
                    f"⚠️ Shell operators detected (<code>|</code> <code>&gt;&gt;</code> <code>&amp;&amp;</code> etc.)\n\n"
                    f"Wrap in <code>bash -c</code>:\n"
                    f"<code>bash -c \"{html.escape(raw)}\"</code>\n\n"
                    f"Or use /rc_shell for a full interactive session.",
                    parse_mode="HTML",
                )
                return

        # Track command in session history (keep last 100)
        session["history"].append(raw)
        if len(session["history"]) > 100:
            session["history"].pop(0)

        # Block interactive commands (vim, ssh, top, …)
        blocked, blocked_cmd = is_interactive_command(raw)
        if blocked:
            bot.reply_to(
                message,
                f"<b>[BLOCKED]</b> <code>{html.escape(blocked_cmd)}</code> requires an "
                "interactive terminal and cannot run remotely.\n"
                "Use /rc_shell for a full interactive session.",
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

        if result.error_msg and (
            "Working directory no longer exists" in result.error_msg
            or "Working directory not accessible" in result.error_msg
        ):
            session["cwd"] = "/"

        _audit(audit_logger, user_id, result)
        _send_reply(bot, message, result, config.command_timeout, session["cwd"], session["fmt"], raw, app_logger)

    # ---- Register command menu with Telegram ----
    try:
        bot.set_my_commands(_BOT_COMMANDS)
        app_logger.info("Bot command menu registered (%d commands)", len(_BOT_COMMANDS))
    except Exception as exc:
        app_logger.warning("Could not register bot command menu: %s", exc)

    return bot


# ---------------------------------------------------------------------------
# Shortcut handler factory — avoids closure-in-loop bugs
# ---------------------------------------------------------------------------

def _make_shortcut_handler(bot, shell_cmd, label, session, config, app_logger, audit_logger):
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
        _send_reply(bot, message, result, config.command_timeout, session["cwd"], session["fmt"], f"/{label}", app_logger)
    return handler


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _close_shell(session: dict, bot, message) -> None:
    sh = session.get("shell")
    if sh is None or not sh.is_alive():
        session["shell"] = None
        bot.reply_to(message, "ℹ️ No active shell session.", parse_mode="HTML")
        return
    sh.close()
    session["shell"] = None
    bot.reply_to(
        message,
        "✅ <b>Shell session closed.</b>  Back to normal mode.",
        parse_mode="HTML",
    )


def _resolve_cd(raw: str, current_cwd: str, home_dir: str) -> tuple:
    """
    Returns (new_cwd, error_msg).  error_msg is '' on success.
    ~ and bare cd both expand to home_dir (set via HOME_DIR in .env).
    """
    parts = raw.strip().split(None, 1)
    target = parts[1].strip() if len(parts) > 1 else "~"

    # Expand ~
    if target == "~" or target == "":
        target = home_dir
    elif target.startswith("~/"):
        target = os.path.join(home_dir, target[2:])

    # Resolve relative paths
    if not os.path.isabs(target):
        target = os.path.normpath(os.path.join(current_cwd, target))
    else:
        target = os.path.normpath(target)

    original = parts[1] if len(parts) > 1 else "~"

    if not os.path.exists(target):
        return current_cwd, f"cd: {original}: No such file or directory"
    if not os.path.isdir(target):
        return current_cwd, f"cd: {original}: Not a directory"

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
    out = result.output.replace("\n", "\\n") if result.output else ""
    if result.timed_out:
        audit_logger.warning(
            "TIMEOUT | user_id=%d | cmd=%r | elapsed=%.2fs | output=%r",
            user_id, result.command, result.elapsed_seconds, out,
        )
    elif result.error_msg:
        audit_logger.error(
            "EXEC_ERROR | user_id=%d | cmd=%r | exit=%s | elapsed=%.2fs | err=%s | output=%r",
            user_id, result.command, result.exit_code, result.elapsed_seconds, result.error_msg, out,
        )
    else:
        audit_logger.info(
            "EXECUTED | user_id=%d | cmd=%r | exit=%s | elapsed=%.2fs | output=%r",
            user_id, result.command, result.exit_code, result.elapsed_seconds, out,
        )


def _format_reply(result: ExecutionResult, timeout: int, cwd: str, fmt: str, cmd: str = "") -> str:
    """
    Four display formats, selectable per-session with /format:

    minimal  — raw output only
    standard — 📁 cwd + output block + exit/time       (default)
    compact  — 📁 cwd + status on one line, then output
    verbose  — 🖥 host  📁 cwd  $ cmd + output + exit/time
    """
    has_out = bool(result.output.strip())

    def _status_text() -> str:
        if result.timed_out:
            return f"TIMEOUT {timeout}s"
        if result.error_msg:
            return "ERR"
        return "OK" if result.exit_code == 0 else f"FAIL({result.exit_code})"

    def _time_text() -> str:
        return f"{result.elapsed_seconds:.2f}s"

    # ── minimal ──────────────────────────────────────────────
    if fmt == "minimal":
        if result.timed_out:
            return f"TIMEOUT after {timeout}s — process killed"
        if result.error_msg:
            return html.escape(result.error_msg)
        return f"<pre>{html.escape(result.output)}</pre>" if has_out else "<i>(no output)</i>"

    # ── compact ──────────────────────────────────────────────
    if fmt == "compact":
        status = _status_text()
        header = f"<code>📁 {html.escape(cwd)}  {html.escape(status)}  {_time_text()}</code>"
        if result.error_msg and not result.timed_out:
            return f"{header}\n<code>{html.escape(result.error_msg)}</code>"
        if has_out:
            return f"{header}\n<pre>{html.escape(result.output)}</pre>"
        return header

    # ── verbose ──────────────────────────────────────────────
    if fmt == "verbose":
        parts = [
            f"<code>🖥 {html.escape(platform.node())}  📁 {html.escape(cwd)}</code>",
        ]
        if cmd:
            parts.append(f"<code>$ {html.escape(cmd)}</code>")
        if result.timed_out:
            parts.append(f"<b>TIMEOUT</b> — exceeded {timeout}s, process killed (SIGKILL).")
        elif result.error_msg:
            parts.append(f"<b>ERROR:</b> {html.escape(result.error_msg)}")
        if has_out:
            parts.append(f"<pre>{html.escape(result.output)}</pre>")
        elif not result.timed_out and not result.error_msg:
            parts.append("<i>(no output)</i>")
        exit_code = result.exit_code if result.exit_code is not None else "—"
        parts.append(f"<code>exit {exit_code}  ·  {_time_text()}</code>")
        return "\n".join(parts)

    # ── styled ───────────────────────────────────────────────
    if fmt == "styled":
        if result.timed_out:
            cwd_icon = "💀"
            footer   = f"💀 <b>TIMEOUT {timeout}s</b>"
        elif result.error_msg:
            cwd_icon = "⚠️"
            footer   = f"⚠️ <b>ERR</b>  ·  <code>{_time_text()}</code>"
        elif result.exit_code == 0:
            cwd_icon = "✅"
            footer   = f"🟢 <b>OK</b>  ·  <code>{_time_text()}</code>"
        else:
            cwd_icon = "❌"
            footer   = f"🔴 <b>FAIL({result.exit_code})</b>  ·  <code>{_time_text()}</code>"

        parts = [f"{cwd_icon}  📁 <b>{html.escape(cwd)}</b>"]
        if result.timed_out:
            parts.append(f"<b>Killed after {timeout}s</b>")
        elif result.error_msg:
            parts.append(f"<b>{html.escape(result.error_msg)}</b>")
        if has_out:
            parts.append(f"<pre>{html.escape(result.output)}</pre>")
        elif not result.timed_out and not result.error_msg:
            parts.append("<i>(no output)</i>")
        parts.append(footer)
        return "\n\n".join(parts)

    # ── rich ─────────────────────────────────────────────────
    if fmt == "rich":
        SEP = "━━━━━━━━━━━━━━━━━━━━━━"
        host = html.escape(platform.node())
        parts = [
            SEP,
            f"🖥 <b>{host}</b>  ·  📁 <b>{html.escape(cwd)}</b>",
        ]
        if cmd:
            parts.append(f"▸ <code>{html.escape(cmd)}</code>")
        parts.append(SEP)
        if result.timed_out:
            parts.append(f"💀 <b>Killed after {timeout}s</b>")
        elif result.error_msg:
            parts.append(f"⚠️ <b>{html.escape(result.error_msg)}</b>")
        if has_out:
            parts.append(f"<pre>{html.escape(result.output)}</pre>")
        elif not result.timed_out and not result.error_msg:
            parts.append("<i>(no output)</i>")
        if result.timed_out:
            footer = "💀 <b>TIMEOUT</b>"
        elif result.error_msg:
            footer = f"⚠️ <b>ERR</b>  ·  ⏱ <code>{_time_text()}</code>"
        elif result.exit_code == 0:
            footer = f"✅ <b>OK</b>  ·  ⏱ <code>{_time_text()}</code>"
        else:
            footer = f"❌ <b>FAIL({result.exit_code})</b>  ·  ⏱ <code>{_time_text()}</code>"
        parts.append(footer)
        return "\n".join(parts)

    # ── standard (default) ───────────────────────────────────
    parts = [f"<code>📁 {html.escape(cwd)}</code>"]
    if result.timed_out:
        parts.append(f"<b>TIMEOUT</b> — exceeded {timeout}s, process killed (SIGKILL).")
    elif result.error_msg:
        parts.append(f"<b>ERROR:</b> {html.escape(result.error_msg)}")
    if has_out:
        parts.append(f"<pre>{html.escape(result.output)}</pre>")
    elif not result.timed_out and not result.error_msg:
        parts.append("<i>(no output)</i>")
    if result.exit_code is not None:
        status = "OK" if result.exit_code == 0 else f"FAIL ({result.exit_code})"
        parts.append(f"<code>{html.escape(status)}  {_time_text()}</code>")
    return "\n".join(parts)


def _format_shell_output(output: str, fmt: str, cmd: str = "") -> str:
    """Format interactive shell output using the session's current style."""
    has_out = bool(output and output.strip())

    if fmt == "minimal":
        return f"<pre>{html.escape(output)}</pre>" if has_out else "<i>(no output)</i>"

    if fmt == "compact":
        if has_out:
            return f"<code>🖥 shell</code>\n<pre>{html.escape(output)}</pre>"
        return "<code>🖥 shell  (no output)</code>"

    if fmt == "verbose":
        parts = [f"<code>🖥 {html.escape(platform.node())}</code>"]
        if cmd:
            parts.append(f"<code>$ {html.escape(cmd)}</code>")
        if has_out:
            parts.append(f"<pre>{html.escape(output)}</pre>")
        else:
            parts.append("<i>(no output)</i>")
        return "\n".join(parts)

    if fmt == "styled":
        parts = ["🖥  <b>shell</b>"]
        if has_out:
            parts.append(f"<pre>{html.escape(output)}</pre>")
        else:
            parts.append("<i>(no output)</i>")
        return "\n\n".join(parts)

    if fmt == "rich":
        SEP = "━━━━━━━━━━━━━━━━━━━━━━"
        parts = [SEP, f"🖥 <b>{html.escape(platform.node())}</b>  ·  <b>shell</b>"]
        if cmd:
            parts.append(f"▸ <code>{html.escape(cmd)}</code>")
        parts.append(SEP)
        if has_out:
            parts.append(f"<pre>{html.escape(output)}</pre>")
        else:
            parts.append("<i>(no output)</i>")
        return "\n".join(parts)

    # standard (default)
    if has_out:
        return f"<code>🖥 shell</code>\n<pre>{html.escape(output)}</pre>"
    return "<code>🖥 shell</code>\n<i>(no output)</i>"


def _format_picker_text(current: str) -> str:
    lines = [f"<b>Output format</b>  (current: <code>{current}</code>)\n"]
    for name, desc in _FORMATS.items():
        marker = "✓" if name == current else "·"
        lines.append(f"{marker} <b>{name}</b>  —  {desc}")
    lines.append("\nTap a button to switch:")
    return "\n".join(lines)


def _format_keyboard(current: str) -> InlineKeyboardMarkup:
    """Two-column inline keyboard with the active format marked."""
    names = list(_FORMATS.keys())
    rows = []
    for i in range(0, len(names), 2):
        row = []
        for name in names[i:i + 2]:
            label = f"✓ {name}" if name == current else name
            row.append(InlineKeyboardButton(label, callback_data=f"fmt:{name}"))
        rows.append(row)
    kb = InlineKeyboardMarkup()
    for row in rows:
        kb.add(*row)
    return kb


def _format_example(fmt: str) -> str:
    """Short preview shown after switching format."""
    examples = {
        "minimal":  "<pre>total 48\ndrwxr-xr-x  5 maor maor  4096 Jun 21</pre>",
        "standard": "<code>📁 /home/maor</code>\n<pre>total 48\n...</pre>\n<code>OK  0.02s</code>",
        "compact":  "<code>📁 /home/maor  OK  0.02s</code>\n<pre>total 48\n...</pre>",
        "verbose":  "<code>🖥 myhost  📁 /home/maor</code>\n<code>$ ls -la</code>\n<pre>total 48\n...</pre>\n<code>exit 0  ·  0.02s</code>",
        "styled":   "✅  📁 <b>/home/maor</b>\n\n<pre>total 48\n...</pre>\n\n🟢 <b>OK</b>  ·  <code>0.02s</code>",
        "rich":     "━━━━━━━━━━━━━━━━━━━━━━\n🖥 <b>myhost</b>  ·  📁 <b>/home/maor</b>\n▸ <code>ls -la</code>\n━━━━━━━━━━━━━━━━━━━━━━\n<pre>total 48\n...</pre>\n✅ <b>OK</b>  ·  ⏱ <code>0.02s</code>",
    }
    return f"<b>Preview:</b>\n{examples.get(fmt, '')}"


def _send_reply(bot, message, result, timeout, cwd, fmt, cmd, app_logger) -> None:
    reply = _format_reply(result, timeout, cwd, fmt, cmd)
    try:
        bot.reply_to(message, reply, parse_mode="HTML")
    except Exception as exc:
        app_logger.warning("HTML reply failed, sending plain text: %s", exc)
        bot.reply_to(message, _strip_html(reply))


def _strip_html(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text)
