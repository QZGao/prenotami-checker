#!/usr/bin/env python3
"""
PrenotaMi Schengen Visa Slot Checker + Auto-Booker

Monitors the Italian consulate's PrenotaMi appointment system for available
Schengen visa slots. When a slot is found, it automatically books the
earliest available appointment and sends a Telegram alert.
"""

import os
import sys
import logging
import time
from datetime import datetime
from pathlib import Path
from urllib import parse, request

# --- Configuration ---
def load_env():
    """Load .env file if it exists."""
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

load_env()

EMAIL = os.environ.get("PRENOTAMI_EMAIL", "")
PASSWORD = os.environ.get("PRENOTAMI_PASSWORD", "")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))
NOTIFY_COOLDOWN_SECONDS = int(os.environ.get("NOTIFY_COOLDOWN", "1800"))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

LOG_DIR = Path(__file__).parent / "logs"
COOLDOWN_FILE = Path(__file__).parent / ".last_notified"
BOOKED_FILE = Path(__file__).parent / ".booked"

LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "checker.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("prenotami")

NOTIFICATION_LOG = LOG_DIR / "notifications.log"


def chunk_message(text: str, max_length: int = 4000) -> list[str]:
    """Split long Telegram messages without breaking lines when possible."""
    chunks = []
    remaining = text

    while len(remaining) > max_length:
        split_at = remaining.rfind("\n", 0, max_length)
        if split_at <= 0:
            split_at = max_length
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")

    if remaining:
        chunks.append(remaining)

    return chunks


def send_telegram_notification(subject: str, body: str):
    """Send notification via Telegram bot + local log file."""
    clean_body = body.replace("\\\\n", "\n").replace("\\n", "\n")

    # 1. Write to notification log file (always works)
    try:
        with open(NOTIFICATION_LOG, "a") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"TIME: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"SUBJECT: {subject}\n")
            f.write(f"BODY:\n{clean_body}\n")
            f.write(f"{'='*60}\n")
        log.info(f"Notification logged to {NOTIFICATION_LOG}")
    except Exception as e:
        log.error(f"Failed to write notification log: {e}")

    # 2. Send Telegram bot notification
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram notification skipped: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.")
        return

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    full_message = f"{subject}\n\n{clean_body}"
    message_chunks = chunk_message(full_message)

    try:
        total = len(message_chunks)
        for index, chunk in enumerate(message_chunks, start=1):
            text = chunk if total == 1 else f"[{index}/{total}]\n{chunk}"
            payload = parse.urlencode({
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "disable_web_page_preview": "true",
            }).encode("utf-8")
            req = request.Request(api_url, data=payload, method="POST")
            with request.urlopen(req, timeout=15) as response:
                response.read()

        log.info(f"Telegram notification sent to chat {TELEGRAM_CHAT_ID}")
    except Exception as e:
        log.warning(f"Telegram notification failed: {e}")


def should_notify() -> bool:
    if not COOLDOWN_FILE.exists():
        return True
    try:
        last_notified = float(COOLDOWN_FILE.read_text().strip())
        return (time.time() - last_notified) > NOTIFY_COOLDOWN_SECONDS
    except (ValueError, OSError):
        return True


def mark_notified():
    COOLDOWN_FILE.write_text(str(time.time()))


def is_already_booked() -> bool:
    """Check if we've already successfully booked."""
    return BOOKED_FILE.exists()


def mark_booked(details: str):
    """Record that we successfully booked."""
    BOOKED_FILE.write_text(f"{datetime.now().isoformat()}\n{details}")


ALL_BOOKED_INDICATORS = [
    "All appointments for this service are currently booked",
    "tutti gli appuntamenti",
    "currently booked",
    "attualmente esauriti",
    "posti disponibili per il servizio scelto sono esauriti",
    "elevata richiesta",
    "sono esauriti",
]


def check_page_for_all_booked(page) -> bool:
    """Check if the current page shows an 'all booked' message."""
    try:
        content = page.content()
        return any(ind in content for ind in ALL_BOOKED_INDICATORS)
    except:
        return False


