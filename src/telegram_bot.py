"""
Telegram long-polling integration — Persistent PTY shell mode.

Every plain-text message is fed directly into a persistent bash shell via a
PTY.  Shell state (cwd, env vars, aliases, venv/conda activation, history)
persists across messages exactly like a real terminal.

All shell syntax works natively: |  &&  ||  ;  >  >>  $()  &

Special bot commands
────────────────────
/rc_ctrl_c   — send Ctrl+C (interrupt running command)
/rc_ctrl_d   — send Ctrl+D (EOF to shell)
/rc_restart  — kill + restart the persistent shell
/rc_exit     — close shell (auto-restarts on next command)
/rc_status   — shell PID, CWD, user, uptime
/rc_history  — last 20 commands
/rc_pwd      — current working directory
/rc_exec     — one-shot isolated command (no shell state, old behaviour)
/rc_style    — change output display style
/rc_ping     — latency + host check

Dangerous command confirmation
───────────────────────────────
If REQUIRE_CONFIRM_FOR_DANGEROUS=true (default), commands matching patterns
like `rm -rf`, `mkfs`, `shutdown`, etc. require inline-keyboard confirmation
before execution.

Security
────────
- Unauthorized user IDs are silently dropped.
- Group chats are rejected.
- The bot token is never echoed in logs or replies.
- Every command is logged with timestamp, user ID, CWD, and result summary.
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
from .pty_shell import PtyShell
from .security import is_authorized, get_dangerous_reason

# ---------------------------------------------------------------------------
# Output formats
# ---------------------------------------------------------------------------
_FORMATS = {
    "terminal": "user@host:cwd$ — classic shell prompt style  (default)",
    "minimal":  "Raw output only — cleanest for copy-paste",
    "standard": "📁 cwd header + output + exit code",
    "compact":  "📁 cwd and status on one header line, then output",
    "verbose":  "🖥 hostname + 📁 cwd + echoed command + output + timing",
    "styled":   "🟢/🔴 emoji status + bold text — modern look",
    "rich":     "━━ border header with hostname, cwd, and command",
}

# ---------------------------------------------------------------------------
# System shortcuts — appear in the / picker.
# Each runs inside the persistent PTY shell, so cwd and env are respected.
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

_HELP_TEXT = """<b>Secure Remote CLI — Persistent Shell Mode</b>

Every message runs in a persistent bash shell. Shell state persists:
cwd, env vars, aliases, venv/conda activations, functions, history.

All shell syntax works natively:
<code>|</code>  <code>&amp;&amp;</code>  <code>||</code>  <code>;</code>  <code>&gt;</code>  <code>&gt;&gt;</code>  <code>$(...)</code>  <code>&amp;</code>

<b>─── Shell controls ───</b>
/rc_ctrl_c   — interrupt running command  (Ctrl+C)
/rc_ctrl_d   — send EOF to shell  (Ctrl+D)
/rc_restart  — kill + restart the shell  (fresh session)
/rc_exit     — close shell  (auto-restarts on next command)
/rc_status   — shell PID, CWD, user, uptime
/rc_history  — last 20 commands
/rc_pwd      — current working directory
/rc_exec     — one-shot isolated command  (no shell state)
/rc_style    — change output style
/rc_ping     — latency + host check

<b>─── Examples ───</b>
<code>export FOO=bar</code>   → <code>echo $FOO</code>  → prints bar
<code>cd /tmp</code>          → <code>pwd</code>         → /tmp
<code>source venv/bin/activate</code>  → <code>which python</code>  → venv python
<code>sleep 100</code>        → /rc_ctrl_c       → interrupts it

