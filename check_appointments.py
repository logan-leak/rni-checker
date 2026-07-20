#!/usr/bin/env python3
"""
Breda RNI appointment checker.

What this script does:
- Opens the Breda RNI booking page with Playwright.
- Navigates to August 2026.
- Detects which days are available.
- Sends an email when new relevant openings appear.
- Supports --test-email to verify SMTP without needing Playwright.

Design goals:
- Robust enough for GitHub Actions.
- Safe SMTP port handling.
- Separate test-email mode so you can confirm email delivery independently.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path
from typing import Iterable, Optional

BOOKING_URL = "https://breda.mijnafspraakmaken.nl/?lang=en&link=6adf&product=45"

TARGET_YEAR = 2026
TARGET_MONTH = 8  # August
PREFERRED_DAY = 19
EARLIEST_RELEVANT_DAY = 18

STATE_FILE = Path(__file__).parent / "state.json"
DEBUG_SCREENSHOT = Path(__file__).parent / "debug_screenshot.png"
DEBUG_HTML = Path(__file__).parent / "debug_page.html"

MONTH_NAMES_EN = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
MONTH_NAMES_NL = [
    "januari", "februari", "maart", "april", "mei", "juni",
    "juli", "augustus", "september", "oktober", "november", "december",
]

MONTH_NAME_TO_NUM = {name: i + 1 for i, name in enumerate(MONTH_NAMES_EN)}
MONTH_NAME_TO_NUM.update({name: i + 1 for i, name in enumerate(MONTH_NAMES_NL)})


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"known_available_days": [], "last_failure_notified": False}

    try:
        data = json.loads(STATE_FILE.read_text())
        if not isinstance(data, dict):
            raise ValueError("state.json is not a JSON object")
        data.setdefault("known_available_days", [])
        data.setdefault("last_failure_notified", False)
        return data
    except Exception:
        return {"known_available_days": [], "last_failure_notified": False}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def _smtp_port() -> int:
    raw = (os.getenv("SMTP_PORT") or "587").strip()
    return int(raw)


def send_email(subject: str, body: str) -> None:
    smtp_host = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = _smtp_port()
    from_addr = os.environ["EMAIL_FROM"]
    password = os.environ["EMAIL_PASSWORD"]
    to_addr = os.environ["EMAIL_TO"]

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(from_addr, password)
        server.sendmail(from_addr, [to_addr], msg.as_string())


def send_test_email() -> None:
    send_email(
        "RNI checker test email",
        "This is a test email from the Breda RNI appointment checker.\n\n"
        "If you received this, SMTP is working.",
    )


# ---------------------------------------------------------------------------
# Playwright helpers
# ---------------------------------------------------------------------------
def _month_year_pattern() -> re.Pattern[str]:
    month_alt = "|".join(
        [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
            "Januari", "Februari", "Maart", "April", "Mei", "Juni",
            "Juli", "Augustus", "September", "Oktober", "November", "December",
        ]
    )
    return re.compile(rf"(?im)^({month_alt})\s+(\d{{4}})$")


def dismiss_cookie_banner(page) -> None:
    for name in [
        "Accept",
        "Akkoord",
        "Alles accepteren",
        "Toestaan",
        "OK",
        "Accepteren",
    ]:
        try:
            btn = page.get_by_role("button", name=name, exact=False)
            if btn.count() > 0:
                btn.first.click(timeout=2000)
                page.wait_for_timeout(500)
                return
        except Exception:
            pass


def open_calendar_if_needed(page) -> None:
    try:
        opener = page.get_by_text("Click here to open the calendar", exact=False)
        if opener.count() > 0 and opener.first.is_visible():
            opener.first.click(timeout=3000)
            page.wait_for_timeout(800)
    except Exception:
        pass


def click_through_intro_steps(page) -> None:
    for text in ["Next", "Continue", "Volgende", "Doorgaan", "Verder"]:
        try:
            btn = page.get_by_role("button", name=text, exact=False)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click(timeout=2000)
                page.wait_for_timeout(800)
        except Exception:
            pass


def get_visible_month_year(page) -> Optional[tuple[int, int]]:
    """
    Look for the visible calendar heading line like:
    'July 2026'
    This is safer than scanning the whole page for any month name.
    """
    try:
        body_text = page.locator("body").inner_text()
    except Exception:
        return None

    pattern = _month_year_pattern()
    for line in body_text.splitlines():
        line = line.strip()
        m = pattern.match(line)
        if not m:
            continue
        month_name = m.group(1).lower()
        year = int(m.group(2))
        month_num = MONTH_NAME_TO_NUM.get(month_name)
        if month_num:
            return month_num, year
    return None


def _select_month_year_if_present(page, target_month: int, target_year: int) -> bool:
    """
    Some widgets expose native <select> controls. If present, use them.
    Returns True if the widget appears to be on the target month/year.
    """
    try:
        selects = page.locator("select")
        if selects.count() < 2:
            return False

        target_month_name_en = MONTH_NAMES_EN[target_month - 1].title()
        target_month_name_nl = MONTH_NAMES_NL[target_month - 1].title()

        month_select = selects.nth(0)
        year_select = selects.nth(1)

        for month_label in (target_month_name_en, target_month_name_nl):
            try:
                month_select.select_option(label=month_label)
                year_select.select_option(label=str(target_year))
                page.wait_for_timeout(1000)
                if get_visible_month_year(page) == (target_month, target_year):
                    return True
            except Exception:
                pass
    except Exception:
        pass

    return False


def navigate_to_target_month(page, target_month: int, target_year: int, max_clicks: int = 24) -> bool:
    """
    First try native month/year selects. If that fails, click the real 'Next month'
    button until the visible heading matches the target month/year.
    """
    if get_visible_month_year(page) == (target_month, target_year):
        return True

    if _select_month_year_if_present(page, target_month, target_year):
        return True

    next_button_candidates = [
        page.get_by_role("button", name="Next month", exact=False),
        page.get_by_role("button", name="Volgende maand", exact=False),
        page.locator('button:has-text("Next month")'),
        page.locator('button:has-text("Volgende maand")'),
        page.locator('button[aria-label*="next" i]'),
        page.locator('button[aria-label*="volgende" i]'),
        page.locator('a[aria-label*="next" i]'),
        page.locator('a[aria-label*="volgende" i]'),
        page.locator('button:has-text(">")'),
    ]

    for _ in range(max_clicks):
        if get_visible_month_year(page) == (target_month, target_year):
            return True

        clicked = False
        for candidate in next_button_candidates:
            try:
                if candidate.count() > 0 and candidate.first.is_visible():
                    candidate.first.click(timeout=2500)
                    page.wait_for_timeout(900)
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            break

    return get_visible_month_year(page) == (target_month, target_year)


def extract_available_days(page) -> list[int]:
    """
    Return available day numbers from the currently visible calendar.
    This uses several text/ARIA heuristics because the widget is not a standard API.
    """
    available: set[int] = set()

    selectors = [
        "button",
        "a",
        "td",
        '[role="button"]',
        '[role="option"]',
        "[aria-label]",
        "[title]",
    ]

    seen = set()

    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = loc.count()
        except Exception:
            continue

        for i in range(count):
            el = loc.nth(i)
            try:
                if not el.is_visible():
                    continue
            except Exception:
                continue

            try:
                text = el.inner_text().strip()
            except Exception:
                text = ""

            try:
                aria = (el.get_attribute("aria-label") or "").strip()
            except Exception:
                aria = ""

            try:
                title = (el.get_attribute("title") or "").strip()
            except Exception:
                title = ""

            payload = " ".join(part for part in [aria, title, text] if part).strip()
            if not payload:
                continue

            key = payload[:200]
            if key in seen:
                continue
            seen.add(key)

            lower = payload.lower()
            if "date is available" not in lower:
                continue

            # Parse the first 1-31 number we can find in the cell label/text.
            nums = re.findall(r"\b(\d{1,2})\b", payload)
            for n in nums:
                day = int(n)
                if 1 <= day <= 31:
                    available.add(day)
                    break

    return sorted(available)


def check_appointments() -> list[int]:
    """
    Returns a sorted list of available day numbers for the target month.
    Raises RuntimeError if the page structure cannot be understood.
    """
    from playwright.sync_api import sync_playwright  # local import so --test-email works without Playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            page.goto(BOOKING_URL, wait_until="networkidle", timeout=60000)
            dismiss_cookie_banner(page)
            open_calendar_if_needed(page)
            click_through_intro_steps(page)
            page.wait_for_timeout(1500)

            reached = navigate_to_target_month(page, TARGET_MONTH, TARGET_YEAR)

            if not reached:
                page.screenshot(path=str(DEBUG_SCREENSHOT), full_page=True)
                DEBUG_HTML.write_text(page.content(), encoding="utf-8")
                raise RuntimeError(
                    "Could not navigate to the target month. "
                    "Saved debug_screenshot.png and debug_page.html for troubleshooting."
                )

            available_days = extract_available_days(page)
            return available_days
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Breda RNI appointment checker")
    parser.add_argument(
        "--test-email",
        action="store_true",
        help="Send a test email and exit without running the scraper",
    )
    args = parser.parse_args()

    if args.test_email:
        send_test_email()
        print("Test email sent successfully.")
        return 0

    state = load_state()

    try:
        available_days = check_appointments()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)

        if not state.get("last_failure_notified"):
            try:
                send_email(
                    "RNI checker: script needs attention",
                    "The Breda RNI appointment checker hit an error and could not "
                    f"read the calendar:\n\n{exc}\n\n"
                    "Check the GitHub Actions run logs and the debug artifacts "
                    "(debug_screenshot.png / debug_page.html) to see what changed.",
                )
                state["last_failure_notified"] = True
                save_state(state)
            except Exception as email_exc:
                print(f"Also failed to send failure email: {email_exc}", file=sys.stderr)

        return 1

    if state.get("last_failure_notified"):
        state["last_failure_notified"] = False

    relevant_days = [d for d in available_days if d >= EARLIEST_RELEVANT_DAY]
    known = set(state.get("known_available_days", []))
    new_days = sorted(set(relevant_days) - known)

    print(f"Available August {TARGET_YEAR} days found: {available_days}")
    print(f"New (not previously seen) relevant days: {new_days}")

    if new_days:
        preferred_hit = PREFERRED_DAY in new_days
        subject = "🎉 RNI Breda: NEW appointment slot(s) open in August!"
        if preferred_hit:
            subject = "🎉🎉 RNI Breda: August 19 is OPEN — book now!"

        lines = [
            f"New availability found on the Breda RNI booking page for August {TARGET_YEAR}:",
            "",
        ]
        for d in new_days:
            marker = "  <-- your preferred date!" if d == PREFERRED_DAY else ""
            lines.append(f"  - August {d}{marker}")

        lines += [
            "",
            f"Book here (dates fill fast): {BOOKING_URL}",
            "",
            f"All currently available August days: {available_days}",
        ]
        send_email(subject, "\n".join(lines))

    state["known_available_days"] = relevant_days
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())