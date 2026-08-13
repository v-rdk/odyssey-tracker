#!/usr/bin/env python3
"""
BFI IMAX - The Odyssey (70mm) screening tracker.

Watches the BFI IMAX listing for The Odyssey and sends a Telegram alert when:
  1. any screening appears on a date after CUTOFF_DATE (i.e. the run is extended)
  2. any screening that was previously "Sold out" becomes bookable (returns)
  3. the booking-information article text changes (this is where BFI posts
     on-sale dates and times for new batches)

The screening list is rendered by the site's Tessitura widget, so a plain
HTTP request will not see it. Playwright is required.

Env vars:
  TELEGRAM_BOT_TOKEN   required
  TELEGRAM_CHAT_ID     required
  STATE_PATH           optional, defaults to ./state.json
  WATCH_RETURNS        optional, "1" to walk every page looking for returns
                       (slower, ~16+ page loads). Default "0" = last page only.
"""

import json
import os
import re
import sys
import hashlib
from datetime import date, datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = "https://whatson.bfi.org.uk/imax/Online/default.asp"
PERMALINK_PARAM = "BOparam::WScontent::loadArticle::permalink"
PAGE_PARAM = "BOset::WScontent::SearchResultsInfo::current_page"

# Listings to watch. The SDH screenings are a separate listing on the BFI site.
LISTINGS = {
    "Odyssey 70mm": "odyssey-the-film-imax-70mm-2026",
    "Odyssey 70mm (SDH)": "odyssey-the-film-imax-70mm-2026-sdh",
}

# Article page where BFI posts on-sale dates for new batches.
INFO_ARTICLE = "odyssey-booking-information"

# Anything strictly after this date is a new screening you care about.
CUTOFF_DATE = date(2026, 9, 10)

STATE_PATH = Path(os.environ.get("STATE_PATH", "state.json"))
WATCH_RETURNS = os.environ.get("WATCH_RETURNS", "0") == "1"

MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11,
    "December": 12,
}

ROW_RE = re.compile(
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
    r"(?P<day>\d{1,2})\s+"
    r"(?P<month>January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+"
    r"(?P<year>\d{4})\s+"
    r"(?P<time>\d{1,2}:\d{2})",
    re.IGNORECASE,
)

SOLD_OUT_RE = re.compile(r"sold\s*out", re.IGNORECASE)


# ----------------------------------------------------------------------------
# Page helpers
# ----------------------------------------------------------------------------

def listing_url(permalink: str, page_no: int | None = None) -> str:
    url = f"{BASE}?{PERMALINK_PARAM}={permalink}"
    if page_no is not None:
        url += f"&{PAGE_PARAM}={page_no}"
    return url


def open_listing(page, permalink: str, page_no: int | None = None) -> str:
    """Load a listing page and return the visible text of the main content."""
    page.goto(listing_url(permalink, page_no), wait_until="networkidle", timeout=60_000)
    # Give the booking widget a beat to inject the performance rows.
    try:
        page.wait_for_function(
            "() => /\\d{1,2}:\\d{2}/.test(document.body.innerText)",
            timeout=20_000,
        )
    except PWTimeout:
        pass
    return page.inner_text("body")


def find_last_page(text: str) -> int:
    """Read the pagination strip and return the highest page number shown."""
    # Pagination renders as a run of bare numbers near the end of the listing.
    tail = text[-2000:]
    nums = [int(n) for n in re.findall(r"(?<!\d)(\d{1,3})(?!\d)", tail)]
    nums = [n for n in nums if 1 <= n <= 200]
    return max(nums) if nums else 1


def parse_screenings(text: str) -> list[dict]:
    """Pull every screening row out of the page text with its sold-out status."""
    matches = list(ROW_RE.finditer(text))
    rows = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else min(len(text), m.end() + 200)
        segment = text[m.end():end]
        try:
            d = date(int(m.group("year")), MONTHS[m.group("month").capitalize()], int(m.group("day")))
        except (KeyError, ValueError):
            continue
        rows.append({
            "date": d.isoformat(),
            "time": m.group("time"),
            "sold_out": bool(SOLD_OUT_RE.search(segment)),
        })
    return rows


