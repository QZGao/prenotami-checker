from __future__ import annotations

import json
import logging
import os
import signal
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from .config import Config
from .exceptions import RestartLoop
from .prenotami import (
    ALL_BOOKED_INDICATORS,
    LOGIN_LINK_SELECTORS,
    LOGIN_SUBMIT_SELECTORS,
    LOGGED_IN_SELECTORS,
    PASSWORD_SELECTORS,
    USERNAME_SELECTORS,
    URL_STATE_CHALLENGE,
    URL_STATE_PRENOTAMI,
    URL_STATE_SSO,
    attempt_auto_book,
    click_first_visible,
    detect_bot_challenge,
    wait_for_first_visible,
    wait_for_page_ready,
    wait_for_url_state,
)
from .telegram_api import TelegramClient, write_notification_log


log = logging.getLogger("prenotami")


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

    def current_page(self, create: bool = True):
        if self.page and not self.page.is_closed():
            return self.page

        if not self.context:
            return None

        pages = [page for page in self.context.pages if not page.is_closed()]
        if pages:
            self.page = pages[-1]
        elif create:
            self.page = self.context.new_page()
            self.page.on("dialog", lambda dialog: dialog.accept())
        else:
            self.page = None
        return self.page

    def save_state(self, mode: str, **extra: object) -> None:
        page = self.current_page(create=False)
        state = {
            "mode": mode,
            "pause_reason": self.pause_reason,
            "check_count": self.check_count,
            "headless": self.config.browser_headless,
            "display": self.current_display(),
            "profile_dir": str(self.config.browser_profile_dir),
            "url": page.url if page else "",
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
            self.page.on("dialog", lambda dialog: dialog.accept())
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
        log.info(
            "Browser started with persistent profile %s (headless=%s)",
            self.config.browser_profile_dir,
            self.config.browser_headless,
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
        page = self.current_page(create=False)
        if page:
            indicator = detect_bot_challenge(page)
            if indicator:
                self.pause_for_challenge(stage, indicator)

        self.poll_telegram_commands(timeout=0)
        if self.pause_requested:
            self.pause_for_manual_request(stage)

    def ensure_logged_in(self) -> None:
        page = self.current_page(create=True)
        self.save_state("running", step="homepage")

        log.info("Navigating to PrenotaMi...")
        page.goto("https://prenotami.esteri.it/", wait_until="domcontentloaded", timeout=60000)
        route = wait_for_url_state(
            page,
            expected_states=[URL_STATE_PRENOTAMI, URL_STATE_SSO, URL_STATE_CHALLENGE],
            timeout=120000,
            settle_seconds=2,
        )
        self.safe_point(f"landing:{route}")

        if route == URL_STATE_PRENOTAMI:
            wait_for_page_ready(
                page,
                selectors=LOGIN_LINK_SELECTORS + LOGGED_IN_SELECTORS + ["body"],
                timeout=20000,
                settle_seconds=2,
            )

            if wait_for_first_visible(page, LOGGED_IN_SELECTORS, timeout=1500):
                log.info("Existing authenticated session detected.")
                return

            log.info("Clicking login from PrenotaMi homepage...")
            clicked = click_first_visible(page, LOGIN_LINK_SELECTORS, timeout=4000)
            if not clicked:
                shot = self.capture_page("homepage_not_ready")
                raise RuntimeError(f"Homepage loaded but login link was not found at {page.url}. Screenshot: {shot}")

            route = wait_for_url_state(
                page,
                expected_states=[URL_STATE_SSO, URL_STATE_CHALLENGE],
                timeout=30000,
                settle_seconds=2,
            )
            self.safe_point(f"after-login-click:{route}")

        if route != URL_STATE_SSO:
            raise RuntimeError(f"Expected SSO login page but reached {page.url}")

        wait_for_page_ready(page, selectors=USERNAME_SELECTORS + PASSWORD_SELECTORS + ["body"], timeout=20000, settle_seconds=2)
        self.safe_point("sso-login-page")

        username_filled = False
        for selector in USERNAME_SELECTORS:
            try:
                element = page.locator(selector).first
                if element.is_visible(timeout=3000):
                    element.fill(self.config.email)
                    username_filled = True
                    break
            except Exception:
                continue
        if not username_filled:
            raise RuntimeError("Username input was not found on the login page.")

        password_filled = False
        for selector in PASSWORD_SELECTORS:
            try:
                element = page.locator(selector).first
                if element.is_visible(timeout=3000):
                    element.fill(self.config.password)
                    password_filled = True
                    break
            except Exception:
                continue
        if not password_filled:
            raise RuntimeError("Password input was not found on the login page.")

        submit_clicked = click_first_visible(page, LOGIN_SUBMIT_SELECTORS, timeout=2000)
        if not submit_clicked:
            raise RuntimeError("Login submit button was not found.")

        try:
            route = wait_for_url_state(
                page,
                expected_states=[URL_STATE_PRENOTAMI, URL_STATE_CHALLENGE],
                timeout=120000,
                settle_seconds=3,
            )
        except RuntimeError:
            current_url = page.url
            shot = self.capture_page("post_login_timeout")
            raise RuntimeError(
                "Login submit did not reach PrenotaMi or the challenge page within 120s. "
                f"Current URL: {current_url}. Screenshot: {shot}"
            )
        self.safe_point(f"post-login:{route}")

        if route == URL_STATE_PRENOTAMI:
            wait_for_page_ready(page, selectors=LOGGED_IN_SELECTORS + ["body"], timeout=20000, settle_seconds=2)
            log.info("Login reached PrenotaMi.")
            return

        raise RuntimeError(f"Login did not reach a usable PrenotaMi state. Current URL: {page.url}")

    def click_schengen_prenota(self) -> bool:
        page = self.current_page(create=True)
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
            return True

        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(2)

        return bool(
            page.evaluate(
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
        )

    def run_single_check(self) -> None:
        if is_already_booked(self.config.booked_file):
            log.info("Already booked. Skipping check.")
            self.stop_requested = True
            return

        self.ensure_browser()
        self.safe_point("before-check")

        log.info("Starting slot check...")
        self.ensure_logged_in()
        self.safe_point("after-login")

        page = self.current_page(create=True)
        self.save_state("running", step="services")

        log.info("Navigating to services...")
        page.goto("https://prenotami.esteri.it/Services", wait_until="domcontentloaded", timeout=30000)
        route = wait_for_url_state(
            page,
            expected_states=[URL_STATE_PRENOTAMI, URL_STATE_SSO, URL_STATE_CHALLENGE],
            timeout=60000,
            settle_seconds=2,
        )
        self.safe_point(f"services:{route}")
        if route != URL_STATE_PRENOTAMI:
            raise RuntimeError(f"Services navigation did not stay on PrenotaMi. Current URL: {page.url}")
        wait_for_page_ready(page, selectors=["#advanced", "tr", "table", "text=Schengen", "body"], timeout=20000, settle_seconds=2)

        log.info("Clicking Schengen visa PRENOTA...")
        if not self.click_schengen_prenota():
            raise RuntimeError("No Schengen PRENOTA button found")

        log.info("Clicked PRENOTA for Schengen visa")
        time.sleep(6)
        self.safe_point("after-prenota")

        page_content = page.content()
        page.screenshot(path=str(self.config.log_dir / "after_prenota.png"))
        is_all_booked = any(indicator in page_content for indicator in ALL_BOOKED_INDICATORS)

        if not is_all_booked:
            time.sleep(3)
            self.safe_point("after-prenota-second-check")
            page_content = page.content()
            is_all_booked = any(indicator in page_content for indicator in ALL_BOOKED_INDICATORS)

        if is_all_booked:
            log.info("No slots available - all booked.")
            try:
                ok_btn = page.locator("button:has-text('OK'), a:has-text('OK')").first
                if ok_btn.is_visible(timeout=2000):
                    ok_btn.click()
            except Exception:
                pass
            return

        log.info("Slots detected. Attempting auto-book.")
        result = attempt_auto_book(page, log_dir=self.config.log_dir, checkpoint=self.safe_point)
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