def attempt_auto_book(page) -> str:
    """
    When slots are available, attempt to book the earliest one.
    Returns a description of what happened.
    """
    log.info("🎉 ATTEMPTING AUTO-BOOK...")
    page.screenshot(path=str(LOG_DIR / "autobook_start.png"))

    try:
        # Wait for any calendar/form to load
        time.sleep(3)

        # CRITICAL: Re-check for 'all booked' popup that may have appeared late
        if check_page_for_all_booked(page):
            log.info("❌ FALSE ALARM: 'all booked' popup appeared after delay. Aborting auto-book.")
            page.screenshot(path=str(LOG_DIR / "autobook_false_alarm.png"))
            try:
                ok_btn = page.locator("button:has-text('OK'), a:has-text('OK')").first
                if ok_btn.is_visible(timeout=2000):
                    ok_btn.click()
            except:
                pass
            return "FALSE_ALARM: all booked popup appeared after initial check"

        page.screenshot(path=str(LOG_DIR / "autobook_after_wait.png"))

        # Look for a calendar with available dates
        # PrenotaMi typically shows a calendar where available dates are clickable
        page_content = page.content()

        # Try to find and click available date cells
        # Available dates usually have a specific class or are not grayed out
        available_date = page.evaluate("""() => {
            // Look for calendar cells that are clickable/available
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
                    // Click the first available date
                    els[0].click();
                    return `Clicked date via: ${sel} (found ${els.length} options)`;
                }
            }
            // Try any green/available-looking elements
            const allTds = document.querySelectorAll('td');
            for (const td of allTds) {
                const style = window.getComputedStyle(td);
                if (style.backgroundColor === 'rgb(0, 128, 0)' ||
                    style.backgroundColor === 'green' ||
                    td.classList.contains('available') ||
                    td.classList.contains('free') ||
                    (style.cursor === 'pointer' && !td.classList.contains('disabled'))) {
                    td.click();
                    return `Clicked available date cell`;
                }
            }
            return null;
        }""")

        if available_date:
            log.info(f"Date selection: {available_date}")
        else:
            log.info("No standard calendar found, looking for other booking UI elements...")

        time.sleep(2)
        page.screenshot(path=str(LOG_DIR / "autobook_after_date.png"))

        # Look for time slots
        time_slot = page.evaluate("""() => {
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
                    } else if (els[0].tagName === 'INPUT') {
                        els[0].click();
                        return `Clicked time radio: ${els[0].value}`;
                    } else {
                        els[0].click();
                        return `Clicked time slot: ${els[0].textContent.trim()}`;
                    }
                }
            }
            return null;
        }""")

        if time_slot:
            log.info(f"Time selection: {time_slot}")
        
        time.sleep(2)
        page.screenshot(path=str(LOG_DIR / "autobook_after_time.png"))

        # Auto-fill the EXACT PrenotaMi booking form fields
        # Use a robust label-finding approach: walk ALL text on the page near each element
        try:
            page.evaluate("""() => {
                // Helper: find the label text for a form element by searching parent, siblings, 
                // associated <label>, and nearby text nodes
                function getLabelText(el) {
                    // Check for <label for="id">
                    if (el.id) {
                        const lbl = document.querySelector('label[for="' + el.id + '"]');
                        if (lbl) return lbl.textContent.toLowerCase();
                    }
                    // Check closest label parent
                    const parentLabel = el.closest('label');
                    if (parentLabel) return parentLabel.textContent.toLowerCase();
                    // Check previous siblings and parent's previous siblings
                    let node = el.previousElementSibling;
                    while (node) {
                        const txt = node.textContent.trim();
                        if (txt.length > 0 && txt.length < 100) return txt.toLowerCase();
                        node = node.previousElementSibling;
                    }
                    // Check parent's text before this element
                    const parent = el.parentElement;
                    if (parent) {
                        const prevSib = parent.previousElementSibling;
                        if (prevSib) return prevSib.textContent.toLowerCase();
                    }
                    return '';
                }
                
                // Helper: select an option by text match
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
                
                // === DROPDOWNS ===
                const selects = document.querySelectorAll('select');
                for (const select of selects) {
                    const label = getLabelText(select);
                    const name = (select.name || select.id || '').toLowerCase();
                    const all = label + ' ' + name;
                    console.log('SELECT label:', all, 'options:', Array.from(select.options).map(o=>o.text));
                    
                    if (all.includes('tipo') && all.includes('prenot')) {
                        selectOption(select, 'singol');
                    } else if (all.includes('passaporto')) {
                        selectOption(select, 'ordinar');
                    } else if (all.includes('motivo') || all.includes('soggiorno')) {
                        selectOption(select, 'turism');
                    } else {
                        // Pick first non-empty option
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
                
                // === TEXT INPUTS & TEXTAREAS ===
                const inputs = document.querySelectorAll('input[type="text"], textarea');
                for (const input of inputs) {
                    const label = getLabelText(input);
                    const name = (input.name || input.id || input.placeholder || '').toLowerCase();
                    const all = label + ' ' + name;
                    console.log('INPUT label:', all);
                    
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
                
                // === CHECKBOXES (Privacy / Terms) ===
                const checkboxes = document.querySelectorAll('input[type="checkbox"]');
                for (const cb of checkboxes) {
                    if (!cb.checked) cb.click();
                }
            }""")
            log.info("Auto-fill completed for PrenotaMi form fields")
        except Exception as e:
            log.warning(f"Auto-fill warning: {e}")
        
        time.sleep(1)
        page.screenshot(path=str(LOG_DIR / "autobook_after_fill.png"))

        # Look for a submit/confirm/book button
        # Re-check AGAIN before submitting — popups can appear at any time
        if check_page_for_all_booked(page):
            log.info("❌ FALSE ALARM at submit stage: 'all booked' popup appeared.")
            page.screenshot(path=str(LOG_DIR / "autobook_false_alarm_submit.png"))
            return "FALSE_ALARM: all booked popup appeared before submit"

        submit_clicked = page.evaluate("""() => {
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
        }""")

        if submit_clicked:
            log.info(f"Submit: {submit_clicked}")
            time.sleep(5)
            page.screenshot(path=str(LOG_DIR / "autobook_after_submit.png"))

            # Check for 'all booked' popup AGAIN after submit
            if check_page_for_all_booked(page):
                log.info("❌ FALSE ALARM after submit: still no real slots.")
                page.screenshot(path=str(LOG_DIR / "autobook_false_alarm_after_submit.png"))
                return "FALSE_ALARM: all booked popup appeared after submit"

            # Check for REAL confirmation — must see specific booking success indicators
            final_content = page.content().lower()
            strong_confirm = ['prenotazione effettuata', 'booking confirmed',
                             'appuntamento confermato', 'successfully booked',
                             'conferma prenotazione', 'your appointment']
            if any(w in final_content for w in strong_confirm):
                page.screenshot(path=str(LOG_DIR / "BOOKING_CONFIRMED.png"))
                return f"BOOKING CONFIRMED! {submit_clicked}"
            
            # Weaker indicators — booking MAY have worked but needs verification
            weak_confirm = ['calendario', 'calendar', 'data e ora', 'date and time',
                           'i miei appuntamenti']
            if any(w in final_content for w in weak_confirm):
                page.screenshot(path=str(LOG_DIR / "BOOKING_MAYBE_CONFIRMED.png"))
                return f"BOOKING_MAYBE: Reached calendar/date page. {submit_clicked}"

        # If we got here, take a final screenshot and return what we know
        page.screenshot(path=str(LOG_DIR / "autobook_final_state.png"))
        
        # Get whatever text is on screen for the notification
        visible_text = page.evaluate("() => document.body.innerText.substring(0, 2000)")
        return f"Booking attempted. Page state: {visible_text[:500]}"

    except Exception as e:
        log.error(f"Auto-book error: {e}")
        try:
            page.screenshot(path=str(LOG_DIR / "autobook_error.png"))
        except:
            pass
        return f"Auto-book error: {e}"


