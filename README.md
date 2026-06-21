# Secure Remote Chat CLI

Control your Linux machine through Telegram — securely, from anywhere, with a full audit trail.

Built for machines locked behind WireGuard with **zero open inbound ports**. Uses outbound long-polling exclusively, so no firewall rules or port-forwarding ever needed.

---

## Features

- **Telegram long-polling** — outbound only, no webhooks, no inbound ports
- **Identity allowlist** — messages from unknown user IDs are silently dropped
- **Shell injection prevention** — `shell=False` + `shlex.split()` throughout
- **Hard timeout** — SIGKILL sent to the entire process group after N seconds
- **Interactive command guard** — blocks `vim`, `ssh`, `top`, etc. before they spawn
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

## Security Model

| Layer | Mechanism |
|---|---|
| Network | WireGuard — zero inbound ports, long-polling only |
| Identity | `ALLOWED_TELEGRAM_USER_IDS` allowlist in `.env` |
| Chat scope | Private DMs only — group chats rejected |
| Execution | `shell=False` + `shlex.split()` — no injection surface |
| Timeout | `SIGKILL` on entire process group via `os.killpg` |
| Interactive block | Blocklist checked before spawn; `stdin=DEVNULL` as backstop |
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
COMMAND_TIMEOUT=10
MAX_OUTPUT_LINES=50
MAX_OUTPUT_BYTES=3800
LOG_DIR=/var/log/remote-cli
```

**Get your user ID:** message [@userinfobot](https://t.me/userinfobot) on Telegram.

---

## Sudo Allowlist

The `chatcli` user can be granted passwordless access to specific commands via `/etc/sudoers.d/chatcli`. A documented template is provided:

```bash
# From the management console:
[7] Config → [3] Install sudoers → [2] Edit sudoers

# Or manually:
sudo cp scripts/sudoers_chatcli.example /etc/sudoers.d/chatcli
sudo chmod 440 /etc/sudoers.d/chatcli
sudo visudo -f /etc/sudoers.d/chatcli   # edit to your needs
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
│   ├── executor.py               ← command engine (shlex, SIGKILL, truncation)
│   ├── security.py               ← allowlist check, interactive command blocklist
│   ├── telegram_bot.py           ← long-polling bot, access control, HTML replies
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
