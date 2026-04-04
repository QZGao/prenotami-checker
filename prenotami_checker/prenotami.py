from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .exceptions import RestartLoop


log = logging.getLogger("prenotami")

ALL_BOOKED_INDICATORS = [
    "All appointments for this service are currently booked",
    "tutti gli appuntamenti",
    "currently booked",
    "attualmente esauriti",
    "posti disponibili per il servizio scelto sono esauriti",
    "elevata richiesta",
    "sono esauriti",
]

BOT_CHALLENGE_INDICATORS = [
    "validate.perfdrive.com",
]

LOGIN_LINK_SELECTORS = [
    "a:has-text('EFFETTUARE IL LOGIN')",
    "a:has-text('LOG IN')",
    "a:has-text('Log in')",
    "a[href*='Login']",
]

LOGGED_IN_SELECTORS = [
    "a[href*='Logout']",
    "a[href*='Services']",
    "text=I miei appuntamenti",
    "text=My appointments",
]

USERNAME_SELECTORS = [
    "input#UserName",
    "input[name='UserName']",
    "input#floatingLabelInput33",
    "input[type='text']",
]

PASSWORD_SELECTORS = [
    "input#Password",
    "input[name='Password']",
    "input#floatingLabelInput38",
    "input[type='password']",
]

LOGIN_SUBMIT_SELECTORS = [
    "button:has-text('Next')",
    "button:has-text('Sign in')",
    "button[type='submit']",
]

URL_STATE_PRENOTAMI = "prenotami"
URL_STATE_SSO = "sso"
URL_STATE_CHALLENGE = "challenge"
URL_STATE_UNKNOWN = "unknown"


def check_page_for_all_booked(page) -> bool:
    """Check if the current page shows an 'all booked' message."""
    try:
        content = page.content()
        return any(indicator in content for indicator in ALL_BOOKED_INDICATORS)
    except Exception:
        return False


def classify_page_url(url: str) -> str:
    """Classify the current browser URL into a small set of trusted route states."""
    if not url:
        return URL_STATE_UNKNOWN

    try:
        hostname = urlparse(url).netloc.lower()
    except Exception:
        return URL_STATE_UNKNOWN

    if hostname.startswith("www."):
        hostname = hostname[4:]

    if hostname == "prenotami.esteri.it":
        return URL_STATE_PRENOTAMI
    if hostname == "iam.esteri.it":
        return URL_STATE_SSO
    if hostname == "validate.perfdrive.com":
        return URL_STATE_CHALLENGE
    return URL_STATE_UNKNOWN


def detect_bot_challenge(page) -> str | None:
    """Return a short description when the page is a bot-challenge page."""
    try:
        url = page.url
    except Exception:
        return None

    if classify_page_url(url) == URL_STATE_CHALLENGE:
        for indicator in BOT_CHALLENGE_INDICATORS:
            if indicator in url.lower():
                return indicator
        return url
    return None


def wait_for_url_state(
    page,
    expected_states: list[str] | tuple[str, ...],
    timeout: int = 30000,
    settle_seconds: float = 1.0,
) -> str:
    """Wait until the page URL resolves to one of the expected route states."""
    deadline = time.time() + (timeout / 1000)
    seen_urls: list[str] = []

    while time.time() < deadline:
        try:
            current_url = page.url
            state = classify_page_url(current_url)
            if current_url and (not seen_urls or seen_urls[-1] != current_url):
                seen_urls.append(current_url)
                if len(seen_urls) > 5:
                    seen_urls = seen_urls[-5:]
            if state in expected_states:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=2000)
                except Exception:
                    pass
                time.sleep(settle_seconds)
                return state
        except Exception:
            pass
        time.sleep(0.5)

    trail = " -> ".join(seen_urls) if seen_urls else "(no navigable URL observed)"
    raise RuntimeError(
        f"Timed out waiting for URL state {list(expected_states)}. "
        f"Current URL: {getattr(page, 'url', '(unknown)')}. Recent URLs: {trail}"
    )


