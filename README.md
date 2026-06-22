# Secure Remote Chat CLI

Control your Linux machine through Telegram — with a **persistent bash shell**, full shell state, and a complete audit trail.

Built for machines locked behind WireGuard with **zero open inbound ports**. Uses outbound long-polling exclusively, so no firewall rules or port-forwarding ever needed.

---

## How it works

Every Telegram message you send is written directly into a persistent bash shell running on your server via a PTY. The shell stays alive between messages, so **all state persists**:

- Current working directory (`cd` works normally)
- Environment variables (`export FOO=bar` then `echo $FOO` → `bar`)
- Aliases and shell functions
- Active virtualenvs (`source venv/bin/activate` then `which python` → venv python)
- Conda environments (`conda activate myenv` then `python` → conda python)
- Command history

All shell syntax works natively — no wrapping required:

```
|    &&    ||    ;    >    >>    $()    &
```

---

## Features

- **Persistent PTY shell** — real bash session, full state across messages
- **Telegram long-polling** — outbound only, no webhooks, no inbound ports
- **Identity allowlist** — messages from unknown user IDs are silently dropped
- **Dangerous command confirmation** — `rm -rf`, `mkfs`, `shutdown`, etc. require inline-keyboard confirmation
- **Ctrl+C / Ctrl+D support** — interrupt a running command at any time via `/rc_ctrl_c`
- **Shell status** — see PID, CWD, user, uptime with `/rc_status`
- **Rotating audit log** — every command, rejection, and timeout is logged; stdout never is
- **Unprivileged execution** — runs as a dedicated `chatcli` system user, never root
- **Systemd hardened** — `NoNewPrivileges`, `ProtectSystem=strict`, `SystemCallFilter`, and more
- **One-file manager** — `run.sh` handles install, start/stop, logs, config, and updates

---

## Quick Start

```bash
git clone https://github.com/akamaor/remote-cli.git
cd remote-cli
./run.sh
```

Select **[1] Install** from the menu. It will walk you through every step.

---

## Bot Commands

### Shell controls

| Command | Description |
|---|---|
| `/rc_ctrl_c` | Send Ctrl+C — interrupt a running command |
| `/rc_ctrl_d` | Send Ctrl+D — EOF to the shell |
| `/rc_restart` | Kill and restart the persistent shell |
| `/rc_exit` | Close the shell (auto-restarts on next command) |
| `/rc_status` | Show shell PID, CWD, user, uptime, last command |
| `/rc_history` | Show last 20 commands |
| `/rc_pwd` | Show current working directory |
| `/rc_exec <cmd>` | One-shot isolated command (no shell state, old behaviour) |
| `/rc_style` | Change output display style |
| `/rc_ping` | Latency and host check |
| `/rc_help` | Full help and command list |

### System shortcuts

`/sysinfo` `/hostname` `/cpu` `/uptime` `/whoami` `/env`  
`/ls` `/df` `/disk` `/du` `/inodes`  
`/free` `/ps` `/top5cpu` `/top5mem` `/vmstat`  
`/ip` `/routes` `/netstat` `/connections` `/dns`  
`/services` `/failed` `/timers`  
`/logs` `/errors` `/auth` `/rc_logs`  
`/who` `/last` `/users` `/sudoers`  
`/updates` `/installed`

---

## Usage Examples

```
# Environment persistence
export DATABASE_URL=postgres://localhost/mydb
echo $DATABASE_URL
→ postgres://localhost/mydb

# Directory navigation
cd /var/log
ls -la
→ lists /var/log

# Virtualenv
python3 -m venv venv && source venv/bin/activate
which python
→ /home/user/venv/bin/python

# Pipes and chaining
ps aux | grep nginx | wc -l
find /var/log -name "*.log" -newer /tmp/marker | xargs wc -l

# Interrupt a long-running command
sleep 100
/rc_ctrl_c
→ ^C  (shell still alive)

# Check shell state
/rc_status
→ PID: 12345
   CWD: /var/log
   Uptime: 5m 12s
```

---

## Output Styles

Switch with `/rc_style <name>` or tap the inline picker:

| Style | Description |
|---|---|
| `terminal` | `user@host:cwd$` — classic shell prompt (default) |
| `minimal` | Raw output only — cleanest for copy-paste |
| `standard` | `📁 cwd` header + output |
| `compact` | CWD and status on one line, then output |
| `verbose` | Hostname + CWD + echoed command + timing |
| `styled` | 🟢/🔴 emoji status + bold text |
| `rich` | `━━` border header with hostname and CWD |

