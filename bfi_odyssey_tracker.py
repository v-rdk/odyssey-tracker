#!/usr/bin/env python3
"""
BFI IMAX - The Odyssey (70mm) screening tracker.

Watches the BFI IMAX listing for The Odyssey and sends a Telegram alert when:
  1. any screening appears on a date after CUTOFF_DATE (the run is extended)
  2. any screening shows as bookable rather than sold out (new batch or returns)
  3. the booking-information article changes (BFI posts on-sale times there)

The screening list is injected by the site's Tessitura widget, so a plain HTTP
request will not see it. Playwright is required.

Env vars:
  TELEGRAM_BOT_TOKEN   required
  TELEGRAM_CHAT_ID     required
  STATE_PATH           optional, defaults to ./state.json
"""

import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = "https://whatson.bfi.org.uk/imax/Online/default.asp"
PERMALINK_PARAM = "BOparam::WScontent::loadArticle::permalink"
PAGE_PARAM = "BOset::WScontent::SearchResultsInfo::current_page"

LISTINGS = {
    "Odyssey 70mm": "odyssey-the-film-imax-70mm-2026",
    "Odyssey 70mm (SDH)": "odyssey-the-film-imax-70mm-2026-sdh",
}

INFO_ARTICLE = "odyssey-booking-information"

# Anything strictly after this date is a new screening worth shouting about.
CUTOFF_DATE = date(2026, 9, 10)

# Hard stop so a pagination quirk can never loop forever.
MAX_PAGES = 60

STATE_PATH = Path(os.environ.get("STATE_PATH", "state.json"))

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
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

# Fires once a row like "... 2026 13:30" is present in the DOM.
ROWS_PRESENT_JS = r"() => /\d{4}\s+\d{1,2}:\d{2}/.test(document.body.innerText)"


# ----------------------------------------------------------------------------
# Page helpers
# ----------------------------------------------------------------------------

def listing_url(permalink: str, page_no: int | None = None) -> str:
    url = f"{BASE}?{PERMALINK_PARAM}={permalink}"
    if page_no is not None:
        url += f"&{PAGE_PARAM}={page_no}"
    return url


def load_page(page, permalink: str, page_no: int | None = None,
              need_rows: bool = True, attempts: int = 2) -> str:
    """
    Load a page and return its visible text.

    Waits on domcontentloaded rather than networkidle: this site keeps
    background requests running indefinitely, so networkidle never fires.
    """
    url = listing_url(permalink, page_no)
    last_err: Exception | None = None

    for _ in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except PWTimeout as exc:
            last_err = exc
            continue

        if need_rows:
            try:
                page.wait_for_function(ROWS_PRESENT_JS, timeout=25_000)
            except PWTimeout:
                # No rows may be legitimate, e.g. a page past the end.
                pass
        else:
            page.wait_for_timeout(2_000)

        return page.inner_text("body")

    raise last_err if last_err else RuntimeError(f"could not load {url}")


def parse_screenings(text: str) -> dict[str, dict]:
    """Pull every screening row out of the page text with its sold-out status."""
    matches = list(ROW_RE.finditer(text))
    rows: dict[str, dict] = {}

    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else min(len(text), m.end() + 200)
        segment = text[m.end():end]
        try:
            d = date(
                int(m.group("year")),
                MONTHS[m.group("month").lower()],
                int(m.group("day")),
            )
        except (KeyError, ValueError):
            continue

        key = f"{d.isoformat()} {m.group('time')}"
        entry = {"date": d.isoformat(), "time": m.group("time"),
                 "sold_out": bool(SOLD_OUT_RE.search(segment))}
        # If the same slot shows twice, keep the more available reading.
        if key not in rows or (rows[key]["sold_out"] and not entry["sold_out"]):
            rows[key] = entry

    return rows


def scrape_listing(page, label: str, permalink: str) -> dict:
    """
    Walk pages 1, 2, 3 ... until a page is empty or repeats the previous one.

    No pagination parsing: out-of-range pages on this site tend to clamp back
    to the last real page, so a repeat is the reliable end-of-list signal.
    """
    all_rows: dict[str, dict] = {}
    seen: set[str] = set()

    first = parse_screenings(load_page(page, permalink))
    if not first:
        raise RuntimeError("no screening rows found on the first page")

    all_rows.update(first)
    seen.add(json.dumps(sorted(first), sort_keys=True))
    pages_read = 1

    for n in range(2, MAX_PAGES + 1):
        rows = parse_screenings(load_page(page, permalink, n))
        if not rows:
            break
        sig = json.dumps(sorted(rows), sort_keys=True)
        if sig in seen:
            break
        seen.add(sig)
        for k, v in rows.items():
            if k not in all_rows or (all_rows[k]["sold_out"] and not v["sold_out"]):
                all_rows[k] = v
        pages_read = n

    return {
        "label": label,
        "pages": pages_read,
        "screenings": dict(sorted(all_rows.items())),
    }


def scrape_info_article(page) -> str:
    """Hash the booking-info article body so we can detect edits."""
    body = load_page(page, INFO_ARTICLE, need_rows=False)
    core = re.sub(r"\s+", " ", body).split("Add a promo code")[0]
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
    resp = requests.post(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
        json={
            "chat_id": os.environ["TELEGRAM_CHAT_ID"],
            "text": message,
            "parse_mode": "HTML",
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
    failures: list[str] = []
    new_state: dict = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")
    }

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
                failures.append(label)
                # Carry the old state forward so we do not re-alert next run.
                if label in state:
                    new_state[label] = state[label]
                continue

            print(f"[{label}] {len(result['screenings'])} screenings "
                  f"across {result['pages']} page(s)")

            prev = state.get(label, {})
            prev_screenings = prev.get("screenings", {})
            prev_pages = prev.get("pages", 0)

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

            bookable = [
                k for k, v in result["screenings"].items()
                if not v["sold_out"]
                and (k not in prev_screenings or prev_screenings[k].get("sold_out", True))
            ]
            if bookable:
                lines = "\n".join(f"  - {pretty(k)}" for k in sorted(bookable)[:25])
                alerts.append(
                    f"<b>BOOKABLE: {label}</b>\n"
                    f"{len(bookable)} screening(s) showing availability:\n{lines}"
                )

            if prev_pages and result["pages"] > prev_pages:
                alerts.append(
                    f"<b>LISTING GREW: {label}</b>\n"
                    f"Pages went from {prev_pages} to {result['pages']}."
                )

            new_state[label] = result

        try:
            info_hash = scrape_info_article(page)
            new_state["info_hash"] = info_hash
            print(f"info article hash: {info_hash}")
            if state.get("info_hash") and state["info_hash"] != info_hash:
                alerts.append(
                    "<b>BOOKING INFO PAGE CHANGED</b>\n"
                    "BFI edited the Odyssey booking-information article. "
                    "New on-sale dates are usually posted there first."
                )
        except Exception as exc:
            print(f"info article check failed: {exc}", file=sys.stderr)
            failures.append("info article")
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

    # Fail loudly if nothing could be scraped, so a silently broken tracker
    # shows up as a red run instead of a permanent "No change".
    if len(failures) >= len(LISTINGS) + 1:
        print("Every check failed. Treating this run as broken.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