def wait_for_first_visible(page, selectors: list[str], timeout: int = 15000) -> str | None:
    """Return the first selector that becomes visible within the timeout."""
    for selector in selectors:
        try:
            page.locator(selector).first.wait_for(state="visible", timeout=timeout)
            return selector
        except Exception:
            continue
    return None


def click_first_visible(page, selectors: list[str], timeout: int = 2000) -> str | None:
    """Click the first visible selector from the provided list."""
    for selector in selectors:
        try:
            element = page.locator(selector).first
            if element.is_visible(timeout=timeout):
                element.click()
                return selector
        except Exception:
            continue
    return None


def wait_for_page_ready(
    page,
    selectors: list[str] | None = None,
    timeout: int = 20000,
    settle_seconds: float = 1.0,
) -> str | None:
    """
    Wait for DOM readiness instead of network-idle.
    PrenotaMi keeps background activity open often enough that network-idle is brittle.
    """
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception as exc:
        log.warning(f"DOM content load wait timed out on {page.url}: {exc}")

    matched_selector = None
    if selectors:
        matched_selector = wait_for_first_visible(page, selectors, timeout=timeout)
        if matched_selector:
            log.info(f"Page ready via selector: {matched_selector}")
        else:
            log.warning(f"None of the expected selectors became visible on {page.url}")
    else:
        try:
            page.locator("body").first.wait_for(state="attached", timeout=timeout)
        except Exception as exc:
            log.warning(f"Body did not attach on {page.url}: {exc}")

    time.sleep(settle_seconds)
    return matched_selector


