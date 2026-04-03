from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path


def load_env(root_dir: Path) -> None:
    """Load .env file if it exists."""
    env_file = root_dir / ".env"
    if not env_file.exists():
        return

    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def resolve_path(root_dir: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root_dir / path
    return path


@dataclass(slots=True)
class Config:
    root_dir: Path
    log_dir: Path
    booked_file: Path
    state_file: Path
    telegram_offset_file: Path
    notification_log: Path
    browser_profile_dir: Path
    email: str
    password: str
    telegram_bot_token: str
    telegram_chat_id: str
    check_interval: int
    browser_headless: bool
    browser_width: int
    browser_height: int
    browser_locale: str
    browser_timezone: str
    user_agent: str
    playwright_no_sandbox: bool
    manual_solve_url: str
    manual_solve_note: str
    telegram_poll_timeout: int
    default_timeout_ms: int

    def validate(self) -> None:
        required = {
            "PRENOTAMI_EMAIL": self.email,
            "PRENOTAMI_PASSWORD": self.password,
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "TELEGRAM_CHAT_ID": self.telegram_chat_id,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise SystemExit(
                f"Missing required configuration: {', '.join(missing)}. "
                "Copy .env.example to .env and fill in the values."
            )


def build_config() -> Config:
    root_dir = Path(__file__).resolve().parent.parent
    load_env(root_dir)

    log_dir = root_dir / "logs"
    log_dir.mkdir(exist_ok=True)

    browser_profile_dir = resolve_path(
        root_dir,
        os.environ.get("BROWSER_PROFILE_DIR", ".browser-profile"),
    )
    browser_profile_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        root_dir=root_dir,
        log_dir=log_dir,
        booked_file=root_dir / ".booked",
        state_file=root_dir / ".state.json",
        telegram_offset_file=root_dir / ".telegram_offset",
        notification_log=log_dir / "notifications.log",
        browser_profile_dir=browser_profile_dir,
        email=os.environ.get("PRENOTAMI_EMAIL", ""),
        password=os.environ.get("PRENOTAMI_PASSWORD", ""),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        check_interval=int(os.environ.get("CHECK_INTERVAL", "300")),
        browser_headless=env_bool("BROWSER_HEADLESS", False),
        browser_width=int(os.environ.get("BROWSER_WIDTH", "1280")),
        browser_height=int(os.environ.get("BROWSER_HEIGHT", "800")),
        browser_locale=os.environ.get("BROWSER_LOCALE", "en-US"),
        browser_timezone=os.environ.get("BROWSER_TIMEZONE", "America/Los_Angeles"),
        user_agent=os.environ.get(
            "BROWSER_USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36",
        ),
        playwright_no_sandbox=env_bool("PLAYWRIGHT_NO_SANDBOX", False),
        manual_solve_url=os.environ.get("MANUAL_SOLVE_URL", ""),
        manual_solve_note=os.environ.get(
            "MANUAL_SOLVE_NOTE",
            "Open the existing browser session over VNC/noVNC, solve the challenge, "
            "then send /resume in Telegram.",
        ),
        telegram_poll_timeout=int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "15")),
        default_timeout_ms=int(os.environ.get("DEFAULT_TIMEOUT_MS", "20000")),
    )


def configure_logging(log_dir: Path) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "checker.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("prenotami")