---

## Security Model

| Layer | Mechanism |
|---|---|
| Network | WireGuard — zero inbound ports, long-polling only |
| Identity | `ALLOWED_TELEGRAM_USER_IDS` allowlist in `.env` |
| Chat scope | Private DMs only — group chats rejected |
| Dangerous commands | Inline-keyboard confirmation for `rm -rf`, `mkfs`, `shutdown`, disk writes, etc. |
| Timeout | PTY command timeout (default 60s), configurable up to 3600s |
| Logging | stdout never written to log — sensitive output stays out of plaintext files |
| Privilege | Dedicated `chatcli` system user + visudo allowlist |
| Systemd | `NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, `SystemCallFilter` |

Unauthorized senders receive **no reply** — the bot's existence is not confirmed to probing attackers.

---

## Configuration

Copy `.env.example` to `/opt/remote-cli/.env` and fill in:

```env
TELEGRAM_BOT_TOKEN=your_token_from_botfather
ALLOWED_TELEGRAM_USER_IDS=123456789
HOME_DIR=/home/youruser

# Shell
DEFAULT_SHELL=/bin/bash
REQUIRE_CONFIRM_FOR_DANGEROUS=true

# Output
OUTPUT_FORMAT=terminal

# Limits
COMMAND_TIMEOUT=60
MAX_OUTPUT_LINES=200
MAX_OUTPUT_BYTES=3900
LOG_DIR=/var/log/remote-cli
```

**Get your user ID:** message [@userinfobot](https://t.me/userinfobot) on Telegram.

---

## Management Console

`run.sh` is the single entry point for everything:

```
  SERVICE
  [1]  Install / Reinstall
  [2]  Status
  [3]  Start
  [4]  Stop
  [5]  Restart

  MANAGEMENT
  [6]  Logs       → live journal, app log, audit log, rejection events
  [7]  Config     → edit .env, install/edit sudoers allowlist
  [8]  Update     → git pull + rsync + pip install + restart

  DANGER ZONE
  [9]  Uninstall

  [a]  About   [0]  Exit
```

---

## Sudo Allowlist

The `chatcli` user can be granted passwordless access to specific commands via `/etc/sudoers.d/chatcli`:

```bash
# From the management console:
[7] Config → [3] Install sudoers → [2] Edit sudoers

# Or manually:
sudo cp scripts/sudoers_chatcli.example /etc/sudoers.d/chatcli
sudo chmod 440 /etc/sudoers.d/chatcli
sudo visudo -f /etc/sudoers.d/chatcli
```

Only list commands you actually use. Avoid wildcards like `systemctl *`.

---

## Project Structure

```
remote-cli/
├── run.sh                        ← management console (start here)
├── .env.example                  ← configuration template
├── requirements.txt
├── src/
│   ├── main.py                   ← entry point
│   ├── config.py                 ← loads and validates .env
│   ├── pty_shell.py              ← persistent PTY bash session (PtyShell)
│   ├── executor.py               ← one-shot command engine (used by /rc_exec)
│   ├── security.py               ← allowlist check, dangerous command patterns
│   ├── telegram_bot.py           ← long-polling bot, PTY routing, all handlers
│   └── logger_setup.py           ← rotating app.log + audit.log
├── systemd/
│   └── remote-cli.service        ← hardened unit file
└── scripts/
    ├── setup.sh                  ← headless provisioning (alternative to run.sh)
    └── sudoers_chatcli.example   ← visudo template
```

---

## Requirements

- Python 3.10+
- `python3-venv`
- `rsync`
- `systemd`

Dependencies installed automatically by `run.sh`:

```
pyTelegramBotAPI>=4.14.0
python-dotenv>=1.0.0
```

---

## Roadmap

- [x] Telegram (long-polling)
- [x] Persistent PTY shell — full bash state across messages
- [x] Ctrl+C / Ctrl+D support
- [x] Dangerous command confirmation
- [ ] Signal integration
- [ ] WhatsApp integration

The architecture is chat-provider-agnostic — new adapters slot in alongside the Telegram module.

---

## Author

**Maor** aka [akamaor](https://github.com/akamaor)  
DevSecOps Engineer · Algorithm Engineer · Python Developer

---

## License

MIT
