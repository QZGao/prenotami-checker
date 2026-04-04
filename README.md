# PrenotaMi Schengen Visa Checker

Monitors the Italian consulate's [PrenotaMi](https://prenotami.esteri.it/) appointment system for **Schengen visa** slots, attempts to book when a slot appears, and sends Telegram alerts.

## How It Works

1. Opens PrenotaMi in Chromium using [Playwright](https://playwright.dev/python/).
2. Logs into your PrenotaMi account.
3. Opens the Schengen visa **PRENOTA** flow.
4. If appointments are exhausted, waits for the next check.
5. If a slot appears, attempts to complete the booking form and submit it.
6. If the site shows an anti-bot challenge, pauses and waits for you to solve it in the same browser session, then continues after `/resume` in Telegram.

## Prerequisites

- **Python 3.10+**
- A **PrenotaMi account**
- A **Telegram bot token** and **chat ID**
- A **desktop session** only if you want manual challenge solving
  - for example VNC or noVNC
  - use `BROWSER_HEADLESS=false`

## Setup

```bash
git clone https://github.com/QZGao/prenotami-checker.git
cd prenotami-checker

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

cp .env.example .env
```

Edit `.env`:

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
```

Optional browser settings:

- `BROWSER_WIDTH`
- `BROWSER_HEIGHT`
- `BROWSER_LOCALE`
- `BROWSER_TIMEZONE`
- `BROWSER_USER_AGENT`
- `DEFAULT_TIMEOUT_MS`

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

## Telegram Commands

- `/status` shows the current mode and URL
- `/screenshot` sends a fresh browser screenshot
- `/pause` pauses the checker
- `/resume` resumes after a pause or challenge solve
- `/help` shows the command list

## Ubuntu / VNC

If you want to solve challenges manually on a server:

- run with `BROWSER_HEADLESS=false`
- keep the browser profile stable with `BROWSER_PROFILE_DIR`
- expose the same desktop session over VNC or noVNC
- set `MANUAL_SOLVE_URL` so Telegram alerts include the connection link

For `systemd`, run `checker.py --loop` directly and set `DISPLAY` if using a headed session.

Example:

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
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/prenotami-checker/.venv/bin/python /opt/prenotami-checker/checker.py --loop
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Notes

- Only one checker process should use a given `BROWSER_PROFILE_DIR`.
- The checker keeps the browser session open between runs and reuses the current page when possible.
- The booking payload in code is still applicant-specific. Update the hard-coded travel/applicant details before using it for someone else.
- This repo currently targets the San Francisco consulate flow.

## Logs

- Runtime log: `logs/checker.log`
- Notification log: `logs/notifications.log`
- Screenshots: `logs/`
- State snapshot: `.state.json`

## License

MIT
