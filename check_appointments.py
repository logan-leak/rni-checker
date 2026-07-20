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
are clickable (available) vs. greyed out/disabled (full).

Because every municipality's calendar widget is built a little differently,
the "is this day available" detection uses a few heuristics (see
find_calendar_container / extract_day_cells below). If the site's HTML
doesn't match what we expect, the script will:
  - save a screenshot (debug_screenshot.png) and full page HTML
    (debug_page.html) so we can see what happened, and
  - exit with an error instead of silently reporting "nothing found".

STATE / DE-DUPING
------------------
Previously-seen available dates are stored in state.json so you only get
emailed about *new* openings, not the same ones every 15 minutes. The
GitHub Actions workflow commits state.json back to the repo after each run.
"""

import json
import os
import smtplib
import sys
from datetime import date
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

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

MONTH_NAMES_EN = ["january", "february", "march", "april", "may", "june", "july",
                   "august", "september", "october", "november", "december"]
MONTH_NAMES_NL = ["januari", "februari", "maart", "april", "mei", "juni", "juli",
                   "augustus", "september", "oktober", "november", "december"]


# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"known_available_days": [], "last_failure_notified": False}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# EMAIL
# ---------------------------------------------------------------------------
def send_email(subject: str, body: str) -> None:
    smtp_host = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    from_addr = os.environ["EMAIL_FROM"]
    password = os.environ["EMAIL_PASSWORD"]
    to_addr = os.environ["EMAIL_TO"]

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    with smtplib.SMTP(smtp_host, smtp_port) as server:
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
    """Some booking widgets need you to click 'Next/Continue' through a step
    or two (confirming location/product) before showing the calendar. We
    click a few plausible buttons if present; harmless if the calendar is
    already showing."""
    for text in ["Next", "Continue", "Volgende", "Doorgaan", "Verder"]:
        try:
            btn = page.get_by_role("button", name=text, exact=False)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click(timeout=2000)
                page.wait_for_timeout(800)
        except Exception:
            pass


def get_visible_month_year(page):
    """Look for a heading/label like 'August 2026' or 'augustus 2026'
    somewhere on the page. Returns (month_index_1_based, year) or None."""
    body_text = page.locator("body").inner_text().lower()
    for idx, name in enumerate(MONTH_NAMES_EN + MONTH_NAMES_NL):
        month_num = (idx % 12) + 1
        if name in body_text:
            # crude year search near the month name
            pos = body_text.find(name)
            snippet = body_text[pos:pos + 40]
            for token in snippet.split():
                token = token.strip(",.")
                if token.isdigit() and len(token) == 4:
                    return month_num, int(token)
    return None


def navigate_to_target_month(page, target_month: int, target_year: int, max_clicks: int = 14) -> bool:
    next_selectors = [
        'button[aria-label*="next" i]',
        'button[aria-label*="volgende" i]',
        'a[aria-label*="next" i]',
        'button:has-text(">")',
        '[class*="next" i]',
    ]

    for _ in range(max_clicks):
        current = get_visible_month_year(page)
        if current == (target_month, target_year):
            return True

        clicked = False
        for sel in next_selectors:
            try:
                el = page.locator(sel).first
                if el.count() > 0 and el.is_visible():
                    el.click(timeout=2000)
                    page.wait_for_timeout(700)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            break

    return get_visible_month_year(page) == (target_month, target_year)


def find_calendar_container(page):
    """Try a handful of common container selectors used by calendar/date-
    picker widgets. Returns the first one that contains several numeric
    day buttons/links, or None."""
    candidate_selectors = [
        '[class*="calendar" i]',
        '[class*="Calendar" i]',
        '[role="grid"]',
        'table',
        'form',
        'body',
    ]
    for sel in candidate_selectors:
        try:
            container = page.locator(sel).first
            if container.count() == 0:
                continue
            day_like = container.locator("button, a, td")
            n = day_like.count()
            numeric = 0
            for i in range(min(n, 200)):
                t = day_like.nth(i).inner_text().strip()
                if t.isdigit() and 1 <= int(t) <= 31:
                    numeric += 1
            if numeric >= 15:  # looks like a full month grid
                return container
        except Exception:
            continue
    return None


def extract_available_days(container) -> list:
    """Return a sorted list of day-of-month ints that appear to be
    available (i.e. a clickable, non-disabled element whose text is just
    the day number)."""
    available = []
    elements = container.locator("button, a, td")
    n = elements.count()
    for i in range(n):
        el = elements.nth(i)
        try:
            text = el.inner_text().strip()
        except Exception:
            continue
        if not text.isdigit():
            continue
        day = int(text)
        if not (1 <= day <= 31):
            continue

        try:
            disabled = el.get_attribute("disabled") is not None
            aria_disabled = (el.get_attribute("aria-disabled") or "").lower() == "true"
            class_attr = (el.get_attribute("class") or "").lower()
            looks_disabled = disabled or aria_disabled or "disabled" in class_attr or "unavailable" in class_attr
            visible = el.is_visible()
        except Exception:
            continue

        if visible and not looks_disabled:
            available.append(day)

    return sorted(set(available))


def check_appointments() -> list:
    """Returns a sorted list of available day-numbers in the target month,
    or raises RuntimeError if the page structure couldn't be understood."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(BOOKING_URL, wait_until="networkidle", timeout=60000)
        dismiss_cookie_banner(page)
        click_through_intro_steps(page)
        page.wait_for_timeout(1500)

        reached = navigate_to_target_month(page, TARGET_MONTH, TARGET_YEAR)

        container = find_calendar_container(page)
        if container is None or not reached:
            page.screenshot(path=str(DEBUG_SCREENSHOT), full_page=True)
            DEBUG_HTML.write_text(page.content())
            browser.close()
            raise RuntimeError(
                "Could not find/navigate the calendar. Saved debug_screenshot.png "
                "and debug_page.html for troubleshooting."
            )

        available_days = extract_available_days(container)
        browser.close()
        return available_days


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
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
