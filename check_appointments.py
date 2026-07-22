#!/usr/bin/env python3
"""
Breda RNI appointment checker - debugging version.

Expected flow:
1) advance to step 2
2) advance to step 3
3) click the calendar icon
4) click the right arrow once
5) read the August availability text

This version is intentionally verbose and easier to debug.
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

BOOKING_URL = "https://breda.mijnafspraakmaken.nl/?lang=en&link=6adf&product=45"

STATE_FILE = Path(__file__).parent / "state.json"
DEBUG_SCREENSHOT = Path(__file__).parent / "debug_screenshot.png"
DEBUG_HTML = Path(__file__).parent / "debug_page.html"

TARGET_MONTH_NAME = "August"
PREFERRED_DAY = 19
EARLIEST_RELEVANT_DAY = 18


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
def smtp_port() -> int:
    raw = (os.getenv("SMTP_PORT") or "587").strip()
    return int(raw)


def send_email(subject: str, body: str) -> None:
    smtp_host = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    port = smtp_port()
    from_addr = os.environ["EMAIL_FROM"]
    password = os.environ["EMAIL_PASSWORD"]
    to_addr = os.environ["EMAIL_TO"]

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    with smtplib.SMTP(smtp_host, port, timeout=30) as server:
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
def page_text(page) -> str:
    return page.locator("body").inner_text()


def click_first_visible(locator, timeout: int = 3000) -> bool:
    try:
        if locator.count() > 0 and locator.first.is_visible():
            locator.first.click(timeout=timeout)
            return True
    except Exception:
        pass
    return False


def click_any(page, candidates) -> bool:
    for candidate in candidates:
        try:
            if candidate.count() > 0 and candidate.first.is_visible():
                candidate.first.click(timeout=3000)
                page.wait_for_timeout(900)
                return True
        except Exception:
            pass
    return False


def dismiss_cookie_banner(page) -> None:
    candidates = [
        page.get_by_role("button", name=re.compile(r"accept|akkoord|allow|toestaan|ok", re.I)),
        page.get_by_text(re.compile(r"accept|akkoord|allow|toestaan|ok", re.I)),
    ]
    for c in candidates:
        if click_first_visible(c):
            page.wait_for_timeout(500)
            return


def advance_to_step_2(page) -> None:
    """
    Some runs land directly on step 2; some may need a first click.
    This function clicks through until step 2 is visible.
    """
    for _ in range(4):
        text = page_text(page).lower()
        if "step 2 of 5: things to keep in mind" in text:
            return

        clicked = click_any(
            page,
            [
                page.get_by_role("button", name=re.compile(r"continue|next|doorgaan|volgende", re.I)),
                page.get_by_role("link", name=re.compile(r"continue|next|doorgaan|volgende", re.I)),
                page.get_by_text(re.compile(r"continue|next|doorgaan|volgende", re.I)),
            ],
        )
        if not clicked:
            break

        page.wait_for_timeout(1200)

    text = page_text(page).lower()
    if "step 2 of 5: things to keep in mind" not in text:
        raise RuntimeError("Could not advance to step 2.")


def advance_to_step_3(page) -> None:
    """
    Click the step 2 continue button to reach step 3.
    """
    for _ in range(4):
        text = page_text(page).lower()
        if "step 3 of 5: select a date and time" in text:
            return

        clicked = click_any(
            page,
            [
                page.get_by_role("button", name=re.compile(r"continue to step 3|continue|doorgaan|volgende|next", re.I)),
                page.get_by_role("link", name=re.compile(r"continue to step 3|continue|doorgaan|volgende|next", re.I)),
                page.get_by_text(re.compile(r"continue to step 3|continue|doorgaan|volgende|next", re.I)),
            ],
        )
        if not clicked:
            break

        page.wait_for_timeout(1200)

    text = page_text(page).lower()
    if "step 3 of 5: select a date and time" not in text:
        raise RuntimeError("Could not advance to step 3.")


def click_calendar_icon(page) -> None:
    # 1) Try the visible text and force the click.
    text_locator = page.get_by_text("Click here to open the calendar", exact=False)

    if text_locator.count() > 0:
        try:
            text_locator.first.scroll_into_view_if_needed()
            text_locator.first.click(timeout=3000, force=True)
            page.wait_for_timeout(1200)
            return
        except Exception:
            pass

        try:
            text_locator.first.evaluate(
                """
                el => {
                  const clickable =
                    el.closest('button, a, [role="button"]') ||
                    el.parentElement?.closest('button, a, [role="button"]') ||
                    el.parentElement;
                  if (clickable) clickable.click();
                }
                """
            )
            page.wait_for_timeout(1200)
            return
        except Exception:
            pass

    # 2) Fallback: try common calendar-button selectors.
    fallback_candidates = [
        page.locator('button[aria-label*="calendar" i]'),
        page.locator('button[title*="calendar" i]'),
        page.locator('button:has(svg)'),
        page.locator('button:has-text("calendar")'),
        page.locator('a:has-text("calendar")'),
        page.get_by_role("button", name=re.compile(r"calendar", re.I)),
    ]

    for candidate in fallback_candidates:
        try:
            if candidate.count() > 0 and candidate.first.is_visible():
                candidate.first.click(timeout=3000, force=True)
                page.wait_for_timeout(1200)
                return
        except Exception:
            pass

    # 3) Debug dump if nothing worked.
    print("\n--- Calendar opener debug ---")
    print("Visible text count:", text_locator.count())
    for sel in [
        'button[aria-label*="calendar" i]',
        'button[title*="calendar" i]',
        'button:has(svg)',
        'button',
        'a',
    ]:
        try:
            print(sel, "=>", page.locator(sel).count())
        except Exception as e:
            print(sel, "=> ERROR", e)

    raise RuntimeError("Could not click the calendar opener.")


def click_right_arrow(page) -> None:
    """
    Click the next month/right arrow once.
    """
    candidates = [
        page.get_by_role("button", name=re.compile(r"next month|volgende maand|next|volgende", re.I)),
        page.locator('button:has-text("Next month")'),
        page.locator('button:has-text("Volgende maand")'),
        page.locator('button[aria-label*="next" i]'),
        page.locator('button[aria-label*="volgende" i]'),
        page.locator('a[aria-label*="next" i]'),
        page.locator('a[aria-label*="volgende" i]'),
        page.locator('button:has-text(">")'),
    ]

    if not click_any(page, candidates):
        raise RuntimeError("Could not click the right arrow / next month button.")

    page.wait_for_timeout(1500)


def read_august_availability(page) -> tuple[int | None, str]:
    """
    Parse the page text for:
      - "There are no days available in August"
      - "There are X days available in August"
    """
    text = page_text(page)

    m = re.search(
        r"There are\s+(no|\d+)\s+days available in August",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None, text

    value = m.group(1).lower()
    if value == "no":
        return 0, text
    return int(value), text


def run_checker(headless: bool = True) -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=250 if not headless else 0)
        page = browser.new_page()

        try:
            page.goto(BOOKING_URL, wait_until="networkidle", timeout=60000)
            dismiss_cookie_banner(page)

            advance_to_step_2(page)
            advance_to_step_3(page)
            click_calendar_icon(page)
            click_right_arrow(page)

            count, text = read_august_availability(page)
            if count is None:
                DEBUG_SCREENSHOT.write_bytes(page.screenshot(full_page=True))
                DEBUG_HTML.write_text(page.content(), encoding="utf-8")
                raise RuntimeError(
                    "Could not find the August availability text. "
                    "Saved debug_screenshot.png and debug_page.html for troubleshooting."
                )

            print(f"August availability count: {count}")
            print()
            print(text)

            return count
        except Exception:
            try:
                DEBUG_SCREENSHOT.write_bytes(page.screenshot(full_page=True))
                DEBUG_HTML.write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
            raise
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Breda RNI appointment checker")
    parser.add_argument("--test-email", action="store_true", help="Send a test email and exit")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run the browser visibly for debugging",
    )
    args = parser.parse_args()

    if args.test_email:
        send_test_email()
        print("Test email sent successfully.")
        return 0

    state = load_state()

    try:
        count = run_checker(headless=not args.headed)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)

        if not state.get("last_failure_notified"):
            try:
                send_email(
                    "RNI checker: script needs attention",
                    "The Breda RNI appointment checker hit an error and could not read the page:\n\n"
                    f"{exc}\n\n"
                    "Check debug_screenshot.png and debug_page.html to see what changed.",
                )
                state["last_failure_notified"] = True
                save_state(state)
            except Exception as email_exc:
                print(f"Also failed to send failure email: {email_exc}", file=sys.stderr)

        return 1

    if state.get("last_failure_notified"):
        state["last_failure_notified"] = False

    # For this debugging version, just log the count.
    relevant = count if count is not None else 0
    state["known_available_days"] = [relevant]
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())