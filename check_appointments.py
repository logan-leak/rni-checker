#!/usr/bin/env python3
"""
Breda RNI appointment checker.

What it does:
- Opens the Breda appointment page with Playwright.
- Clicks through to the date/time step if needed.
- Opens the calendar.
- Selects August 2026 using the month/year dropdowns when available.
- Falls back to clicking the Next month button if needed.
- Detects available days in the target month.
- Emails you when it finds new availability.
- Supports --test-email so you can verify SMTP separately from scraping.

This script is intended for GitHub Actions, but it can also be run locally
if Playwright and Chromium are installed in your environment.
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
from typing import Optional

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

MONTH_LOOKUP: dict[str, int] = {}
for idx in range(12):
    for raw in (
        MONTH_NAMES_EN[idx],
        MONTH_NAMES_NL[idx],
        MONTH_NAMES_EN[idx][:3],
        MONTH_NAMES_NL[idx][:3],
    ):
        MONTH_LOOKUP[re.sub(r"[^a-z]", "", raw.lower())] = idx + 1


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"known_available_days": [], "last_failure_notified": False}

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state.json is not a JSON object")
        data.setdefault("known_available_days", [])
        data.setdefault("last_failure_notified", False)
        return data
    except Exception:
        return {"known_available_days": [], "last_failure_notified": False}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


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
# Helpers
# ---------------------------------------------------------------------------
def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def month_aliases(month_idx: int) -> list[str]:
    en = MONTH_NAMES_EN[month_idx - 1]
    nl = MONTH_NAMES_NL[month_idx - 1]
    return [en, en[:3], en.title(), en[:3].title(), nl, nl[:3], nl.title(), nl[:3].title()]


def month_to_num(text: str) -> Optional[int]:
    return MONTH_LOOKUP.get(normalize(text))


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
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click(timeout=2000)
                page.wait_for_timeout(500)
                return
        except Exception:
            pass


def click_any_visible(page, patterns) -> bool:
    for pattern in patterns:
        candidates = [
            page.get_by_role("button", name=re.compile(pattern, re.I)),
            page.get_by_role("link", name=re.compile(pattern, re.I)),
            page.get_by_text(re.compile(pattern, re.I)),
            page.locator(f'button:has-text("{pattern}")'),
            page.locator(f'a:has-text("{pattern}")'),
        ]
        for candidate in candidates:
            try:
                if candidate.count() > 0 and candidate.first.is_visible():
                    candidate.first.click(timeout=3000)
                    page.wait_for_timeout(1200)
                    return True
            except Exception:
                pass
    return False


def advance_to_step_3(page) -> None:
    """
    The Breda flow may start on Step 2. Click through to Step 3 if needed.
    """
    for _ in range(6):
        try:
            body = page.locator("body").inner_text().lower()
        except Exception:
            body = ""

        if "step 3 of 5: select a date and time" in body:
            return

        if "step 2 of 5: things to keep in mind" in body:
            clicked = click_any_visible(page, [
                "Continue to step 3",
                "Continue",
                "Doorgaan",
                "Volgende",
                "Next",
            ])
        else:
            clicked = click_any_visible(page, [
                "Continue to step 4",
                "Continue to step 3",
                "Continue",
                "Doorgaan",
                "Volgende",
                "Next",
            ])

        if not clicked:
            break

    raise RuntimeError("Could not advance to step 3.")


def open_calendar_if_needed(page) -> None:
    candidates = [
        page.get_by_text("Click here to open the calendar", exact=False),
        page.get_by_role("button", name=re.compile(r"calendar", re.I)),
        page.locator('button[aria-label*="calendar" i]'),
        page.locator('button[title*="calendar" i]'),
    ]

    for candidate in candidates:
        try:
            if candidate.count() > 0 and candidate.first.is_visible():
                candidate.first.click(timeout=3000)
                page.wait_for_timeout(1200)
                return
        except Exception:
            pass


def option_texts(select_locator) -> list[str]:
    texts: list[str] = []
    try:
        options = select_locator.locator("option")
        for i in range(options.count()):
            txt = (options.nth(i).inner_text() or "").strip()
            if txt:
                texts.append(txt)
    except Exception:
        pass
    return texts


def find_month_year_selects(page):
    """
    Identify the month and year <select> elements by examining their options.
    """
    selects = page.locator("select")
    if selects.count() < 2:
        return None, None

    month_select = None
    year_select = None

    for i in range(selects.count()):
        sel = selects.nth(i)
        opts = [normalize(t) for t in option_texts(sel)]
        joined = " ".join(opts)

        month_score = sum(1 for opt in opts if month_to_num(opt) is not None)
        year_score = sum(1 for opt in opts if re.fullmatch(r"20\d{2}", opt))

        if month_select is None and (month_score >= 4 or any(m[:3] in joined for m in MONTH_NAMES_EN + MONTH_NAMES_NL)):
            month_select = sel
            continue

        if year_select is None and year_score >= 4:
            year_select = sel
            continue

    if month_select is None:
        month_select = selects.nth(0)
    if year_select is None and selects.count() > 1:
        year_select = selects.nth(1)

    return month_select, year_select


def select_option_by_text(select_locator, target_texts: list[str]) -> bool:
    targets = {normalize(t) for t in target_texts if normalize(t)}

    try:
        options = select_locator.locator("option")
        for i in range(options.count()):
            option = options.nth(i)
            text = normalize(option.inner_text())
            value = normalize(option.get_attribute("value") or "")

            for target in targets:
                if (
                    text == target
                    or text.startswith(target)
                    or target.startswith(text)
                    or value == target
                ):
                    select_locator.select_option(index=i)
                    return True
    except Exception:
        pass

    for txt in target_texts:
        try:
            select_locator.select_option(label=txt)
            return True
        except Exception:
            pass

    return False


def get_visible_month_year(page) -> Optional[tuple[int, int]]:
    """
    Read the visible month/year from the dropdowns if possible.
    """
    try:
        month_select, year_select = find_month_year_selects(page)
        if month_select is not None and year_select is not None:
            month_text = normalize(month_select.locator("option:checked").inner_text())
            year_text = year_select.locator("option:checked").inner_text().strip()
            month_num = month_to_num(month_text)
            if month_num and year_text.isdigit():
                return month_num, int(year_text)
    except Exception:
        pass

    return None


def select_target_month_year(page, target_month: int, target_year: int) -> bool:
    month_select, year_select = find_month_year_selects(page)
    if month_select is None or year_select is None:
        return False

    if not select_option_by_text(month_select, month_aliases(target_month)):
        return False

    if not select_option_by_text(year_select, [str(target_year)]):
        return False

    page.wait_for_timeout(1500)
    return True


def navigate_to_target_month(page, target_month: int, target_year: int, max_clicks: int = 24) -> bool:
    """
    Prefer direct dropdown selection. If that fails, click the Next month button.
    """
    if get_visible_month_year(page) == (target_month, target_year):
        return True

    if select_target_month_year(page, target_month, target_year):
        page.wait_for_timeout(1000)
        if get_visible_month_year(page) == (target_month, target_year):
            return True

    next_selectors = [
        'button:has-text("Next month")',
        'button:has-text("Volgende maand")',
        'button[aria-label*="next" i]',
        'button[aria-label*="volgende" i]',
        'a[aria-label*="next" i]',
        'a[aria-label*="volgende" i]',
        'button:has-text(">")',
    ]

    for _ in range(max_clicks):
        if get_visible_month_year(page) == (target_month, target_year):
            return True

        clicked = False
        for sel in next_selectors:
            try:
                locator = page.locator(sel)
                if locator.count() > 0 and locator.first.is_visible():
                    locator.first.click(timeout=2000)
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
    Parse the visible page text and collect day numbers from lines that say
    'Date is available'.
    """
    try:
        body = page.locator("body").inner_text()
    except Exception:
        return []

    available: set[int] = set()
    for line in body.splitlines():
        if "date is available" not in line.lower():
            continue

        nums = re.findall(r"\b(\d{1,2})\b", line)
        for num in nums:
            day = int(num)
            if 1 <= day <= 31:
                available.add(day)
                break

    return sorted(available)


def check_appointments() -> list[int]:
    """
    Returns the available day numbers in the target month.
    Raises RuntimeError if the page structure can't be understood.
    """
    from playwright.sync_api import sync_playwright  # local import so --test-email works without Playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            page.goto(BOOKING_URL, wait_until="networkidle", timeout=60000)
            dismiss_cookie_banner(page)
            advance_to_step_3(page)
            open_calendar_if_needed(page)
            page.wait_for_timeout(1200)

            reached = navigate_to_target_month(page, TARGET_MONTH, TARGET_YEAR)
            if not reached:
                page.screenshot(path=str(DEBUG_SCREENSHOT), full_page=True)
                DEBUG_HTML.write_text(page.content(), encoding="utf-8")
                raise RuntimeError(
                    "Could not navigate to the target month. "
                    "Saved debug_screenshot.png and debug_page.html for troubleshooting."
                )

            return extract_available_days(page)
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
                    "The Breda RNI appointment checker hit an error and could not read the calendar:\n\n"
                    f"{exc}\n\n"
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
        subject = "🎉 RNI Breda: NEW appointment slot(s) open in August!"
        if PREFERRED_DAY in new_days:
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