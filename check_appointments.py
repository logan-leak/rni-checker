#!/usr/bin/env python3
"""
Checks the Breda RNI appointment booking widget for open slots in August 2026,
and emails you when it finds any (with extra emphasis if Aug 19 specifically
is open). Designed to be run on a schedule by GitHub Actions, but also runs
fine from your own machine.

HOW IT WORKS
------------
This site (mijnafspraakmaken.nl / JCC-Afspraken) doesn't expose a public
JSON API, so this script drives a real (headless) browser with Playwright,
loads the booking calendar, navigates to August 2026, and reads which days
are marked as available.

Because every municipality's calendar widget is built a little differently,
the "is this day available" detection uses a few heuristics. If the site's
HTML doesn't match what we expect, the script will:
  - save a screenshot (debug_screenshot.png) and full page HTML
    (debug_page.html) so we can see what happened, and
  - exit with an error instead of silently reporting "nothing found".

STATE / DE-DUPING
------------------
Previously-seen available dates are stored in state.json so you only get
emailed about *new* openings, not the same ones every 15 minutes. The GitHub
Actions workflow commits state.json back to the repo after each run.

TEST MODE
---------
Run with --test-email to send a one-off email using the configured SMTP
settings. This is the fastest way to verify that email delivery works.
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

from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
BOOKING_URL = "https://breda.mijnafspraakmaken.nl/?lang=en&link=6adf&product=45"
TARGET_YEAR = 2026
TARGET_MONTH = 8  # August
PREFERRED_DAY = 19
# We only care about dates from arrival onward.
EARLIEST_RELEVANT_DAY = 18

STATE_FILE = Path(__file__).parent / "state.json"
DEBUG_SCREENSHOT = Path(__file__).parent / "debug_screenshot.png"
DEBUG_HTML = Path(__file__).parent / "debug_page.html"

MONTH_NAMES_EN = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]
MONTH_NAMES_NL = [
    "januari",
    "februari",
    "maart",
    "april",
    "mei",
    "juni",
    "juli",
    "augustus",
    "september",
    "oktober",
    "november",
    "december",
]
MONTH_NAME_TO_NUM = {
    **{name: i + 1 for i, name in enumerate(MONTH_NAMES_EN)},
    **{name: i + 1 for i, name in enumerate(MONTH_NAMES_NL)},
}
MONTH_YEAR_RE = re.compile(
    r"^(January|February|March|April|May|June|July|August|"
    r"September|October|November|December|"
    r"januari|februari|maart|april|mei|juni|juli|augustus|"
    r"september|oktober|november|december)\s+(\d{4})$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {"known_available_days": [], "last_failure_notified": False}
    return {"known_available_days": [], "last_failure_notified": False}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# EMAIL
# ---------------------------------------------------------------------------
def _smtp_port() -> int:
    raw = (os.getenv("SMTP_PORT") or "587").strip()
    try:
        return int(raw)
    except ValueError:
        return 587


def send_email(subject: str, body: str) -> None:
    smtp_host = (os.getenv("SMTP_SERVER") or "smtp.gmail.com").strip() or "smtp.gmail.com"
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


# ---------------------------------------------------------------------------
# SCRAPING
# ---------------------------------------------------------------------------
def dismiss_cookie_banner(page) -> None:
    for text in ["Accept", "Akkoord", "Alles accepteren", "Toestaan", "OK", "Accepteren"]:
        try:
            btn = page.get_by_role("button", name=text, exact=False)
            if btn.count() > 0:
                btn.first.click(timeout=2000)
                page.wait_for_timeout(500)
                return
        except Exception:
            pass


def click_through_intro_steps(page) -> None:
    """Some booking widgets need you to click through a step or two before
    showing the calendar."""
    for text in ["Next", "Continue", "Volgende", "Doorgaan", "Verder"]:
        try:
            btn = page.get_by_role("button", name=text, exact=False)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click(timeout=2000)
                page.wait_for_timeout(800)
        except Exception:
            pass


def _page_text(page) -> str:
    return page.locator("body").inner_text()


def get_visible_month_year(page) -> Optional[tuple[int, int]]:
    """Find an exact month/year heading like 'July 2026'."""
    for raw_line in _page_text(page).splitlines():
        line = " ".join(raw_line.split())
        match = MONTH_YEAR_RE.match(line)
        if not match:
            continue
        month_name = match.group(1).lower()
        month_num = MONTH_NAME_TO_NUM.get(month_name)
        if month_num is None:
            continue
        return month_num, int(match.group(2))
    return None


def _click_first_visible(page, selectors: list[str]) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() == 0:
                continue
            el = loc.first
            if el.is_visible():
                el.click(timeout=2000)
                return True
        except Exception:
            continue
    return False


def navigate_to_target_month(page, target_month: int, target_year: int, max_clicks: int = 24) -> bool:
    next_selectors = [
        'button:has-text("Next month")',
        'button:has-text("Next")',
        'button[aria-label*="next month" i]',
        'button[aria-label*="next" i]',
        'a[aria-label*="next" i]',
        'button:has-text(">")',
        '[class*="next" i]',
    ]
    prev_selectors = [
        'button:has-text("Previous month")',
        'button:has-text("Previous")',
        'button[aria-label*="previous month" i]',
        'button[aria-label*="previous" i]',
        'a[aria-label*="previous" i]',
        'button:has-text("<")',
        '[class*="previous" i]',
        '[class*="prev" i]',
    ]

    target_index = target_year * 12 + target_month

    for _ in range(max_clicks):
        current = get_visible_month_year(page)
        if current == (target_month, target_year):
            return True

        if current is None:
            moved = _click_first_visible(page, next_selectors)
        else:
            current_index = current[1] * 12 + current[0]
            if current_index < target_index:
                moved = _click_first_visible(page, next_selectors)
            else:
                moved = _click_first_visible(page, prev_selectors)

        if not moved:
            break

        page.wait_for_timeout(700)

    return get_visible_month_year(page) == (target_month, target_year)


def extract_available_days(page, month_num: int) -> list[int]:
    """Parse the page text for blocks like:
       21
       July 21
       Date is available
    """
    month_name = MONTH_NAMES_EN[month_num - 1].capitalize()
    text = _page_text(page)

    pattern = re.compile(
        rf"(?ims)(?:^|\n)\s*(\d{{1,2}})\s+{re.escape(month_name)}\s+\1\s+Date is available\b"
    )
    days = [int(m.group(1)) for m in pattern.finditer(text)]
    return sorted(set(days))


def check_appointments() -> list[int]:
    """Return a sorted list of available day numbers in the target month.
    Raises RuntimeError if the page structure couldn't be understood."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(BOOKING_URL, wait_until="networkidle", timeout=60000)
            dismiss_cookie_banner(page)
            click_through_intro_steps(page)
            page.wait_for_timeout(1500)

            reached = navigate_to_target_month(page, TARGET_MONTH, TARGET_YEAR)
            if not reached:
                page.screenshot(path=str(DEBUG_SCREENSHOT), full_page=True)
                DEBUG_HTML.write_text(page.content())
                raise RuntimeError(
                    "Could not navigate to the target month. "
                    "Saved debug_screenshot.png and debug_page.html for troubleshooting."
                )

            available_days = extract_available_days(page, TARGET_MONTH)
            return available_days
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Breda RNI appointment checker")
    parser.add_argument(
        "--test-email",
        action="store_true",
        help="Send a one-off test email using the configured SMTP settings and exit.",
    )
    args = parser.parse_args()

    if args.test_email:
        send_email(
            "RNI checker test email",
            "This is a test email from the Breda RNI appointment checker.\n\n"
            "If you can read this, SMTP is working.",
        )
        print("Test email sent.")
        return

    state = load_state()

    try:
        available_days = check_appointments()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if not state.get("last_failure_notified"):
            try:
                send_email(
                    "RNI checker: script needs attention",
                    "The Breda RNI appointment checker hit an error and couldn't "
                    f"read the calendar:\n\n{exc}\n\n"
                    "Check the GitHub Actions run's uploaded debug files "
                    "(debug_screenshot.png / debug_page.html) to see what changed.",
                )
                state["last_failure_notified"] = True
                save_state(state)
            except Exception as email_exc:
                print(f"Also failed to send failure email: {email_exc}", file=sys.stderr)
        sys.exit(1)

    # success -> clear failure flag
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


if __name__ == "__main__":
    main()