<b>─── System shortcuts ───</b>
<b>System</b>  /sysinfo /hostname /cpu /uptime /whoami /env
<b>Filesystem</b>  /ls /df /disk /du /inodes
<b>Memory &amp; CPU</b>  /free /ps /top5cpu /top5mem /vmstat
<b>Network</b>  /ip /routes /netstat /connections /dns
<b>Services</b>  /services /failed /timers
<b>Logs</b>  /logs /errors /auth /rc_logs
<b>Users</b>  /who /last /users /sudoers
<b>Packages</b>  /updates /installed
"""

_BOT_COMMANDS = [
    BotCommand("rc_help",     "Help and usage guide"),
    BotCommand("rc_ping",     "Latency and host check"),
    BotCommand("rc_pwd",      "Show current working directory"),
    BotCommand("rc_style",    "Change output display style"),
    BotCommand("rc_status",   "Shell status: PID, CWD, uptime"),
    BotCommand("rc_history",  "Show recent command history"),
    BotCommand("rc_ctrl_c",   "Send Ctrl+C — interrupt running command"),
    BotCommand("rc_ctrl_d",   "Send Ctrl+D — EOF to shell"),
    BotCommand("rc_restart",  "Restart the persistent shell"),
    BotCommand("rc_exit",     "Close shell (auto-restarts on next command)"),
    BotCommand("rc_exec",     "One-shot isolated command (no shell state)"),
    BotCommand("rc_logs",     "Last 30 remote CLI log entries"),
    # System shortcuts
    BotCommand("sysinfo",     "OS and kernel version"),
    BotCommand("hostname",    "Full hostname"),
    BotCommand("cpu",         "CPU architecture and details"),
    BotCommand("uptime",      "Uptime and load average"),
    BotCommand("whoami",      "Current user and groups"),
    BotCommand("env",         "Environment variables"),
    BotCommand("ls",          "List files in current directory"),
    BotCommand("df",          "Disk space summary"),
    BotCommand("disk",        "Disk usage full table"),
    BotCommand("du",          "Directory sizes"),
    BotCommand("inodes",      "Inode usage per filesystem"),
    BotCommand("free",        "RAM and swap usage"),
    BotCommand("ps",          "All processes sorted by CPU"),
    BotCommand("top5cpu",     "Top processes by CPU"),
    BotCommand("top5mem",     "Top processes by memory"),
    BotCommand("vmstat",      "Virtual memory statistics"),
    BotCommand("ip",          "Network interfaces and IPs"),
    BotCommand("routes",      "Routing table"),
    BotCommand("netstat",     "Listening ports and services"),
    BotCommand("connections", "Active TCP connections"),
    BotCommand("dns",         "DNS resolver config"),
    BotCommand("services",    "Running systemd services"),
    BotCommand("failed",      "Failed systemd services"),
    BotCommand("timers",      "Scheduled systemd timers"),
    BotCommand("logs",        "Last 40 journal entries"),
    BotCommand("errors",      "Last 20 error-level events"),
    BotCommand("auth",        "Last 20 SSH auth events"),
    BotCommand("who",         "Currently logged-in users"),
    BotCommand("last",        "Last 10 logins"),
    BotCommand("users",       "All local user accounts"),
    BotCommand("sudoers",     "Current bot sudo allowlist"),
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
    # More worker threads so /rc_ctrl_c can always get a thread even when
    # another thread is blocked waiting for PTY output.
    bot = telebot.TeleBot(config.telegram_bot_token, parse_mode=None, num_threads=6)

    _user = os.path.basename(config.home_dir.rstrip("/")) or "root"
    session: dict = {
        "shell":             None,   # PtyShell instance
        "fmt":               config.output_format,
        "user":              _user,
        "pending_dangerous": None,   # command string awaiting confirm
    }

    # Auto-start shell immediately
    try:
        session["shell"] = _start_shell(session, config)
        app_logger.info(
            "PTY shell started | pid=%d | cwd=%s | shell=%s",
            session["shell"].proc.pid,
            session["shell"].get_cwd(),
            config.default_shell,
        )
    except Exception as exc:
        app_logger.warning("Could not auto-start PTY shell: %s", exc)

    # ── /rc_ping ──────────────────────────────────────────────────────
    @bot.message_handler(commands=["rc_ping"])
    def handle_ping(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return
        latency_ms = (time.time() - message.date) * 1000
        sh = session.get("shell")
        cwd = sh.get_cwd() if sh and sh.is_alive() else "—"
        bot.reply_to(
            message,
            f"<b>Pong</b>  |  {latency_ms:.0f} ms  |  "
            f"<code>{html.escape(platform.node())}</code>  |  "
            f"📁 <code>{html.escape(cwd)}</code>  |  "
            f"fmt: <code>{session['fmt']}</code>",
            parse_mode="HTML",
        )

    # ── /rc_pwd ───────────────────────────────────────────────────────
    @bot.message_handler(commands=["rc_pwd"])
    def handle_pwd(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return
        sh = session.get("shell")
        cwd = sh.get_cwd() if sh and sh.is_alive() else "(shell not running)"
        bot.reply_to(message, f"<code>📁 {html.escape(cwd)}</code>", parse_mode="HTML")

    # ── /rc_status ────────────────────────────────────────────────────
    @bot.message_handler(commands=["rc_status"])
    def handle_rc_status(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return
        sh = session.get("shell")
        if sh is None or not sh.is_alive():
            bot.reply_to(
                message,
                "⛔ <b>Shell is not running.</b>\nSend any command to auto-start it.",
                parse_mode="HTML",
            )
            return
        st = sh.status()
        uptime = int(st["uptime_s"])
        h, m, s = uptime // 3600, (uptime % 3600) // 60, uptime % 60
        uptime_str = (f"{h}h {m}m {s}s" if h else f"{m}m {s}s" if m else f"{s}s")
        last_cmd = "never"
        if st["last_cmd_at"]:
            ago = int(time.monotonic() - st["last_cmd_at"])
            last_cmd = f"{ago}s ago"
        try:
            import pwd as _pwd
            run_user = _pwd.getpwuid(os.getuid()).pw_name
        except Exception:
            run_user = os.environ.get("USER", "unknown")
        bot.reply_to(
            message,
            f"<b>Shell Status</b>\n\n"
            f"🟢 Running\n"
            f"PID:          <code>{st['pid']}</code>\n"
            f"Shell:        <code>{html.escape(st['shell'])}</code>\n"
            f"CWD:          <code>{html.escape(st['cwd'])}</code>\n"
            f"User:         <code>{html.escape(run_user)}</code>\n"
            f"Uptime:       <code>{uptime_str}</code>\n"
            f"Last command: <code>{last_cmd}</code>",
            parse_mode="HTML",
        )

    # ── /rc_history ───────────────────────────────────────────────────
    @bot.message_handler(commands=["rc_history"])
    def handle_rc_history(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return
        sh = session.get("shell")
        hist = sh.get_history() if sh else []
        if not hist:
            bot.reply_to(message, "<i>No commands in history yet.</i>", parse_mode="HTML")
            return
        tail = hist[-20:]
        lines = "\n".join(f"{i+1:>4}  {html.escape(cmd)}" for i, cmd in enumerate(tail))
        bot.reply_to(
            message,
            f"<b>History (last {len(tail)}):</b>\n<pre>{lines}</pre>",
            parse_mode="HTML",
        )

    # ── /rc_ctrl_c ────────────────────────────────────────────────────
    @bot.message_handler(commands=["rc_ctrl_c"])
    def handle_ctrl_c(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return
        sh = session.get("shell")
        if sh is None or not sh.is_alive():
            bot.reply_to(message, "⛔ No active shell.", parse_mode="HTML")
            return
        sh.send_ctrl_c()
        app_logger.info("CTRL_C | user_id=%d", message.from_user.id)
        bot.reply_to(message, "✅ Ctrl+C sent.", parse_mode="HTML")

    # ── /rc_ctrl_d ────────────────────────────────────────────────────
    @bot.message_handler(commands=["rc_ctrl_d"])
    def handle_ctrl_d(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return
        sh = session.get("shell")
        if sh is None or not sh.is_alive():
            bot.reply_to(message, "⛔ No active shell.", parse_mode="HTML")
            return
        sh.send_ctrl_d()
        app_logger.info("CTRL_D | user_id=%d", message.from_user.id)
        bot.reply_to(message, "✅ Ctrl+D sent.", parse_mode="HTML")

    # ── /rc_restart ───────────────────────────────────────────────────
    @bot.message_handler(commands=["rc_restart"])
    def handle_rc_restart(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return
        sh = session.get("shell")
        if sh:
            sh.close()
        session["shell"] = None
        session["pending_dangerous"] = None
        try:
            new_sh = _start_shell(session, config)
            app_logger.info(
                "SHELL_RESTART | user_id=%d | pid=%d",
                message.from_user.id, new_sh.proc.pid,
            )
            bot.reply_to(
                message,
                f"🔄 <b>Shell restarted</b>  |  PID: <code>{new_sh.proc.pid}</code>  |  "
                f"📁 <code>{html.escape(new_sh.get_cwd())}</code>",
                parse_mode="HTML",
            )
        except Exception as exc:
            bot.reply_to(
                message,
                f"❌ Could not restart shell: {html.escape(str(exc))}",
                parse_mode="HTML",
            )

    # ── /rc_exit ──────────────────────────────────────────────────────
    @bot.message_handler(commands=["rc_exit"])
    def handle_rc_exit(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return
        sh = session.get("shell")
        if sh is None or not sh.is_alive():
            session["shell"] = None
            bot.reply_to(message, "ℹ️ No active shell.", parse_mode="HTML")
            return
        sh.close()
        session["shell"] = None
        session["pending_dangerous"] = None
        app_logger.info("SHELL_EXIT | user_id=%d", message.from_user.id)
        bot.reply_to(
            message,
            "✅ <b>Shell closed.</b>  Send any command to start a new one.",
            parse_mode="HTML",
        )

    # ── /rc_exec — one-shot isolated command (legacy/sandbox mode) ────
    @bot.message_handler(commands=["rc_exec"])
    def handle_rc_exec(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return
        parts = (message.text or "").strip().split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            bot.reply_to(
                message,
                "Usage: <code>/rc_exec &lt;command&gt;</code>\n"
                "Runs a single isolated command (no persistent shell state).",
                parse_mode="HTML",
            )
            return
        cmd = parts[1].strip()
        sh = session.get("shell")
        cwd = sh.get_cwd() if sh and sh.is_alive() else config.home_dir
        user_id = message.from_user.id
        app_logger.info("EXEC_ONESHOT | user_id=%d | cwd=%r | cmd=%r", user_id, cwd, cmd[:200])
        bot.send_chat_action(message.chat.id, "typing")
        result = execute(
            command=cmd,
            timeout=config.command_timeout,
            max_output_lines=config.max_output_lines,
            max_output_bytes=config.max_output_bytes,
            cwd=cwd,
        )
        _audit(audit_logger, user_id, result)
        _send_reply(bot, message, result, config.command_timeout, cwd, session["fmt"], cmd, app_logger, session["user"])

    # ── /rc_style [name] ──────────────────────────────────────────────
    @bot.message_handler(commands=["rc_style"])
    def handle_format(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return
        parts = (message.text or "").strip().split()
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
        bot.reply_to(
            message,
            _format_picker_text(session["fmt"]),
            parse_mode="HTML",
            reply_markup=_format_keyboard(session["fmt"]),
        )

    # ── /rc_help, /start ──────────────────────────────────────────────
    @bot.message_handler(commands=["rc_help", "start"])
    def handle_help(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return
        bot.reply_to(message, _HELP_TEXT, parse_mode="HTML")

    # ── Inline keyboard callbacks ─────────────────────────────────────

    @bot.callback_query_handler(func=lambda call: call.data.startswith("fmt:"))
    def handle_format_callback(call) -> None:
        user_id = call.from_user.id
        if not is_authorized(user_id, config.allowed_user_ids):
            bot.answer_callback_query(call.id, "Not authorised.")
            return
        chosen = call.data[4:]
        if chosen not in _FORMATS:
            bot.answer_callback_query(call.id, "Unknown format.")
            return
        session["fmt"] = chosen
        app_logger.info("FORMAT_CHANGE | user_id=%d | fmt=%s", user_id, chosen)
        try:
            bot.edit_message_text(
                _format_picker_text(chosen),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode="HTML",
                reply_markup=_format_keyboard(chosen),
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id, f"✅ {chosen}")

    @bot.callback_query_handler(func=lambda call: call.data.startswith("danger:"))
    def handle_danger_callback(call) -> None:
        user_id = call.from_user.id
        if not is_authorized(user_id, config.allowed_user_ids):
            bot.answer_callback_query(call.id, "Not authorised.")
            return

        if call.data == "danger:confirm":
            cmd = session.pop("pending_dangerous", None)
            if cmd is None:
                bot.answer_callback_query(call.id, "No pending command.")
                return
            bot.answer_callback_query(call.id, "Running…")
            try:
                bot.edit_message_reply_markup(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=None,
                )
            except Exception:
                pass
            sh = _ensure_shell(session, config, app_logger)
            if sh is None:
                bot.send_message(call.message.chat.id, "❌ Shell not available.", parse_mode="HTML")
                return
            audit_logger.warning("DANGER_CONFIRMED | user_id=%d | cmd=%r", user_id, cmd[:200])
            app_logger.info("DANGER_CONFIRMED | user_id=%d | cmd=%r", user_id, cmd[:200])
            output = sh.send_line(cmd, timeout=config.command_timeout)
            cwd = sh.get_cwd()
            reply = _format_shell_output(output, session["fmt"], cmd, cwd, session["user"])
            if not sh.is_alive():
                reply += "\n\n<i>Shell session ended.</i>"
                session["shell"] = None
            try:
                bot.send_message(call.message.chat.id, reply, parse_mode="HTML")
            except Exception:
                bot.send_message(call.message.chat.id, output or "(no output)")

        elif call.data == "danger:cancel":
            session["pending_dangerous"] = None
            bot.answer_callback_query(call.id, "Cancelled.")
            try:
                bot.edit_message_text(
                    "❌ <b>Command cancelled.</b>",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    parse_mode="HTML",
                )
            except Exception:
                pass

    # ── Shortcut commands (/ls, /df, /ps, …) ─────────────────────────
    for _cmd_name, (_shell_cmd, _) in _SHORTCUTS.items():
        _handler = _make_shortcut_handler(
            bot, _shell_cmd, _cmd_name, session, config, app_logger, audit_logger
        )
        bot.message_handler(commands=[_cmd_name])(_handler)

    # ── All other text → persistent PTY shell ────────────────────────
    @bot.message_handler(func=lambda m: True, content_types=["text"])
    def handle_command(message: Message) -> None:
        if not _check_access(message, config, audit_logger):
            return

        user_id = message.from_user.id
        raw = (message.text or "").strip()
        if not raw:
            return

        # Ensure the shell is running; auto-restart if it died
        sh = _ensure_shell(session, config, app_logger)
        if sh is None:
            bot.reply_to(
                message,
                "❌ Could not start shell. Check logs and try /rc_restart.",
                parse_mode="HTML",
            )
            return

        # Dangerous command confirmation
        if config.require_confirm_dangerous:
            reason = get_dangerous_reason(raw)
            if reason:
                session["pending_dangerous"] = raw
                markup = InlineKeyboardMarkup()
                markup.add(
                    InlineKeyboardButton("✅ Yes, run it", callback_data="danger:confirm"),
                    InlineKeyboardButton("❌ Cancel",      callback_data="danger:cancel"),
                )
                bot.reply_to(
                    message,
                    f"⚠️ <b>Dangerous command detected</b>\n\n"
                    f"<code>{html.escape(raw)}</code>\n"
                    f"Reason: <i>{html.escape(reason)}</i>\n\n"
                    "Are you sure you want to run this?",
                    parse_mode="HTML",
                    reply_markup=markup,
                )
                return

        _run_pty(bot, message, raw, sh, session, config, app_logger, audit_logger)

    # ── Register Telegram command menu ────────────────────────────────
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
        sh = _ensure_shell(session, config, app_logger)
        if sh is None:
            bot.reply_to(message, "❌ Shell not available.", parse_mode="HTML")
            return
        user_id = message.from_user.id
        app_logger.info("SHORTCUT | user_id=%d | cmd=%r", user_id, shell_cmd)
        bot.send_chat_action(message.chat.id, "typing")
        output = sh.send_line(shell_cmd, timeout=config.command_timeout)
        cwd = sh.get_cwd()
        if not sh.is_alive():
            session["shell"] = None
        reply = _format_shell_output(output, session["fmt"], f"/{label}", cwd, session["user"])
        try:
            bot.reply_to(message, reply, parse_mode="HTML")
        except Exception:
            bot.reply_to(message, output or "(no output)")
    return handler


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _start_shell(session: dict, config: Config) -> PtyShell:
    """Start a fresh PtyShell, store it in session, and return it."""
    cwd = config.home_dir if os.access(config.home_dir, os.X_OK) else "/"
    base_path = os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    extra_path = os.environ.get("EXTRA_PATH", "")
    full_path = (extra_path + ":" + base_path) if extra_path else base_path
    sh = PtyShell(
        shell=config.default_shell,
        cwd=cwd,
        extra_env={"PATH": full_path},
    )
    session["shell"] = sh
    return sh


def _ensure_shell(session: dict, config: Config, app_logger: logging.Logger) -> "PtyShell | None":
    """Return the live shell, auto-restarting it if it died."""
    sh = session.get("shell")
    if sh is not None and sh.is_alive():
        return sh
    # Shell is dead or was never started
    if sh is not None:
        app_logger.warning("Shell died unexpectedly (exit %s) — restarting", sh.proc.returncode)
        try:
            sh.close()
        except Exception:
            pass
    session["shell"] = None
    try:
        new_sh = _start_shell(session, config)
        app_logger.info("Shell auto-restarted | pid=%d", new_sh.proc.pid)
        return new_sh
    except Exception as exc:
        app_logger.error("Could not restart shell: %s", exc)
        return None


def _run_pty(bot, message, cmd: str, sh: PtyShell, session: dict, config: Config,
             app_logger: logging.Logger, audit_logger: logging.Logger) -> None:
    """Send a command to the PTY and reply with the output."""
    user_id = message.from_user.id
    app_logger.info("PTY_SEND | user_id=%d | cmd=%r", user_id, cmd[:200])
    bot.send_chat_action(message.chat.id, "typing")

    output = sh.send_line(cmd, timeout=config.command_timeout)

    cwd = sh.get_cwd()
    if not sh.is_alive():
        session["shell"] = None

    # Apply output size limits (PTY can produce a lot)
    output = _truncate(output, config.max_output_lines, config.max_output_bytes)

    audit_logger.info(
        "PTY_RESULT | user_id=%d | cmd=%r | cwd=%s | output_len=%d",
        user_id, cmd[:200], cwd, len(output),
    )

    reply = _format_shell_output(output, session["fmt"], cmd, cwd, session["user"])
    if not sh.is_alive():
        reply += "\n\n<i>Shell session ended. Send any command to restart.</i>"

    try:
        bot.reply_to(message, reply, parse_mode="HTML")
    except Exception:
        bot.reply_to(message, output or "(no output)")


def _truncate(text: str, max_lines: int, max_bytes: int) -> str:
    lines = text.splitlines()
    total = len(lines)
    if total > max_lines:
        lines = lines[-max_lines:]
        header = f"[… {total - max_lines} earlier lines omitted …]\n"
    else:
        header = ""
    result = header + "\n".join(lines)
    encoded = result.encode("utf-8")
    if len(encoded) > max_bytes:
        result = "[… truncated …]\n" + encoded[-max_bytes:].decode("utf-8", errors="replace")
    return result


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


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def _format_shell_output(output: str, fmt: str, cmd: str = "", cwd: str = "", user: str = "") -> str:
    """Format PTY output using the session's current style."""
    has_out = bool(output and output.strip())

    if fmt == "terminal":
        _u = user or "user"
        _h = platform.node()
        prompt = f"<b>{html.escape(_u)}@{html.escape(_h)}:{html.escape(cwd or '~')}$</b>"
        if cmd:
            prompt += f" {html.escape(cmd)}"
        if has_out:
            return f"{prompt}\n\n<pre>{html.escape(output)}</pre>"
        return prompt

    if fmt == "minimal":
        return f"<pre>{html.escape(output)}</pre>" if has_out else "<i>(no output)</i>"

    if fmt == "compact":
        if has_out:
            return f"<code>📁 {html.escape(cwd)}</code>\n<pre>{html.escape(output)}</pre>"
        return f"<code>📁 {html.escape(cwd)}  (no output)</code>"

    if fmt == "verbose":
        parts = [f"<code>🖥 {html.escape(platform.node())}  📁 {html.escape(cwd)}</code>"]
        if cmd:
            parts.append(f"<code>$ {html.escape(cmd)}</code>")
        if has_out:
            parts.append(f"<pre>{html.escape(output)}</pre>")
        else:
            parts.append("<i>(no output)</i>")
        return "\n".join(parts)

    if fmt == "styled":
        parts = [f"📁 <b>{html.escape(cwd)}</b>"]
        if has_out:
            parts.append(f"<pre>{html.escape(output)}</pre>")
        else:
            parts.append("<i>(no output)</i>")
        return "\n\n".join(parts)

    if fmt == "rich":
        SEP = "━━━━━━━━━━━━━━━━━━━━━━"
        parts = [SEP, f"🖥 <b>{html.escape(platform.node())}</b>  ·  📁 <b>{html.escape(cwd)}</b>"]
        if cmd:
            parts.append(f"▸ <code>{html.escape(cmd)}</code>")
        parts.append(SEP)
        if has_out:
            parts.append(f"<pre>{html.escape(output)}</pre>")
        else:
            parts.append("<i>(no output)</i>")
        return "\n".join(parts)

    # standard
    if has_out:
        return f"<code>📁 {html.escape(cwd)}</code>\n<pre>{html.escape(output)}</pre>"
    return f"<code>📁 {html.escape(cwd)}</code>\n<i>(no output)</i>"