def scrape_listing(page, label: str, permalink: str) -> dict:
    """Scrape one listing. Returns page count and the screenings found."""
    first_text = open_listing(page, permalink)
    last_page = find_last_page(first_text)

    screenings = parse_screenings(first_text)

    pages_to_read = range(2, last_page + 1) if WATCH_RETURNS else (
        [last_page] if last_page > 1 else []
    )
    for n in pages_to_read:
        txt = open_listing(page, permalink, n)
        screenings.extend(parse_screenings(txt))

    # Dedupe on date + time, keeping the most available status seen.
    merged: dict[str, dict] = {}
    for s in screenings:
        key = f"{s['date']} {s['time']}"
        if key not in merged or (merged[key]["sold_out"] and not s["sold_out"]):
            merged[key] = s

    return {
        "label": label,
        "last_page": last_page,
        "screenings": dict(sorted(merged.items())),
    }


def scrape_info_article(page) -> str:
    """Hash the booking-info article body so we can detect edits."""
    page.goto(listing_url(INFO_ARTICLE), wait_until="networkidle", timeout=60_000)
    body = page.inner_text("body")
    # Strip the volatile basket / promo furniture before hashing.
    core = re.sub(r"\s+", " ", body)
    core = core.split("Add a promo code")[0]
    return hashlib.sha256(core.encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------------
# State + notification
# ----------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def send_telegram(message: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"Telegram send failed: {resp.status_code} {resp.text}", file=sys.stderr)


def pretty(key: str) -> str:
    d, t = key.split(" ")
    return datetime.strptime(d, "%Y-%m-%d").strftime("%a %d %b") + f" {t}"


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> int:
    state = load_state()
    alerts: list[str] = []
    new_state: dict = {"checked_at": datetime.utcnow().isoformat(timespec="seconds") + "Z"}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            locale="en-GB",
            timezone_id="Europe/London",
        )
        page = ctx.new_page()

        for label, permalink in LISTINGS.items():
            try:
                result = scrape_listing(page, label, permalink)
            except Exception as exc:
                print(f"[{label}] scrape failed: {exc}", file=sys.stderr)
                continue

            prev = state.get(label, {})
            prev_screenings = prev.get("screenings", {})
            prev_last_page = prev.get("last_page", 0)

            # 1. Screenings past the cutoff that we have not alerted on yet.
            extended = [
                k for k, v in result["screenings"].items()
                if date.fromisoformat(v["date"]) > CUTOFF_DATE and k not in prev_screenings
            ]
            if extended:
                lines = "\n".join(f"  - {pretty(k)}" for k in sorted(extended)[:25])
                alerts.append(
                    f"<b>NEW DATES: {label}</b>\n"
                    f"{len(extended)} screening(s) past {CUTOFF_DATE:%d %b}:\n{lines}"
                )

            # 2. Anything bookable right now (returns or a fresh batch on sale).
            bookable = [
                k for k, v in result["screenings"].items()
                if not v["sold_out"] and (
                    k not in prev_screenings or prev_screenings[k].get("sold_out", True)
                )
            ]
            if bookable:
                lines = "\n".join(f"  - {pretty(k)}" for k in sorted(bookable)[:25])
                alerts.append(
                    f"<b>BOOKABLE: {label}</b>\n"
                    f"{len(bookable)} screening(s) showing availability:\n{lines}"
                )

            # 3. Pagination grew, which means rows were added somewhere.
            if prev_last_page and result["last_page"] > prev_last_page:
                alerts.append(
                    f"<b>LISTING GREW: {label}</b>\n"
                    f"Pages went from {prev_last_page} to {result['last_page']}."
                )

            new_state[label] = result

        # 4. Booking-information article edited (this is where on-sale times land).
        try:
            info_hash = scrape_info_article(page)
            new_state["info_hash"] = info_hash
            if state.get("info_hash") and state["info_hash"] != info_hash:
                alerts.append(
                    "<b>BOOKING INFO PAGE CHANGED</b>\n"
                    "BFI edited the Odyssey booking-information article. "
                    "New on-sale dates are usually posted here first."
                )
        except Exception as exc:
            print(f"info article check failed: {exc}", file=sys.stderr)
            new_state["info_hash"] = state.get("info_hash")

        browser.close()

    if alerts:
        body = "\n\n".join(alerts)
        body += f'\n\n<a href="{listing_url(LISTINGS["Odyssey 70mm"])}">Open the BFI listing</a>'
        send_telegram(body)
        print(f"Sent {len(alerts)} alert block(s).")
    else:
        print("No change.")

    save_state(new_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
