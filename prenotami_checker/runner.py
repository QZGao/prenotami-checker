from __future__ import annotations

import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from .config import Config
from .exceptions import RestartLoop
from .prenotami import (
    BOOKING_PAGE_SELECTORS,
    LOGIN_LINK_SELECTORS,
    LOGIN_SUBMIT_SELECTORS,
    LOGGED_IN_SELECTORS,
    PASSWORD_SELECTORS,
    SERVICES_PAGE_SELECTORS,
    USERNAME_SELECTORS,
    URL_STATE_CHALLENGE,
    URL_STATE_PRENOTAMI,
    URL_STATE_SSO,
    attempt_auto_book,
    check_page_for_all_booked,
    classify_page_url,
    click_first_visible,
    detect_bot_challenge,
    fill_first_visible,
    is_services_page,
    wait_for_first_visible,
    wait_for_page_ready,
)
from .telegram_api import TelegramClient, write_notification_log


log = logging.getLogger("prenotami")


PAGE_STATE_CHALLENGE = "challenge"
PAGE_STATE_SSO_LOGIN = "sso_login"
PAGE_STATE_HOME_LOGGED_OUT = "home_logged_out"
PAGE_STATE_AUTHENTICATED = "authenticated"
PAGE_STATE_SERVICES = "services"
PAGE_STATE_ALL_BOOKED = "all_booked"
PAGE_STATE_BOOKING = "booking"
PAGE_STATE_PRENOTAMI_OTHER = "prenotami_other"
PAGE_STATE_UNKNOWN = "unknown"

AUTHENTICATED_PAGE_STATES = {
    PAGE_STATE_AUTHENTICATED,
    PAGE_STATE_SERVICES,
    PAGE_STATE_BOOKING,
}

KNOWN_PAGE_STATES = {
    PAGE_STATE_CHALLENGE,
    PAGE_STATE_SSO_LOGIN,
    PAGE_STATE_HOME_LOGGED_OUT,
    PAGE_STATE_AUTHENTICATED,
    PAGE_STATE_SERVICES,
    PAGE_STATE_ALL_BOOKED,
    PAGE_STATE_BOOKING,
    PAGE_STATE_PRENOTAMI_OTHER,
}

PAGE_STATE_PRIORITY = {
    PAGE_STATE_CHALLENGE: 70,
    PAGE_STATE_ALL_BOOKED: 60,
    PAGE_STATE_BOOKING: 50,
    PAGE_STATE_SERVICES: 40,
    PAGE_STATE_AUTHENTICATED: 30,
    PAGE_STATE_SSO_LOGIN: 20,
    PAGE_STATE_HOME_LOGGED_OUT: 10,
    PAGE_STATE_PRENOTAMI_OTHER: 5,
    PAGE_STATE_UNKNOWN: 0,
}

ENGLISH_PAGE_MARKERS = [
    "text=My appointments",
    "text=Services provided by the office",
    "text=Log out",
]

ITALIAN_PAGE_MARKERS = [
    "text=I miei appuntamenti",
    "text=Servizi erogati dalla sede",
    "text=Disconnetti",
]


@dataclass(slots=True)
class BrowserObservation:
    state: str
    page: object | None
    url: str
    language: str


@dataclass(slots=True)
class CheckContext:
    attempts: dict[str, int] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)
    prenota_clicked: bool = False
    autobook_attempted: bool = False
    last_marker: str = ""

    def begin_transition(self, observation: BrowserObservation) -> None:
        marker = f"{observation.state}:{observation.url}"
        if marker != self.last_marker:
            self.last_marker = marker
            self.attempts.clear()
        self.history.append(marker)
        if len(self.history) > 20:
            self.history = self.history[-20:]

    def record_attempt(self, name: str) -> int:
        count = self.attempts.get(name, 0) + 1
        self.attempts[name] = count
        return count

    def history_summary(self) -> str:
        if not self.history:
            return "(no observed states)"
        return " -> ".join(self.history)


def is_already_booked(booked_file: Path) -> bool:
    return booked_file.exists()


def mark_booked(booked_file: Path, details: str) -> None:
    booked_file.write_text(f"{datetime.now().isoformat()}\n{details}", encoding="utf-8")