def _format_reply(result: ExecutionResult, timeout: int, cwd: str, fmt: str, cmd: str = "", user: str = "") -> str:
    """Format one-shot executor result (used by /rc_exec and shortcuts)."""
    has_out = bool(result.output.strip())

    if fmt == "terminal":
        _u = user or "user"
        _h = platform.node()
        prompt = f"<b>{html.escape(_u)}@{html.escape(_h)}:{html.escape(cwd)}$</b>"
        if cmd:
            prompt += f" {html.escape(cmd)}"
        parts = [prompt]
        if result.timed_out:
            parts.append(f"\n\n<i>killed — {timeout}s timeout</i>")
        elif result.error_msg:
            parts.append(f"\n\n<i>{html.escape(result.error_msg)}</i>")
        else:
            if has_out:
                parts.append(f"\n\n<pre>{html.escape(result.output)}</pre>")
            if result.exit_code not in (None, 0):
                parts.append(f"\n<i>exit {result.exit_code}  ·  {result.elapsed_seconds:.2f}s</i>")
            elif result.elapsed_seconds > 3.0:
                parts.append(f"\n<i>{result.elapsed_seconds:.2f}s</i>")
        return "".join(parts)

    def _status_text() -> str:
        if result.timed_out:  return f"TIMEOUT {timeout}s"
        if result.error_msg:  return "ERR"
        return "OK" if result.exit_code == 0 else f"FAIL({result.exit_code})"

    def _time_text() -> str:
        return f"{result.elapsed_seconds:.2f}s"

    if fmt == "minimal":
        if result.timed_out: return f"TIMEOUT after {timeout}s — process killed"
        if result.error_msg: return html.escape(result.error_msg)
        return f"<pre>{html.escape(result.output)}</pre>" if has_out else "<i>(no output)</i>"

    if fmt == "compact":
        status = _status_text()
        header = f"<code>📁 {html.escape(cwd)}  {html.escape(status)}  {_time_text()}</code>"
        if result.error_msg and not result.timed_out:
            return f"{header}\n<code>{html.escape(result.error_msg)}</code>"
        if has_out:
            return f"{header}\n<pre>{html.escape(result.output)}</pre>"
        return header

    if fmt == "verbose":
        parts = [f"<code>🖥 {html.escape(platform.node())}  📁 {html.escape(cwd)}</code>"]
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

    if fmt == "styled":
        if result.timed_out:
            footer = f"💀 <b>TIMEOUT {timeout}s</b>"
        elif result.error_msg:
            footer = f"⚠️ <b>ERR</b>  ·  <code>{_time_text()}</code>"
        elif result.exit_code == 0:
            footer = f"🟢 <b>OK</b>  ·  <code>{_time_text()}</code>"
        else:
            footer = f"🔴 <b>FAIL({result.exit_code})</b>  ·  <code>{_time_text()}</code>"
        parts = [f"📁 <b>{html.escape(cwd)}</b>"]
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

    if fmt == "rich":
        SEP = "━━━━━━━━━━━━━━━━━━━━━━"
        host = html.escape(platform.node())
        parts = [SEP, f"🖥 <b>{host}</b>  ·  📁 <b>{html.escape(cwd)}</b>"]
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

    # standard
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


