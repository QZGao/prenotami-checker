# PrenotaMi Schengen Visa Checker + Manual Challenge Resume

This checker monitors the Italian consulate's [PrenotaMi](https://prenotami.esteri.it/) appointment system for **Schengen visa** slots, attempts to auto-book when it thinks a slot is available, and sends Telegram alerts.

The current flow is built for the Ubuntu/VNC setup discussed in this repo:

1. A single long-lived Playwright browser profile stays open.
2. If PrenotaMi/Radware presents a bot challenge, the checker pauses instead of retrying.
3. Telegram sends you the challenge screenshot plus your configured VNC/noVNC connection hint.
4. You solve the challenge in that exact browser session.
5. You send `/resume` in Telegram and the checker continues from the same profile.

## Why This Refactor Exists

PrenotaMi may redirect automated traffic to `validate.perfdrive.com` before the real site loads. Once that happens, a second browser instance is the wrong tool because cookies, challenge state, and login session belong to the original browser context.

This refactor keeps one browser owner and turns Telegram into a control plane:

- `/status`
- `/screenshot`
- `/pause`
- `/resume`
- `/help`

## Prerequisites

- **Python 3.10+**
- A **PrenotaMi account**
- A **Telegram bot token** and **chat ID**
- A **desktop session on the server** if you want manual challenge solving
  - VNC or noVNC is the intended model
  - run with `BROWSER_HEADLESS=false`

## Setup

```bash
git clone https://github.com/anglil/prenotami-checker.git
cd prenotami-checker

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium

cp .env.example .env
```

Edit `.env` with your credentials and VNC hint:

```bash
PRENOTAMI_EMAIL=your-email@example.com
PRENOTAMI_PASSWORD=your-password
TELEGRAM_BOT_TOKEN=123456789:your-telegram-bot-token
TELEGRAM_CHAT_ID=123456789
CHECK_INTERVAL=300
BROWSER_HEADLESS=false
BROWSER_PROFILE_DIR=.browser-profile
MANUAL_SOLVE_URL=https://your-server.example.com:6080/vnc.html
MANUAL_SOLVE_NOTE=Open the VNC session, solve the challenge in the existing browser window, then send /resume on Telegram.
TELEGRAM_POLL_TIMEOUT=15
PLAYWRIGHT_NO_SANDBOX=false
BROWSER_WIDTH=1280
BROWSER_HEIGHT=800
BROWSER_LOCALE=en-US
BROWSER_TIMEZONE=America/Los_Angeles
DEFAULT_TIMEOUT_MS=20000
```

Additional optional browser/runtime config supported by code:

- `BROWSER_WIDTH` and `BROWSER_HEIGHT` control the viewport size
- `BROWSER_LOCALE` controls Playwright locale
- `BROWSER_TIMEZONE` controls Playwright timezone
- `BROWSER_USER_AGENT` overrides the default browser user agent
- `DEFAULT_TIMEOUT_MS` controls the Playwright default timeout

## Telegram Commands

- `/status` shows current mode, URL, profile dir, and whether the checker is paused
- `/screenshot` sends a fresh screenshot from the current browser page
- `/pause` pauses the loop at the next safe point
- `/resume` resumes after a pause or manual challenge solve
- `/help` prints the command list

## Manual Challenge Flow

When the checker detects a bot challenge:

1. It captures a screenshot and sends it to Telegram.
2. It pauses the loop indefinitely.
3. It keeps the same Playwright profile and browser session alive.
4. You connect to that session through VNC/noVNC and solve the challenge.
5. You send `/resume` in Telegram.
6. The checker verifies the challenge is gone and restarts the check flow.

Important constraints:

- Only one checker process should own the browser profile at a time.
- Do not start a second checker instance against the same `BROWSER_PROFILE_DIR`.
- If you run with `BROWSER_HEADLESS=true`, Telegram pause/resume still works, but there may be no visible browser window to solve manually.

## Usage

### Single check

```bash
source .venv/bin/activate
python checker.py
```

### Continuous monitoring

```bash
source .venv/bin/activate
python checker.py --loop
```

### Wrapper scripts

```bash
./run_checker.sh
./run_loop.sh
```

The wrappers now look for `.venv/` first and fall back to `venv/`.
`run_loop.sh` delegates to `python checker.py --loop` so `CHECK_INTERVAL` is respected by the Python runtime rather than duplicated in shell.

## Ubuntu + systemd

Prefer running `checker.py --loop` directly under `systemd`, not `run_loop.sh`.

Example unit:

```ini
[Unit]
Description=PrenotaMi Checker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_LINUX_USER
WorkingDirectory=/opt/prenotami-checker
Environment=DISPLAY=:1
ExecStart=/opt/prenotami-checker/.venv/bin/python /opt/prenotami-checker/checker.py --loop
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Notes:

- `DISPLAY=:1` is only an example; match your VNC/X server.
- If your server user needs Chromium without sandbox, set `PLAYWRIGHT_NO_SANDBOX=true` in `.env`.
- The browser profile is persisted in `BROWSER_PROFILE_DIR`, so keep that path stable across restarts.

## Logs

- Runtime logs: `logs/checker.log`
- Notifications: `logs/notifications.log`
- Challenge screenshots: `logs/*challenge*.png`
- State snapshot: `.state.json`

## Current Scope

This repo still targets the San Francisco consulate and still contains the existing hard-coded booking details for the intended applicant. The refactor changed runtime/session handling, not the applicant-specific booking payload.

## License

MIT