def attempt_auto_book(
    page,
    log_dir: Path,
    checkpoint: Callable[[str], None] | None = None,
) -> str:
    """
    When slots are available, attempt to book the earliest one.
    Returns a description of what happened.
    """
    log.info("ATTEMPTING AUTO-BOOK...")
    page.screenshot(path=str(log_dir / "autobook_start.png"))

    try:
        if checkpoint:
            checkpoint("autobook:start")

        time.sleep(3)
        if checkpoint:
            checkpoint("autobook:after-wait")

        if check_page_for_all_booked(page):
            log.info("FALSE ALARM: 'all booked' popup appeared after delay. Aborting auto-book.")
            page.screenshot(path=str(log_dir / "autobook_false_alarm.png"))
            try:
                ok_btn = page.locator("button:has-text('OK'), a:has-text('OK')").first
                if ok_btn.is_visible(timeout=2000):
                    ok_btn.click()
            except Exception:
                pass
            return "FALSE_ALARM: all booked popup appeared after initial check"

        page.screenshot(path=str(log_dir / "autobook_after_wait.png"))

        available_date = page.evaluate(
            """() => {
                const selectors = [
                    'td.day:not(.disabled):not(.old):not(.new)',
                    'td.active',
                    'td[data-action="selectDay"]:not(.disabled)',
                    '.datepicker td:not(.disabled):not(.old)',
                    'a.ui-state-default:not(.ui-state-disabled)',
                    '.fc-day:not(.fc-day-disabled)',
                    'td.giorno_disponibile',
                    'td.disponibile',
                    'td[style*="cursor: pointer"]',
                    '.day-content:not(.disabled)',
                ];
                for (const sel of selectors) {
                    const els = document.querySelectorAll(sel);
                    if (els.length > 0) {
                        els[0].click();
                        return `Clicked date via: ${sel} (found ${els.length} options)`;
                    }
                }
                const allTds = document.querySelectorAll('td');
                for (const td of allTds) {
                    const style = window.getComputedStyle(td);
                    if (style.backgroundColor === 'rgb(0, 128, 0)' ||
                        style.backgroundColor === 'green' ||
                        td.classList.contains('available') ||
                        td.classList.contains('free') ||
                        (style.cursor === 'pointer' && !td.classList.contains('disabled'))) {
                        td.click();
                        return 'Clicked available date cell';
                    }
                }
                return null;
            }"""
        )

        if available_date:
            log.info(f"Date selection: {available_date}")
        else:
            log.info("No standard calendar found, looking for other booking UI elements...")

        time.sleep(2)
        if checkpoint:
            checkpoint("autobook:after-date")
        page.screenshot(path=str(log_dir / "autobook_after_date.png"))

        time_slot = page.evaluate(
            """() => {
                const selectors = [
                    'select[name*="time"] option:not(:disabled):not([value=""])',
                    'select[name*="ora"] option:not(:disabled):not([value=""])',
                    '.time-slot:not(.disabled)',
                    'input[type="radio"][name*="time"]',
                    'input[type="radio"][name*="ora"]',
                    'select option:not(:first-child)',
                ];
                for (const sel of selectors) {
                    const els = document.querySelectorAll(sel);
                    if (els.length > 0) {
                        if (els[0].tagName === 'OPTION') {
                            els[0].selected = true;
                            els[0].parentElement.dispatchEvent(new Event('change', {bubbles: true}));
                            return `Selected time: ${els[0].textContent.trim()}`;
                        }
                        if (els[0].tagName === 'INPUT') {
                            els[0].click();
                            return `Clicked time radio: ${els[0].value}`;
                        }
                        els[0].click();
                        return `Clicked time slot: ${els[0].textContent.trim()}`;
                    }
                }
                return null;
            }"""
        )

        if time_slot:
            log.info(f"Time selection: {time_slot}")

        time.sleep(2)
        if checkpoint:
            checkpoint("autobook:after-time")
        page.screenshot(path=str(log_dir / "autobook_after_time.png"))

        try:
            page.evaluate(
                """() => {
                    function getLabelText(el) {
                        if (el.id) {
                            const lbl = document.querySelector('label[for="' + el.id + '"]');
                            if (lbl) return lbl.textContent.toLowerCase();
                        }
                        const parentLabel = el.closest('label');
                        if (parentLabel) return parentLabel.textContent.toLowerCase();
                        let node = el.previousElementSibling;
                        while (node) {
                            const txt = node.textContent.trim();
                            if (txt.length > 0 && txt.length < 100) return txt.toLowerCase();
                            node = node.previousElementSibling;
                        }
                        const parent = el.parentElement;
                        if (parent) {
                            const prevSib = parent.previousElementSibling;
                            if (prevSib) return prevSib.textContent.toLowerCase();
                        }
                        return '';
                    }

                    function selectOption(sel, textMatch) {
                        for (let i = 0; i < sel.options.length; i++) {
                            if (sel.options[i].text.toLowerCase().includes(textMatch)) {
                                sel.value = sel.options[i].value;
                                sel.dispatchEvent(new Event('change', {bubbles: true}));
                                return true;
                            }
                        }
                        return false;
                    }

                    const selects = document.querySelectorAll('select');
                    for (const select of selects) {
                        const label = getLabelText(select);
                        const name = (select.name || select.id || '').toLowerCase();
                        const all = label + ' ' + name;

                        if (all.includes('tipo') && all.includes('prenot')) {
                            selectOption(select, 'singol');
                        } else if (all.includes('passaporto')) {
                            selectOption(select, 'ordinar');
                        } else if (all.includes('motivo') || all.includes('soggiorno')) {
                            selectOption(select, 'turism');
                        } else {
                            for (let i = 0; i < select.options.length; i++) {
                                const v = select.options[i].value;
                                if (v && v !== '0' && !select.options[i].text.toLowerCase().includes('selezion')) {
                                    select.value = v;
                                    select.dispatchEvent(new Event('change', {bubbles: true}));
                                    break;
                                }
                            }
                        }
                    }

                    const inputs = document.querySelectorAll('input[type="text"], textarea');
                    for (const input of inputs) {
                        const label = getLabelText(input);
                        const name = (input.name || input.id || input.placeholder || '').toLowerCase();
                        const all = label + ' ' + name;

                        if (all.includes('indirizzo') || all.includes('residenza') || all.includes('address')) {
                            input.value = '61 McLellan Ave, San Mateo, CA 94403, USA';
                        } else if (all.includes('note') || input.tagName === 'TEXTAREA') {
                            input.value = 'Schengen visa for tourism. Trip: May 22 - June 9, 2026. Hotel Nologo, Genoa.';
                        } else if (all.includes('nome') && !all.includes('cognome')) {
                            input.value = 'Angli';
                        } else if (all.includes('cognome') || all.includes('surname')) {
                            input.value = 'Liu';
                        }

                        input.dispatchEvent(new Event('input', {bubbles: true}));
                        input.dispatchEvent(new Event('change', {bubbles: true}));
                    }

                    const checkboxes = document.querySelectorAll('input[type="checkbox"]');
                    for (const cb of checkboxes) {
                        if (!cb.checked) cb.click();
                    }
                }"""
            )
            log.info("Auto-fill completed for PrenotaMi form fields")
        except Exception as exc:
            log.warning(f"Auto-fill warning: {exc}")

        time.sleep(1)
        if checkpoint:
            checkpoint("autobook:after-fill")
        page.screenshot(path=str(log_dir / "autobook_after_fill.png"))

        if check_page_for_all_booked(page):
            log.info("FALSE ALARM at submit stage: 'all booked' popup appeared.")
            page.screenshot(path=str(log_dir / "autobook_false_alarm_submit.png"))
            return "FALSE_ALARM: all booked popup appeared before submit"

        submit_clicked = page.evaluate(
            """() => {
                const selectors = [
                    'button:not([disabled])',
                    'input[type="submit"]:not([disabled])',
                    'a.btn:not(.disabled)',
                ];
                const keywords = ['confirm', 'submit', 'book', 'prenota', 'conferma',
                                 'save', 'salva', 'avanti', 'next', 'proceed', 'invia'];
                for (const sel of selectors) {
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        const text = el.textContent.trim().toLowerCase();
                        for (const kw of keywords) {
                            if (text.includes(kw)) {
                                el.click();
                                return `Clicked: ${el.textContent.trim()}`;
                            }
                        }
                    }
                }
                return null;
            }"""
        )

        if submit_clicked:
            log.info(f"Submit: {submit_clicked}")
            time.sleep(5)
            if checkpoint:
                checkpoint("autobook:after-submit")
            page.screenshot(path=str(log_dir / "autobook_after_submit.png"))

            if check_page_for_all_booked(page):
                log.info("FALSE ALARM after submit: still no real slots.")
                page.screenshot(path=str(log_dir / "autobook_false_alarm_after_submit.png"))
                return "FALSE_ALARM: all booked popup appeared after submit"

            final_content = page.content().lower()
            strong_confirm = [
                "prenotazione effettuata",
                "booking confirmed",
                "appuntamento confermato",
                "successfully booked",
                "conferma prenotazione",
                "your appointment",
            ]
            if any(word in final_content for word in strong_confirm):
                page.screenshot(path=str(log_dir / "BOOKING_CONFIRMED.png"))
                return f"BOOKING CONFIRMED! {submit_clicked}"

            weak_confirm = [
                "calendario",
                "calendar",
                "data e ora",
                "date and time",
                "i miei appuntamenti",
            ]
            if any(word in final_content for word in weak_confirm):
                page.screenshot(path=str(log_dir / "BOOKING_MAYBE_CONFIRMED.png"))
                return f"BOOKING_MAYBE: Reached calendar/date page. {submit_clicked}"

        page.screenshot(path=str(log_dir / "autobook_final_state.png"))
        visible_text = page.evaluate("() => document.body.innerText.substring(0, 2000)")
        return f"Booking attempted. Page state: {visible_text[:500]}"

    except RestartLoop:
        raise
    except Exception as exc:
        log.error(f"Auto-book error: {exc}")
        try:
            page.screenshot(path=str(log_dir / "autobook_error.png"))
        except Exception:
            pass
        return f"Auto-book error: {exc}"