def check_and_book():
    """Log into PrenotaMi, check for slots, and auto-book if available."""
    from playwright.sync_api import sync_playwright

    if is_already_booked():
        log.info("✅ Already booked! Skipping check. Delete .booked file to re-enable.")
        return

    if not EMAIL or not PASSWORD:
        log.error("PRENOTAMI_EMAIL and PRENOTAMI_PASSWORD must be set.")
        sys.exit(1)

    log.info("Starting slot check...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        
        # Handle Facebook password reuse and any other JS alerts automatically
        page.on("dialog", lambda dialog: dialog.accept())

        try:
            # Step 1: Navigate to PrenotaMi
            log.info("Navigating to PrenotaMi...")
            page.goto("https://prenotami.esteri.it/", timeout=60000)
            page.wait_for_load_state("networkidle", timeout=45000)
            time.sleep(2)

            # Step 2: Click login
            log.info("Clicking login...")
            for sel in ["a:has-text('EFFETTUARE IL LOGIN')", "a:has-text('LOG IN')",
                        "a:has-text('Log in')", "a[href*='Login']"]:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=2000):
                        el.click()
                        break
                except:
                    continue

            page.wait_for_load_state("networkidle", timeout=45000)
            time.sleep(3)

            # Step 3: Login
            log.info("Logging in...")
            for sel in ["input#UserName", "input[name='UserName']", "input[type='text']"]:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=3000):
                        el.fill(EMAIL)
                        break
                except:
                    continue

            for sel in ["input#Password", "input[name='Password']", "input[type='password']"]:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=3000):
                        el.fill(PASSWORD)
                        break
                except:
                    continue

            for sel in ["button:has-text('Next')", "button:has-text('Sign in')", "button[type='submit']"]:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=2000):
                        el.click()
                        break
                except:
                    continue

            page.wait_for_load_state("networkidle", timeout=45000)
            time.sleep(5)

            page_text = page.content().lower()
            if "login failure" in page_text or "login failed" in page_text:
                log.error("Login failed!")
                page.screenshot(path=str(LOG_DIR / "login_failed.png"))
                return

            log.info("Login successful!")

            # Step 4: Navigate to Services
            log.info("Navigating to services...")
            page.goto("https://prenotami.esteri.it/Services", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
            time.sleep(3)

            # Step 5: Click PRENOTA for Schengen visa
            log.info("Clicking Schengen visa PRENOTA...")
            schengen_clicked = page.evaluate("""() => {
                const rows = document.querySelectorAll('tr');
                for (const row of rows) {
                    const text = row.textContent.toLowerCase();
                    if (text.includes('schengen')) {
                        const allLinks = row.querySelectorAll('a, button');
                        for (const l of allLinks) {
                            const t = l.textContent.trim().toUpperCase();
                            if (t.includes('PRENOTA') || t.includes('BOOK')) {
                                l.click();
                                return true;
                            }
                        }
                    }
                }
                return false;
            }""")

            if not schengen_clicked:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(2)
                schengen_clicked = page.evaluate("""() => {
                    const rows = document.querySelectorAll('tr');
                    for (const row of rows) {
                        const text = row.textContent.toLowerCase();
                        if (text.includes('schengen')) {
                            const allLinks = row.querySelectorAll('a, button');
                            for (const l of allLinks) {
                                const t = l.textContent.trim().toUpperCase();
                                if (t.includes('PRENOTA') || t.includes('BOOK')) {
                                    l.click();
                                    return true;
                                }
                            }
                        }
                    }
                    return false;
                }""")

            if not schengen_clicked:
                log.warning("No Schengen PRENOTA button found")
                page.screenshot(path=str(LOG_DIR / "no_schengen.png"))
                return

            log.info("Clicked PRENOTA for Schengen visa")
            # Wait LONGER for any popup to fully render (was 3s, now 6s)
            time.sleep(6)

            # Step 6: Check result — do TWO checks with a gap
            page_content = page.content()
            page.screenshot(path=str(LOG_DIR / "after_prenota.png"))
            is_all_booked = any(ind in page_content for ind in ALL_BOOKED_INDICATORS)

            # If not detected yet, wait a bit more and check again
            if not is_all_booked:
                time.sleep(3)
                page_content2 = page.content()
                is_all_booked = any(ind in page_content2 for ind in ALL_BOOKED_INDICATORS)

            if is_all_booked:
                log.info("❌ No slots available - all booked.")
                try:
                    ok_btn = page.locator("button:has-text('OK'), a:has-text('OK')").first
                    if ok_btn.is_visible(timeout=2000):
                        ok_btn.click()
                except:
                    pass
            else:
                # 🎉 SLOTS AVAILABLE — AUTO-BOOK!
                log.info("🎉🎉🎉 SLOTS DETECTED! ATTEMPTING AUTO-BOOK! 🎉🎉🎉")
                result = attempt_auto_book(page)
                log.info(f"Auto-book result: {result}")

                # Send notification regardless
                travel_info = (
                    "--- TRAVEL DETAILS FOR BOOKING ---\\n"
                    "Name: Angli Liu\\n"
                    "DOB: May 27, 1991\\n"
                    "Citizenship: China\\n"
                    "Passport Key Info: Issued Feb 11, 2022; Expires Feb 10, 2032\\n"
                    "Employer: Meta Platforms, Inc. (Machine Learning Engineer)\\n"
                    "Trip Dates: May 22, 2026 - June 09, 2026\\n"
                    "Hotel: Hotel Nologo (Viale Sauli 5, 16121 Genoa, Italy)\\n"
                    "U.S. Status: H1B, pending I-485, Advance Parole\\n"
                    "---------------------------------\\n\\n"
                )
                # Only send a notification and mark booked for REAL detections, not false alarms
                if "FALSE_ALARM" in result.upper():
                    log.info("False alarm detected — NOT marking as booked, continuing checks.")
                else:
                    send_telegram_notification(
                        "PRENOTAMI: Schengen Visa Slot Detected & Booking Attempted!",
                        f"A Schengen visa slot was detected at the Italian Consulate SF!\\n\\n"
                        f"Auto-book result: {result}\\n\\n"
                        f"IMPORTANT: Please check https://prenotami.esteri.it/ immediately "
                        f"to verify the booking or grab the slot manually!\\n\\n"
                        f"{travel_info}"
                        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\\n\\n"
                        f"-- PrenotaMi Auto-Booker"
                    )
                    mark_notified()

                if "BOOKING CONFIRMED" in result.upper():
                    mark_booked(result)
                    log.info("✅ BOOKING CONFIRMED! Stopping further checks.")
                elif "BOOKING_MAYBE" in result.upper():
                    log.info("⚠️ BOOKING_MAYBE: Not confirmed yet. Will keep checking.")

        except Exception as e:
            log.error(f"Error: {e}")
            try:
                page.screenshot(path=str(LOG_DIR / "error.png"))
            except:
                pass
        finally:
            browser.close()

    log.info("Check complete.")


def run_loop():
    """Run the checker in a loop."""
    log.info(f"Starting auto-book loop (interval: {CHECK_INTERVAL}s = {CHECK_INTERVAL//60} min)...")
    while True:
        if is_already_booked():
            log.info("✅ Already booked! Exiting loop.")
            break
        check_and_book()
        log.info(f"Next check in {CHECK_INTERVAL // 60} minutes...")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="PrenotaMi Schengen Visa Auto-Booker")
    parser.add_argument("--loop", action="store_true", help="Run in continuous loop mode")
    parser.add_argument("--once", action="store_true", help="Run a single check (default)")
    args = parser.parse_args()

    if args.loop:
        run_loop()
    else:
        check_and_book()
