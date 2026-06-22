# Telegram Print Bot

A Telegram bot that receives PDFs and images, asks for confirmation, and sends them to a printer. It supports **CUPS** (local/network printer via `lp`) or **Samba** (Windows shared printer via `smbclient`).

## Prerequisites

- **Linux-like OS** (Raspberry too)
- **Python 3.14+**
- **[uv](https://docs.astral.sh/uv/)** (recommended) or another way to create a virtualenv and install dependencies
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- System tools used at runtime:
  - **CUPS mode:** (`cups-client` on Debian/Ubuntu)
  - **Samba mode:** (`smbclient` package)
  - **PDF checks:** handled in Python via `pikepdf` (no extra system packages)

On Debian/Ubuntu:

```bash
sudo apt install cups-client smbclient
```

## Installation

1. Clone or copy the project to a directory (e.g. `/opt/telegram-print-bot`).

2. Create the virtual environment and install dependencies with uv:

```bash
cd /opt/telegram-print-bot
uv sync
```

This creates `.venv/` and installs packages from `pyproject.toml`.

3. Create your configuration file from the example:

```bash
cp config.env.example config.env
chmod 600 config.env
```

4. Edit `config.env` with your values (see [Configuration](#configuration) below).

5. (Optional) Test manually before enabling systemd:

```bash
set -a && source config.env && set +a
.venv/bin/python bot.py
```

Stop with `Ctrl+C` once you confirm the bot starts and responds in Telegram.

## Configuration

Variables are read from the process environment. When run under systemd, they are loaded from `config.env` via `EnvironmentFile`.

### Mandatory variables

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather. The bot refuses to start without it. |
| `ALLOWED_USER_IDS` | Comma-separated Telegram user IDs allowed to print (e.g. `123456789,987654321`). Get your ID from [@userinfobot](https://t.me/userinfobot). Users not in this list are rejected. |

You must also configure **one** print backend:

**CUPS (default)** — set:

| Variable | Description |
|----------|-------------|
| `CUPS_PRINTER_NAME` | CUPS queue name (see `lpstat -p` or `lpstat -a`). |

**Samba** — set `USE_SAMBA=true` and all of:

| Variable | Description |
|----------|-------------|
| `USE_SAMBA` | `true`, `yes`, or `1` to use Samba instead of CUPS. |
| `SAMBA_HOST` | Windows host IP or hostname. |
| `SAMBA_SHARE` | Shared printer name on that host. |
| `SAMBA_USER` | Windows username. |
| `SAMBA_PASSWORD` | Windows password. |

### Example `config.env`

**CUPS:**

```env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
CUPS_PRINTER_NAME=MyPrinter
USE_SAMBA=false
ALLOWED_USER_IDS=123456789
```

**Samba:**

```env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
USE_SAMBA=true
SAMBA_HOST=192.168.1.100
SAMBA_SHARE=PrinterShare
SAMBA_USER=windows_user
SAMBA_PASSWORD=secret
ALLOWED_USER_IDS=123456789
```

Keep `config.env` out of version control (it is listed in `.gitignore`). Restrict permissions: `chmod 600 config.env`.

## Behavior

1. **Authorization** — Only user IDs listed in `ALLOWED_USER_IDS` can use the bot. Others receive an unauthorized message.

2. **Supported files**
   - **PDF documents** — downloaded and checked before printing.
   - **Images** (JPEG, PNG, etc.) and **photos** — converted to PDF with `img2pdf` when you confirm print.

3. **Confirmation flow**
   - Send a PDF or image to the bot.
   - The bot replies with inline buttons: **Print** or **Cancel**.
   - On **Print**, the file is sent to the configured printer (CUPS or Samba).
   - On **Cancel**, the pending job is discarded.

4. **PDF protection** — Password-protected PDFs or PDFs with printing disabled are rejected. The user is asked to unlock the file and send it again.

5. **Commands** — `/start` shows a short welcome message.

6. **Logging** — INFO-level logs go to stdout/stderr (journal when run under systemd). HTTP client noise from `httpx` is suppressed.

7. **Polling** — The bot uses long polling against the Telegram API (no webhook or inbound port required).

## systemd service

A unit file is included: `telegram-print-bot.service`.

### 1. Adjust the unit file

Edit paths and user if your install differs from the defaults:

```ini
User=printer
WorkingDirectory=/opt/telegram-print-bot
ExecStart=/opt/telegram-print-bot/.venv/bin/python bot.py
EnvironmentFile=/opt/telegram-print-bot/config.env
```

Replace `printer` with the user that should run the bot. That user needs:

- Read access to the project and `config.env`
- Permission to print (`lp` for CUPS, or network access to the Samba host)

### 2. Install and enable

```bash
sudo cp /opt/telegram-print-bot/telegram-print-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable telegram-print-bot
sudo systemctl start telegram-print-bot
```

### 3. Manage the service

```bash
sudo systemctl status telegram-print-bot
sudo systemctl restart telegram-print-bot
sudo systemctl stop telegram-print-bot
```

### 4. View logs

```bash
journalctl -u telegram-print-bot -f
```

The service is configured with `Restart=always` and `RestartSec=10`, so it restarts automatically after crashes or reboots (when enabled).

## Troubleshooting

| Issue | Things to check |
|-------|-----------------|
| Bot does not start | `TELEGRAM_BOT_TOKEN` set in `config.env`; `journalctl -u telegram-print-bot` for errors |
| Unauthorized | Your Telegram user ID is in `ALLOWED_USER_IDS` |
| Print fails (CUPS) | `lpstat -p`; printer name matches `CUPS_PRINTER_NAME`; user in `lp` group if required |
| Print fails (Samba) | Host reachable; share/credentials correct; `smbclient` installed |
| Protected PDF | Remove password or print restrictions before sending |