def _format_picker_text(current: str) -> str:
    lines = [f"<b>Output format</b>  (current: <code>{current}</code>)\n"]
    for name, desc in _FORMATS.items():
        marker = "✓" if name == current else "·"
        lines.append(f"{marker} <b>{name}</b>  —  {desc}")
    lines.append("\nTap a button to switch:")
    return "\n".join(lines)


def _format_keyboard(current: str) -> InlineKeyboardMarkup:
    names = list(_FORMATS.keys())
    kb = InlineKeyboardMarkup()
    for i in range(0, len(names), 2):
        row = []
        for name in names[i:i + 2]:
            label = f"✓ {name}" if name == current else name
            row.append(InlineKeyboardButton(label, callback_data=f"fmt:{name}"))
        kb.add(*row)
    return kb


def _format_example(fmt: str) -> str:
    examples = {
        "terminal": "<b>maor@myhost:/home/maor$</b> ls -la\n\n<pre>total 48\ndrwxr-xr-x  5 maor maor  4096 Jun 22</pre>",
        "minimal":  "<pre>total 48\ndrwxr-xr-x  5 maor maor  4096 Jun 22</pre>",
        "standard": "📁 /home/maor\n<pre>total 48\n...</pre>\nOK  0.02s",
        "compact":  "📁 /home/maor  OK  0.02s\n<pre>total 48\n...</pre>",
        "verbose":  "🖥 myhost  📁 /home/maor\n$ ls -la\n<pre>total 48\n...</pre>\nexit 0  ·  0.02s",
        "styled":   "📁 <b>/home/maor</b>\n\n<pre>total 48\n...</pre>\n\n🟢 <b>OK</b>  ·  <code>0.02s</code>",
        "rich":     "━━━━━━━━━━━━━━━━━━━━━━\n🖥 <b>myhost</b>  ·  📁 <b>/home/maor</b>\n▸ <code>ls -la</code>\n━━━━━━━━━━━━━━━━━━━━━━\n<pre>total 48\n...</pre>",
    }
    return f"<b>Preview:</b>\n{examples.get(fmt, '')}"


def _send_reply(bot, message, result, timeout, cwd, fmt, cmd, app_logger, user="") -> None:
    reply = _format_reply(result, timeout, cwd, fmt, cmd, user)
    try:
        bot.reply_to(message, reply, parse_mode="HTML")
    except Exception as exc:
        app_logger.warning("HTML reply failed, sending plain text: %s", exc)
        bot.reply_to(message, _strip_html(reply))


def _strip_html(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text)