class PrenotamiRunner:
    def __init__(self, config: Config):
        self.config = config
        self.telegram = TelegramClient(
            bot_token=config.telegram_bot_token,
            chat_id=config.telegram_chat_id,
            offset_file=config.telegram_offset_file,
        )
        self.playwright_manager = None
        self.context = None
        self.page = None
        self.stop_requested = False
        self.pause_requested = False
        self.resume_requested = False
        self.check_count = 0
        self.consecutive_errors = 0
        self.mode = "starting"
        self.pause_reason = ""
        self._install_signal_handlers()
        self.save_state("starting", message="Process initialized")

    def _install_signal_handlers(self) -> None:
        def handle_signal(signum, _frame):
            self.stop_requested = True
            log.info(f"Received signal {signum}, shutting down...")

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

    def current_display(self) -> str:
        return os.environ.get("DISPLAY", "")

    def _attach_page_handlers(self, page) -> None:
        if getattr(page, "_prenotami_dialog_handler", False):
            return
        page.on("dialog", lambda dialog: dialog.accept())
        setattr(page, "_prenotami_dialog_handler", True)

    def _page_sort_key(self, page) -> tuple[int, int]:
        try:
            state = classify_page_url(page.url)
            if state == URL_STATE_CHALLENGE:
                return (4, 0)
            if state == URL_STATE_PRENOTAMI:
                return (3, 0)
            if state == URL_STATE_SSO:
                return (2, 0)
            if page.url and page.url != "about:blank":
                return (1, 0)
        except Exception:
            pass
        return (0, 0)

    def _state_priority(self, state: str) -> int:
        return PAGE_STATE_PRIORITY.get(state, 0)

    def focus_page(self, page) -> None:
        if not page:
            return
        try:
            page.bring_to_front()
        except Exception:
            return
        try:
            page.evaluate("() => window.focus()")
        except Exception:
            pass

    def _open_pages(self) -> list[object]:
        if not self.context:
            return []
        return [page for page in self.context.pages if not page.is_closed()]

    def _track_page(self, page) -> None:
        if not page:
            return
        if page is not self.page:
            previous_url = getattr(self.page, "url", "") if self.page else ""
            new_url = getattr(page, "url", "")
            if new_url != previous_url:
                log.info("Switching tracked browser page to %s", new_url or "(blank)")
        self.page = page
        self._attach_page_handlers(self.page)
        self.focus_page(self.page)

    def _enumerate_page_states(self, probe_timeout: int = 400) -> list[tuple[int, str, object]]:
        entries: list[tuple[int, str, object]] = []
        for index, page in enumerate(self._open_pages()):
            try:
                state = self.classify_page_state(page, probe_timeout=probe_timeout)
            except Exception:
                state = PAGE_STATE_UNKNOWN
            entries.append((index, state, page))
        return entries

    def open_pages_snapshot(self, probe_timeout: int = 300) -> list[dict[str, object]]:
        snapshot: list[dict[str, object]] = []
        tracked = self.page
        for index, state, page in self._enumerate_page_states(probe_timeout=probe_timeout):
            try:
                url = page.url
            except Exception:
                url = ""
            snapshot.append(
                {
                    "index": index,
                    "state": state,
                    "url": url,
                    "tracked": page is tracked,
                }
            )
        return snapshot

    def open_pages_summary(self, probe_timeout: int = 300) -> str:
        pages = self.open_pages_snapshot(probe_timeout=probe_timeout)
        if not pages:
            return "(no open pages)"

        parts: list[str] = []
        for entry in pages:
            marker = "*" if entry["tracked"] else "-"
            parts.append(f"{marker}{entry['index']}:{entry['state']}:{entry['url']}")
        return " | ".join(parts)

    def _select_best_page(
        self,
        expected_states: set[str] | None = None,
        probe_timeout: int = 400,
    ) -> tuple[str, object] | None:
        entries = self._enumerate_page_states(probe_timeout=probe_timeout)
        if expected_states is not None:
            entries = [entry for entry in entries if entry[1] in expected_states]
        if not entries:
            return None

        _index, state, page = max(entries, key=lambda entry: (self._state_priority(entry[1]), entry[0]))
        self._track_page(page)
        return state, page

    def current_page(self, create: bool = True):
        if not self.context:
            return None

        pages = self._open_pages()
        if pages:
            best = self._select_best_page(probe_timeout=200)
            if best:
                _state, preferred = best
            else:
                preferred = max(enumerate(pages), key=lambda item: (self._page_sort_key(item[1]), item[0]))[1]
                self._track_page(preferred)
        elif create:
            self.page = self.context.new_page()
            self._track_page(self.page)
        else:
            self.page = None
        return self.page

    def _page_path(self, page) -> str:
        try:
            return urlparse(page.url).path.lower()
        except Exception:
            return ""

    def classify_page_state(self, page, probe_timeout: int = 400) -> str:
        if not page:
            return PAGE_STATE_UNKNOWN

        try:
            route = classify_page_url(page.url)
        except Exception:
            return PAGE_STATE_UNKNOWN

        if route == URL_STATE_CHALLENGE:
            return PAGE_STATE_CHALLENGE

        if route == URL_STATE_SSO:
            return PAGE_STATE_SSO_LOGIN

        if route != URL_STATE_PRENOTAMI:
            return PAGE_STATE_UNKNOWN

        if check_page_for_all_booked(page):
            return PAGE_STATE_ALL_BOOKED

        path = self._page_path(page)
        if path.startswith("/services"):
            return PAGE_STATE_SERVICES
        if path.startswith("/userarea"):
            return PAGE_STATE_AUTHENTICATED

        if wait_for_first_visible(page, LOGIN_LINK_SELECTORS, timeout=probe_timeout):
            return PAGE_STATE_HOME_LOGGED_OUT

        if wait_for_first_visible(page, BOOKING_PAGE_SELECTORS, timeout=probe_timeout):
            return PAGE_STATE_BOOKING

        if wait_for_first_visible(page, SERVICES_PAGE_SELECTORS, timeout=probe_timeout):
            return PAGE_STATE_SERVICES

        if wait_for_first_visible(page, LOGGED_IN_SELECTORS, timeout=probe_timeout):
            return PAGE_STATE_AUTHENTICATED

        return PAGE_STATE_PRENOTAMI_OTHER

    def current_page_state(self, create: bool = True, probe_timeout: int = 400) -> tuple[str, object | None]:
        page = self.current_page(create=create)
        if not page:
            return PAGE_STATE_UNKNOWN, None
        state = self.classify_page_state(page, probe_timeout=probe_timeout)
        return state, page

    def observe(self, create: bool = True, probe_timeout: int = 400) -> BrowserObservation:
        state, page = self.current_page_state(create=create, probe_timeout=probe_timeout)
        url = page.url if page else ""
        language = self.detect_page_language(page, probe_timeout=min(probe_timeout, 300)) if page else "unknown"
        return BrowserObservation(
            state=state,
            page=page,
            url=url,
            language=language,
        )

    def wait_for_observation(
        self,
        predicate,
        timeout: int = 5000,
        probe_timeout: int = 300,
    ) -> BrowserObservation:
        deadline = time.time() + (timeout / 1000)
        last = self.observe(create=True, probe_timeout=probe_timeout)
        while time.time() < deadline:
            last = self.observe(create=True, probe_timeout=probe_timeout)
            if predicate(last):
                return last
            time.sleep(0.2)
        return last

    def wait_for_observation_change(
        self,
        previous: BrowserObservation,
        timeout: int = 5000,
        probe_timeout: int = 300,
    ) -> BrowserObservation:
        return self.wait_for_observation(
            lambda current: (
                current.state != previous.state
                or current.url != previous.url
                or current.language != previous.language
            ),
            timeout=timeout,
            probe_timeout=probe_timeout,
        )

    def wait_for_page_state(
        self,
        expected_states: set[str] | list[str] | tuple[str, ...] | None = None,
        timeout: int = 30000,
        settle_seconds: float = 0.0,
        probe_timeout: int = 400,
    ) -> tuple[str, object]:
        expected = set(expected_states or KNOWN_PAGE_STATES)
        deadline = time.time() + (timeout / 1000)
        seen_states: list[str] = []

        while time.time() < deadline:
            entries = self._enumerate_page_states(probe_timeout=probe_timeout)
            if not entries:
                time.sleep(0.2)
                continue

            for _index, state, page in entries:
                try:
                    current_url = page.url
                except Exception:
                    current_url = ""
                stamp = f"{state}:{current_url}"
                if current_url and stamp not in seen_states:
                    seen_states.append(stamp)
                    if len(seen_states) > 10:
                        seen_states = seen_states[-10:]

            selected = self._select_best_page(expected_states=expected, probe_timeout=probe_timeout)
            if selected:
                state, page = selected
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=1000)
                except Exception:
                    pass
                if settle_seconds:
                    time.sleep(settle_seconds)
                return state, page

            time.sleep(0.2)

        final_match = self._select_best_page(expected_states=expected, probe_timeout=probe_timeout)
        if final_match:
            state, page = final_match
            log.warning(
                "Page state %s reached at timeout boundary; continuing with %s",
                state,
                page.url,
            )
            if settle_seconds:
                time.sleep(settle_seconds)
            return state, page

        state, page = self.current_page_state(create=False, probe_timeout=probe_timeout)
        trail = " -> ".join(seen_states) if seen_states else "(no recognizable page state observed)"
        current_url = page.url if page else "(unknown)"
        raise RuntimeError(
            f"Timed out waiting for page state {sorted(expected)}. "
            f"Current state: {state}. Current URL: {current_url}. Recent states: {trail}"
        )

    def detect_page_language(self, page, probe_timeout: int = 500) -> str:
        if not page:
            return "unknown"
        if classify_page_url(page.url) != URL_STATE_PRENOTAMI:
            return "unknown"

        if wait_for_first_visible(page, ENGLISH_PAGE_MARKERS, timeout=probe_timeout):
            return "en"
        if wait_for_first_visible(page, ITALIAN_PAGE_MARKERS, timeout=probe_timeout):
            return "it"
        return "unknown"

    def ensure_english_language(self, page=None) -> bool:
        if page is None:
            state, page = self.current_action_page("auth:language-switch")
        else:
            state = self.classify_page_state(page, probe_timeout=500)

        if not page:
            return False

        if state not in AUTHENTICATED_PAGE_STATES and state != PAGE_STATE_PRENOTAMI_OTHER:
            return False

        current_language = self.detect_page_language(page, probe_timeout=400)
        if current_language == "en":
            return True

        before = BrowserObservation(
            state=state,
            page=page,
            url=page.url,
            language=current_language,
        )

        try:
            clicked = page.evaluate(
                """() => {
                    const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim().toUpperCase();
                    const candidates = Array.from(document.querySelectorAll('a, button'));
                    for (const el of candidates) {
                        const label = normalize(el.textContent);
                        if (label === 'EN' || label === 'ENG' || label === 'ENGLISH') {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                }"""
            )
        except Exception as exc:
            log.warning("English language switch failed on %s: %s", page.url, exc)
            return False

        if not clicked:
            log.info("No English language switch control found on %s", page.url)
            return False

        log.info("Switching PrenotaMi language to English...")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        updated = self.wait_for_observation(
            lambda current: (
                current.language == "en"
                or current.state != before.state
                or current.url != before.url
            ),
            timeout=8000,
            probe_timeout=300,
        )
        return updated.language == "en"

    def _recover_from_sso_state_change(self, stage: str) -> bool:
        page = self.current_page(create=True)
        state = self.classify_page_state(page, probe_timeout=500)
        if state == PAGE_STATE_SSO_LOGIN:
            return False

        log.info("SSO page changed to %s during %s (%s)", state, stage, page.url)
        return True

    def current_action_page(self, stage: str, probe_timeout: int = 500) -> tuple[str, object]:
        selected = self._select_best_page(probe_timeout=probe_timeout)
        if selected:
            state, page = selected
        else:
            page = self.current_page(create=True)
            state = self.classify_page_state(page, probe_timeout=probe_timeout)
        if state == PAGE_STATE_CHALLENGE:
            indicator = detect_bot_challenge(page) or page.url
            self.pause_for_challenge(stage, indicator)
            selected = self._select_best_page(probe_timeout=probe_timeout)
            if selected:
                state, page = selected
            else:
                page = self.current_page(create=True)
                state = self.classify_page_state(page, probe_timeout=probe_timeout)
        return state, page

    def click_visible_on_current_page(
        self,
        stage: str,
        selectors: list[str],
        timeout: int,
        allowed_states: set[str] | None = None,
    ) -> tuple[str | None, str, object]:
        state, page = self.current_action_page(stage)
        if allowed_states and state not in allowed_states:
            return None, state, page

        clicked = click_first_visible(page, selectors, timeout=timeout)
        if clicked:
            return clicked, state, page

        state, page = self.current_action_page(f"{stage}:after-miss")
        return None, state, page

    def fill_visible_on_current_page(
        self,
        stage: str,
        selectors: list[str],
        value: str,
        timeout: int,
        allowed_states: set[str] | None = None,
    ) -> tuple[str | None, str, object]:
        state, page = self.current_action_page(stage)
        if allowed_states and state not in allowed_states:
            return None, state, page

        filled = fill_first_visible(page, selectors, value, timeout=timeout)
        if filled:
            return filled, state, page

        state, page = self.current_action_page(f"{stage}:after-miss")
        return None, state, page

    def save_state(self, mode: str, **extra: object) -> None:
        page = self.current_page(create=False)
        state = {
            "mode": mode,
            "pause_reason": self.pause_reason,
            "check_count": self.check_count,
            "build_id": self.config.build_id,
            "headless": self.config.browser_headless,
            "display": self.current_display(),
            "profile_dir": str(self.config.browser_profile_dir),
            "url": page.url if page else "",
            "open_pages": self.open_pages_snapshot(probe_timeout=200),
            "updated_at": datetime.now().isoformat(),
        }
        state.update(extra)
        self.config.state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        self.mode = mode

    def notify(self, subject: str, body: str, photo_path: Path | None = None) -> None:
        try:
            write_notification_log(self.config.notification_log, subject, body)
            log.info(f"Notification logged to {self.config.notification_log}")
        except Exception as exc:
            log.error(f"Failed to write notification log: {exc}")

        self.telegram.send_message(f"{subject}\n\n{body}")
        if photo_path:
            self.telegram.send_photo(photo_path, caption=subject)

    def send_help(self) -> None:
        self.telegram.send_message(
            "Commands:\n"
            "/status - current checker state\n"
            "/screenshot - send a fresh browser screenshot\n"
            "/pause - pause the loop at the next safe point\n"
            "/resume - resume after a pause or manual challenge solve\n"
            "/help - show this help"
        )

    def send_status(self) -> None:
        page = self.current_page(create=False)
        lines = [
            f"Mode: {self.mode}",
            f"Build: {self.config.build_id}",
            f"Checks completed: {self.check_count}",
            f"Headless: {self.config.browser_headless}",
            f"Display: {self.current_display() or '(not set)'}",
            f"Profile: {self.config.browser_profile_dir}",
            f"Booked: {is_already_booked(self.config.booked_file)}",
        ]
        if self.pause_reason:
            lines.append(f"Pause reason: {self.pause_reason}")
        if page:
            lines.append(f"Current URL: {page.url}")
        lines.append(f"Open pages: {self.open_pages_summary(probe_timeout=200)}")
        if self.config.manual_solve_url:
            lines.append(f"Manual solve URL: {self.config.manual_solve_url}")
        self.telegram.send_message("\n".join(lines))

    def capture_page(self, prefix: str, full_page: bool = True) -> Path | None:
        page = self.current_page(create=False)
        if not page:
            return None

        filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{prefix}.png"
        path = self.config.log_dir / filename
        try:
            page.screenshot(path=str(path), full_page=full_page)
            return path
        except Exception as exc:
            log.warning(f"Screenshot failed: {exc}")
            return None

    def handle_command(self, text: str) -> None:
        command = text.strip().split()[0].split("@")[0].lower()

        if command == "/help":
            self.send_help()
        elif command == "/status":
            self.send_status()
        elif command == "/screenshot":
            shot = self.capture_page("telegram_request")
            if shot:
                self.telegram.send_photo(shot, caption="Current PrenotaMi browser view")
            else:
                self.telegram.send_message("No active browser page is available for screenshot.")
        elif command == "/pause":
            if self.mode.startswith("paused"):
                self.telegram.send_message("Checker is already paused.")
            else:
                self.pause_requested = True
                self.telegram.send_message("Pause requested. The checker will pause at the next safe point.")
        elif command == "/resume":
            if self.mode.startswith("paused"):
                self.resume_requested = True
                self.telegram.send_message("Resume requested. Verifying browser state...")
            else:
                self.telegram.send_message("Checker is not paused.")

    def poll_telegram_commands(self, timeout: int = 0) -> None:
        for update in self.telegram.get_updates(timeout=timeout):
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            if str(chat.get("id", "")) != self.config.telegram_chat_id:
                continue
            text = message.get("text")
            if text and text.startswith("/"):
                self.handle_command(text)

    def ensure_browser(self) -> None:
        if self.context:
            return

        if not self.config.browser_headless and not self.current_display():
            raise RuntimeError(
                "BROWSER_HEADLESS=false requires a running X server and DISPLAY to be set. "
                "Start the checker inside a VNC/noVNC desktop session and set DISPLAY=:N, "
                "or set BROWSER_HEADLESS=true."
            )

        self.playwright_manager = sync_playwright().start()
        launch_args: list[str] = []
        if self.config.playwright_no_sandbox:
            launch_args.append("--no-sandbox")

        try:
            self.context = self.playwright_manager.chromium.launch_persistent_context(
                user_data_dir=str(self.config.browser_profile_dir),
                headless=self.config.browser_headless,
                user_agent=self.config.user_agent,
                viewport={"width": self.config.browser_width, "height": self.config.browser_height},
                locale=self.config.browser_locale,
                timezone_id=self.config.browser_timezone,
                args=launch_args,
            )
            self.context.set_default_timeout(self.config.default_timeout_ms)
            self.page = self.current_page(create=True)
        except Exception as exc:
            self.close_browser()
            raise RuntimeError(
                "Chromium exited during startup. Common causes in headed mode are: "
                "no usable desktop session for DISPLAY, missing XAUTHORITY/HOME in the "
                "systemd unit, or a stale/locked browser profile. If VNC is running, try "
                "setting HOME=/home/ubuntu and XAUTHORITY=/home/ubuntu/.Xauthority in the unit, "
                "or test with a fresh BROWSER_PROFILE_DIR."
            ) from exc

        self.save_state("running", message="Browser started")
        self.focus_page(self.page)
        log.info(
            "Browser started with persistent profile %s (headless=%s, build=%s)",
            self.config.browser_profile_dir,
            self.config.browser_headless,
            self.config.build_id,
        )

    def close_browser(self) -> None:
        try:
            if self.context:
                self.context.close()
        except Exception as exc:
            log.warning(f"Browser context close failed: {exc}")
        finally:
            self.context = None
            self.page = None

        try:
            if self.playwright_manager:
                self.playwright_manager.stop()
        except Exception as exc:
            log.warning(f"Playwright stop failed: {exc}")
        finally:
            self.playwright_manager = None

    def restart_browser(self) -> None:
        log.info("Restarting browser context...")
        self.close_browser()
        time.sleep(2)
        self.ensure_browser()

    def shutdown(self) -> None:
        self.save_state("stopped", message="Process stopped")
        self.close_browser()

    def wait_until_resumed(self, require_challenge_cleared: bool) -> None:
        while not self.stop_requested:
            self.poll_telegram_commands(timeout=self.config.telegram_poll_timeout)
            if not self.resume_requested:
                continue

            self.resume_requested = False
            page = self.current_page(create=False)
            if require_challenge_cleared and page:
                indicator = detect_bot_challenge(page)
                if indicator:
                    shot = self.capture_page("challenge_still_present")
                    self.notify(
                        "PRENOTAMI: Challenge Still Present",
                        "The challenge is still visible in the same browser session. "
                        "Solve it in VNC/noVNC, then send /resume again.",
                        photo_path=shot,
                    )
                    continue

            self.pause_reason = ""
            self.pause_requested = False
            self.save_state("running", message="Resumed by Telegram command")
            self.telegram.send_message("Checker resumed.")
            raise RestartLoop("Manual resume requested")

    def pause_for_manual_request(self, stage: str) -> None:
        self.pause_requested = False
        self.pause_reason = f"manual pause requested at {stage}"
        shot = self.capture_page(f"manual_pause_{stage}")
        self.save_state("paused_manual", stage=stage)
        self.notify(
            "PRENOTAMI: Checker Paused",
            f"The checker paused at {stage}. Send /resume when you want it to continue.",
            photo_path=shot,
        )
        self.wait_until_resumed(require_challenge_cleared=False)

    def pause_for_challenge(self, stage: str, indicator: str) -> None:
        self.pause_requested = False
        page = self.current_page(create=False)
        shot = self.capture_page(f"challenge_{stage}")
        self.pause_reason = f"anti-bot challenge at {stage}"
        self.save_state("paused_challenge", stage=stage, indicator=indicator)

        lines = [
            f"Anti-bot challenge detected during {stage}.",
            f"Indicator: {indicator}",
            f"Current URL: {page.url if page else '(unknown)'}",
            f"Headless: {self.config.browser_headless}",
            f"Display: {self.current_display() or '(not set)'}",
            f"Profile: {self.config.browser_profile_dir}",
            "The checker is now paused and will not keep retrying.",
            "Solve the challenge in the same browser session, then send /resume.",
        ]
        if self.config.browser_headless:
            lines.append(
                "This browser is headless, so there may be no visible window to solve. "
                "Run with BROWSER_HEADLESS=false inside a VNC/noVNC session."
            )
        if self.config.manual_solve_url:
            lines.append(f"Connect: {self.config.manual_solve_url}")
        if self.config.manual_solve_note:
            lines.append(f"Note: {self.config.manual_solve_note}")

        self.notify(
            "PRENOTAMI: Manual Challenge Solve Required",
            "\n".join(lines),
            photo_path=shot,
        )
        self.wait_until_resumed(require_challenge_cleared=True)

    def safe_point(self, stage: str) -> None:
        observation = self.observe(create=False, probe_timeout=300)
        if observation.page:
            indicator = detect_bot_challenge(observation.page)
            if indicator:
                self.pause_for_challenge(stage, indicator)

        self.poll_telegram_commands(timeout=0)
        if self.pause_requested:
            self.pause_for_manual_request(stage)

    def dismiss_ok_button(self, page) -> None:
        try:
            ok_btn = page.locator("button:has-text('OK'), a:has-text('OK')").first
            if ok_btn.is_visible(timeout=1500):
                ok_btn.click()
        except Exception:
            pass

    def handle_all_booked_state(self, page) -> None:
        log.info("No slots available - all booked.")
        self.dismiss_ok_button(page)

    def handle_autobook_result(self, result: str) -> None:
        log.info(f"Auto-book result: {result}")

        travel_info = (
            "--- TRAVEL DETAILS FOR BOOKING ---\n"
            "Name: Angli Liu\n"
            "DOB: May 27, 1991\n"
            "Citizenship: China\n"
            "Passport Key Info: Issued Feb 11, 2022; Expires Feb 10, 2032\n"
            "Employer: Meta Platforms, Inc. (Machine Learning Engineer)\n"
            "Trip Dates: May 22, 2026 - June 09, 2026\n"
            "Hotel: Hotel Nologo (Viale Sauli 5, 16121 Genoa, Italy)\n"
            "U.S. Status: H1B, pending I-485, Advance Parole\n"
            "---------------------------------\n\n"
        )

        if "FALSE_ALARM" in result.upper():
            log.info("False alarm detected. Continuing checks.")
            return

        self.notify(
            "PRENOTAMI: Schengen Visa Slot Detected & Booking Attempted!",
            f"A Schengen visa slot was detected at the Italian Consulate SF!\n\n"
            f"Auto-book result: {result}\n\n"
            f"IMPORTANT: Check https://prenotami.esteri.it/ in the same browser session to verify the booking.\n\n"
            f"{travel_info}"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"-- PrenotaMi Auto-Booker",
        )

        if "BOOKING CONFIRMED" in result.upper():
            mark_booked(self.config.booked_file, result)
            log.info("Booking confirmed. Stopping further checks.")
            self.stop_requested = True
        elif "BOOKING_MAYBE" in result.upper():
            log.info("Booking maybe confirmed. Will keep checking until strongly confirmed.")

    def maybe_switch_to_english(self, observation: BrowserObservation, context: CheckContext) -> bool:
        if observation.state not in {
            PAGE_STATE_AUTHENTICATED,
            PAGE_STATE_SERVICES,
            PAGE_STATE_BOOKING,
            PAGE_STATE_PRENOTAMI_OTHER,
        }:
            return False

        if observation.language == "en":
            return False

        attempt = context.record_attempt("switch_language")
        if observation.language == "it" and attempt > 3:
            raise RuntimeError(
                f"Failed to switch PrenotaMi to English after {attempt} attempts. "
                f"Current URL: {observation.url}"
            )

        switched = self.ensure_english_language(observation.page)
        if switched:
            log.info("Switched PrenotaMi page to English.")
            self.wait_for_observation(
                lambda current: current.language == "en",
                timeout=8000,
                probe_timeout=300,
            )
            return True

        if observation.language == "it":
            raise RuntimeError(f"Could not switch PrenotaMi page to English at {observation.url}")

        return False

    def advance_check_state(
        self,
        observation: BrowserObservation,
        context: CheckContext,
        *,
        allow_autobook: bool,
    ) -> tuple[str, str | None]:
        page = observation.page or self.current_page(create=True)
        if not page:
            raise RuntimeError("No active browser page is available.")

        if observation.state == PAGE_STATE_CHALLENGE:
            indicator = detect_bot_challenge(page) or observation.url
            self.pause_for_challenge("check:challenge", indicator)
            return "continue", None

        if observation.state == PAGE_STATE_ALL_BOOKED:
            self.handle_all_booked_state(page)
            return "done", "no_slots"

        if self.maybe_switch_to_english(observation, context):
            return "continue", None

        if observation.state == PAGE_STATE_UNKNOWN:
            attempt = context.record_attempt("goto_home")
            if attempt > 4:
                raise RuntimeError(
                    f"Unknown page state did not recover after {attempt} attempts. "
                    f"Current URL: {observation.url}"
                )
            log.info("Unknown page state at %s. Navigating to PrenotaMi homepage.", observation.url or "(blank)")
            page.goto("https://prenotami.esteri.it/", wait_until="domcontentloaded", timeout=60000)
            self.wait_for_observation_change(observation, timeout=10000, probe_timeout=300)
            return "continue", None

        if observation.state == PAGE_STATE_PRENOTAMI_OTHER:
            attempt = context.record_attempt("goto_services")
            if attempt > 4:
                raise RuntimeError(
                    f"Could not stabilize unknown PrenotaMi page after {attempt} attempts. "
                    f"Current URL: {observation.url}"
                )
            log.info("PrenotaMi page %s is not a terminal state. Navigating to services.", observation.url)
            page.goto("https://prenotami.esteri.it/Services", wait_until="domcontentloaded", timeout=30000)
            self.wait_for_observation_change(observation, timeout=10000, probe_timeout=300)
            return "continue", None

        if observation.state == PAGE_STATE_HOME_LOGGED_OUT:
            wait_for_page_ready(
                page,
                selectors=LOGIN_LINK_SELECTORS + LOGGED_IN_SELECTORS + ["body"],
                timeout=15000,
                settle_seconds=0.0,
            )
            refreshed = self.observe(create=True, probe_timeout=400)
            if refreshed.state != PAGE_STATE_HOME_LOGGED_OUT:
                return "continue", None

            attempt = context.record_attempt("click_login")
            if attempt > 4:
                raise RuntimeError(
                    f"Homepage login click did not move the browser after {attempt} attempts. "
                    f"Current URL: {refreshed.url}"
                )

            log.info("Clicking login from PrenotaMi homepage...")
            clicked, live_state, live_page = self.click_visible_on_current_page(
                "auth:click-login",
                LOGIN_LINK_SELECTORS,
                timeout=4000,
                allowed_states={PAGE_STATE_HOME_LOGGED_OUT},
            )
            if not clicked:
                if live_state != PAGE_STATE_HOME_LOGGED_OUT:
                    log.info("Login click skipped because page changed to %s (%s)", live_state, live_page.url)
                    return "continue", None
                shot = self.capture_page("homepage_not_ready")
                raise RuntimeError(f"Homepage loaded but login link was not found at {refreshed.url}. Screenshot: {shot}")

            self.wait_for_observation_change(refreshed, timeout=15000, probe_timeout=300)
            return "continue", None

        if observation.state == PAGE_STATE_SSO_LOGIN:
            attempt = context.record_attempt("submit_sso")
            if attempt > 5:
                raise RuntimeError(
                    f"SSO sign-in page did not transition after {attempt} submit attempts. "
                    f"Current URL: {observation.url}. Open pages: {self.open_pages_summary(probe_timeout=200)}"
                )

            log.info("Submitting SSO login...")
            username_selector, live_state, _live_page = self.fill_visible_on_current_page(
                "auth:fill-username",
                USERNAME_SELECTORS,
                self.config.email,
                timeout=1000,
                allowed_states={PAGE_STATE_SSO_LOGIN},
            )
            if not username_selector:
                if live_state != PAGE_STATE_SSO_LOGIN:
                    return "continue", None
                if self._recover_from_sso_state_change("username lookup"):
                    return "continue", None
                raise RuntimeError("Username input was not found on the login page.")

            password_selector, live_state, _live_page = self.fill_visible_on_current_page(
                "auth:fill-password",
                PASSWORD_SELECTORS,
                self.config.password,
                timeout=1000,
                allowed_states={PAGE_STATE_SSO_LOGIN},
            )
            if not password_selector:
                if live_state != PAGE_STATE_SSO_LOGIN:
                    return "continue", None
                if self._recover_from_sso_state_change("password lookup"):
                    return "continue", None
                raise RuntimeError("Password input was not found on the login page.")

            submit_clicked, live_state, _live_page = self.click_visible_on_current_page(
                "auth:click-submit",
                LOGIN_SUBMIT_SELECTORS,
                timeout=2000,
                allowed_states={PAGE_STATE_SSO_LOGIN},
            )
            if not submit_clicked:
                if live_state != PAGE_STATE_SSO_LOGIN:
                    return "continue", None
                if self._recover_from_sso_state_change("submit lookup"):
                    return "continue", None
                raise RuntimeError("Login submit button was not found.")

            log.info("Open pages after SSO submit: %s", self.open_pages_summary(probe_timeout=200))
            self.wait_for_observation_change(observation, timeout=15000, probe_timeout=300)
            return "continue", None

        if observation.state == PAGE_STATE_AUTHENTICATED:
            attempt = context.record_attempt("goto_services")
            if attempt > 4:
                raise RuntimeError(
                    f"Could not reach the services page after {attempt} attempts. "
                    f"Current URL: {observation.url}"
                )
            log.info("Navigating to services from %s...", observation.url)
            page.goto("https://prenotami.esteri.it/Services", wait_until="domcontentloaded", timeout=30000)
            self.wait_for_observation_change(observation, timeout=10000, probe_timeout=300)
            return "continue", None

        if observation.state == PAGE_STATE_SERVICES:
            wait_for_page_ready(page, selectors=SERVICES_PAGE_SELECTORS, timeout=15000, settle_seconds=0.0)
            refreshed = self.observe(create=True, probe_timeout=400)
            if refreshed.state != PAGE_STATE_SERVICES:
                return "continue", None
            if not is_services_page(refreshed.page):
                shot = self.capture_page("services_not_ready")
                raise RuntimeError(
                    f"Services page did not load the expected booking table. Current URL: {refreshed.url}. Screenshot: {shot}"
                )

            attempt = context.record_attempt("click_prenota")
            if attempt > 4:
                raise RuntimeError(
                    f"Schengen PRENOTA click did not leave the services page after {attempt} attempts. "
                    f"Current URL: {refreshed.url}. Open pages: {self.open_pages_summary(probe_timeout=200)}"
                )

            log.info("Clicking Schengen visa PRENOTA...")
            prenota_click = self.click_schengen_prenota(refreshed)
            if prenota_click == "state_changed":
                log.info("PRENOTA click skipped because the current page changed during the action.")
                return "continue", None
            if prenota_click != "clicked":
                raise RuntimeError("No Schengen PRENOTA button found")

            context.prenota_clicked = True
            return "continue", None

        if observation.state == PAGE_STATE_BOOKING:
            if not allow_autobook:
                return "done", "booking_page"
            if context.autobook_attempted:
                raise RuntimeError(
                    f"Booking page persisted after a booking attempt. Current URL: {observation.url}. "
                    f"Open pages: {self.open_pages_summary(probe_timeout=200)}"
                )
            context.autobook_attempted = True
            log.info("Slots detected. Attempting auto-book.")
            result = attempt_auto_book(page, log_dir=self.config.log_dir, checkpoint=self.safe_point)
            return "autobook", result

        raise RuntimeError(f"Unhandled check state {observation.state} at {observation.url}")

    def drive_state_machine(
        self,
        stage: str,
        *,
        allow_autobook: bool,
        terminal_predicate=None,
        max_transitions: int = 30,
    ) -> tuple[BrowserObservation, str | None]:
        context = CheckContext()
        last_observation = self.observe(create=True, probe_timeout=500)

        for transition in range(1, max_transitions + 1):
            observation = self.observe(create=True, probe_timeout=500)
            context.begin_transition(observation)
            self.save_state(
                "running",
                step=f"{stage}:{transition}:{observation.state}",
                language=observation.language,
                history=context.history[-8:],
            )
            self.safe_point(f"{stage}:{observation.state}")
            observation = self.observe(create=True, probe_timeout=500)
            context.begin_transition(observation)
            last_observation = observation

            if terminal_predicate and terminal_predicate(observation):
                return observation, None

            outcome, detail = self.advance_check_state(
                observation,
                context,
                allow_autobook=allow_autobook,
            )
            if outcome in {"done", "autobook"}:
                return observation, detail

        raise RuntimeError(
            f"{stage} state machine did not settle after {max_transitions} transitions. "
            f"Current state: {last_observation.state}. "
            f"Current URL: {last_observation.url}. "
            f"History: {context.history_summary()}. "
            f"Open pages: {self.open_pages_summary(probe_timeout=200)}"
        )

    def ensure_logged_in(self) -> None:
        observation, _ = self.drive_state_machine(
            "auth",
            allow_autobook=False,
            terminal_predicate=lambda current: (
                current.state in AUTHENTICATED_PAGE_STATES and current.language != "it"
            ),
            max_transitions=20,
        )
        log.info("Authenticated page detected: %s (%s)", observation.state, observation.url)

    def open_schengen_booking_page(self) -> object:
        observation, _ = self.drive_state_machine(
            "booking",
            allow_autobook=False,
            terminal_predicate=lambda current: (
                current.state == PAGE_STATE_ALL_BOOKED
                or (current.state == PAGE_STATE_BOOKING and current.language != "it")
            ),
            max_transitions=20,
        )
        if observation.state == PAGE_STATE_ALL_BOOKED:
            log.info("Booking flow reached the all-booked popup/result page.")
            self.handle_all_booked_state(observation.page)
        else:
            log.info("Booking page ready.")
        return observation.page

    def click_schengen_prenota(self, observation: BrowserObservation | None = None) -> str:
        if observation is None:
            observation = self.observe(create=True, probe_timeout=500)

        state, page = self.current_action_page("booking:click-prenota")
        if state != PAGE_STATE_SERVICES:
            return "state_changed"

        before = BrowserObservation(
            state=state,
            page=page,
            url=page.url,
            language=self.detect_page_language(page, probe_timeout=300),
        )

        schengen_clicked = page.evaluate(
            """() => {
                const rows = document.querySelectorAll('tr');
                for (const row of rows) {
                    const text = row.textContent.toLowerCase();
                    if (text.includes('schengen')) {
                        const allLinks = row.querySelectorAll('a, button');
                        for (const link of allLinks) {
                            const label = link.textContent.trim().toUpperCase();
                            if (label.includes('PRENOTA') || label.includes('BOOK')) {
                                link.click();
                                return true;
                            }
                        }
                    }
                }
                return false;
            }"""
        )
        if schengen_clicked:
            self.wait_for_observation_change(before, timeout=10000, probe_timeout=300)
            return "clicked"

        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

        state, page = self.current_action_page("booking:click-prenota-after-scroll")
        if state != PAGE_STATE_SERVICES:
            return "state_changed"

        clicked_after_scroll = page.evaluate(
            """() => {
                const rows = document.querySelectorAll('tr');
                for (const row of rows) {
                    const text = row.textContent.toLowerCase();
                    if (text.includes('schengen')) {
                        const allLinks = row.querySelectorAll('a, button');
                        for (const link of allLinks) {
                            const label = link.textContent.trim().toUpperCase();
                            if (label.includes('PRENOTA') || label.includes('BOOK')) {
                                link.click();
                                return true;
                            }
                        }
                    }
                }
                return false;
            }"""
        )
        if clicked_after_scroll:
            self.wait_for_observation_change(before, timeout=10000, probe_timeout=300)
            return "clicked"
        return "missing"

    def run_single_check(self) -> None:
        if is_already_booked(self.config.booked_file):
            log.info("Already booked. Skipping check.")
            self.stop_requested = True
            return

        self.ensure_browser()
        self.safe_point("before-check")

        log.info("Starting slot check...")
        _observation, detail = self.drive_state_machine(
            "check",
            allow_autobook=True,
            terminal_predicate=None,
            max_transitions=30,
        )

        if detail in {None, "no_slots", "booking_page"}:
            return

        self.handle_autobook_result(detail)

    def sleep_with_command_polling(self, seconds: int) -> None:
        end_time = time.time() + seconds
        while time.time() < end_time and not self.stop_requested:
            remaining = max(0, int(end_time - time.time()))
            self.poll_telegram_commands(timeout=min(self.config.telegram_poll_timeout, remaining))
            if self.pause_requested:
                self.pause_for_manual_request("between-checks")

    def announce_start(self) -> None:
        lines = [
            f"PrenotaMi checker started. Interval: {self.config.check_interval}s",
            f"Build: {self.config.build_id}",
            f"Headless: {self.config.browser_headless}",
            f"Display: {self.current_display() or '(not set)'}",
            f"Profile: {self.config.browser_profile_dir}",
            "Commands: /status /screenshot /pause /resume /help",
        ]
        if self.config.manual_solve_url:
            lines.append(f"Manual solve URL: {self.config.manual_solve_url}")
        self.telegram.send_message("\n".join(lines))

    def run_loop(self) -> None:
        self.ensure_browser()
        self.announce_start()

        while not self.stop_requested:
            self.poll_telegram_commands(timeout=0)
            if self.pause_requested:
                self.pause_for_manual_request("before-loop-check")

            try:
                self.run_single_check()
                self.consecutive_errors = 0
                self.check_count += 1
                self.save_state("running", step="sleeping")
            except RestartLoop as exc:
                log.info(str(exc))
                self.consecutive_errors = 0
                continue
            except Exception as exc:
                self.consecutive_errors += 1
                log.error(f"Error ({self.consecutive_errors}): {exc}")
                shot = self.capture_page("error")
                self.notify(
                    "PRENOTAMI: Checker Error",
                    f"Error #{self.consecutive_errors}: {exc}",
                    photo_path=shot,
                )
                if self.consecutive_errors >= 3:
                    self.notify(
                        "PRENOTAMI: Restarting Browser Session",
                        f"Restarting the browser after {self.consecutive_errors} consecutive errors.",
                    )
                    self.restart_browser()
                    self.consecutive_errors = 0

            if self.stop_requested:
                break

            log.info(f"Next check in {self.config.check_interval // 60} minutes...")
            self.sleep_with_command_polling(self.config.check_interval)

    def run_once(self) -> None:
        self.ensure_browser()
        self.run_single_check()
