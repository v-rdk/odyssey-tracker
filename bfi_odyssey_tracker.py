#!/usr/bin/env python3
"""
BFI IMAX - The Odyssey (70mm) screening tracker.

Sends a Telegram alert when:
  1. a screening appears on a date after CUTOFF_DATE (the run is extended)
  2. a screening shows as bookable rather than sold out (new batch or returns)
  3. the total page count grows (rows were added somewhere)
  4. the booking-information article changes (BFI posts on-sale times there)

Notes on how this site works, learned the hard way:
  - The screening rows are injected by the Tessitura widget, so plain HTTP
    requests see nothing. Playwright is required.
  - networkidle never fires here. Wait for the rows themselves instead.
  - Pagination hrefs carry a per-session sToken. Hand-built page URLs are
    ignored, so the real hrefs must be read out of ul.pagination.
  - Hammering the site trips a Cloudflare Turnstile challenge. Pace the loads.

Env vars:
  TELEGRAM_BOT_TOKEN   required
  TELEGRAM_CHAT_ID     required
  STATE_PATH           optional, defaults to ./state.json
  DEEP_SCAN            optional, "1" to walk every page (catches returns on
                       early dates too, but ~16 loads and more challenge risk)
  TAIL_PAGES           optional, how many end pages to read, defaults to 3
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = "https://whatson.bfi.org.uk/imax/Online/default.asp"
PERMALINK_PARAM = "BOparam::WScontent::loadArticle::permalink"

LISTINGS = {
    "Odyssey 70mm": "odyssey-the-film-imax-70mm-2026",
    "Odyssey 70mm (SDH)": "odyssey-the-film-imax-70mm-2026-sdh",
}

INFO_ARTICLE = "odyssey-booking-information"

CUTOFF_DATE = date(2026, 9, 10)

STATE_PATH = Path(os.environ.get("STATE_PATH", "state.json"))
DEEP_SCAN = os.environ.get("DEEP_SCAN", "0") == "1"
TAIL_PAGES = int(os.environ.get("TAIL_PAGES", "3"))

# Pace between navigations. Too fast and Cloudflare serves a challenge.
PAUSE_SECONDS = 3.0

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

ROWS_PRESENT_JS = r"() => /\d{4}\s+\d{1,2}:\d{2}/.test(document.body.innerText)"

# Reads the real pagination links, sToken and all.
PAGINATION_JS = """() => {
    const out = [];
    document.querySelectorAll('ul.pagination a.page-link').forEach(a => {
        out.push({
            text: (a.innerText || a.textContent || '').trim(),
            href: a.getAttribute('href') || ''
        });
    });
    return out;
}"""

CHALLENGE_JS = """() => {
    if (document.querySelector('[name="cf-turnstile-response"]')) return true;
    if (document.querySelector('#challenge-form, .cf-challenge')) return true;
    return (document.body.innerText || '').trim().length < 400;
}"""


class Challenged(Exception):
    """Cloudflare served a bot challenge instead of the page."""


# ----------------------------------------------------------------------------
# Page helpers
# ----------------------------------------------------------------------------

def article_url(permalink: str) -> str:
    return f"{BASE}?{PERMALINK_PARAM}={permalink}"


def load(page, url: str, need_rows: bool = True, attempts: int = 3) -> str:
    """Navigate and return visible text, retrying through challenges."""
    last_err: Exception | None = None

    for attempt in range(attempts):
        if attempt:
            time.sleep(PAUSE_SECONDS * (attempt + 1))
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except PWTimeout as exc:
            last_err = exc
            continue

        if page.evaluate(CHALLENGE_JS):
            last_err = Challenged("Cloudflare challenge served")
            print("  challenge detected, backing off", file=sys.stderr)
            continue

        if need_rows:
            try:
                page.wait_for_function(ROWS_PRESENT_JS, timeout=25_000)
            except PWTimeout:
                pass
        else:
            page.wait_for_timeout(2_000)

        return page.inner_text("body")

    raise last_err if last_err else RuntimeError(f"could not load {url}")


def parse_screenings(text: str) -> dict[str, dict]:
    """Pull screening rows out of page text with their sold-out status."""
    matches = list(ROW_RE.finditer(text))
    rows: dict[str, dict] = {}

    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else min(len(text), m.end() + 200)
        segment = text[m.end():end]
        try:
            d = date(int(m.group("year")), MONTHS[m.group("month").lower()],
                     int(m.group("day")))
        except (KeyError, ValueError):
            continue

        key = f"{d.isoformat()} {m.group('time')}"
        entry = {"date": d.isoformat(), "time": m.group("time"),
                 "sold_out": bool(SOLD_OUT_RE.search(segment))}
        if key not in rows or (rows[key]["sold_out"] and not entry["sold_out"]):
            rows[key] = entry

    return rows


def read_pagination(page) -> tuple[int, dict[int, str]]:
    """Return (last page number, {page number: absolute href})."""
    links = page.evaluate(PAGINATION_JS)
    hrefs: dict[int, str] = {}
    for link in links:
        if link["text"].isdigit() and link["href"]:
            hrefs[int(link["text"])] = urljoin(page.url, link["href"])
    last = max(hrefs) if hrefs else 1
    return last, hrefs


def merge(into: dict[str, dict], rows: dict[str, dict]) -> None:
    for k, v in rows.items():
        if k not in into or (into[k]["sold_out"] and not v["sold_out"]):
            into[k] = v


def scrape_listing(page, label: str, permalink: str) -> dict:
    """
    Read page 1, then jump to the tail pages via the real pagination hrefs.

    Screenings are listed chronologically, so newly added dates land at the
    end. Reading the last few pages catches them without crawling all 16.
    """
    all_rows: dict[str, dict] = {}

    text = load(page, article_url(permalink))
    first = parse_screenings(text)
    if not first:
        raise RuntimeError("no screening rows found on the first page")
    merge(all_rows, first)

    last_page, hrefs = read_pagination(page)
    print(f"  page 1: {len(first)} rows, {last_page} page(s) total")

    if DEEP_SCAN:
        wanted = sorted(n for n in hrefs if n >= 2)
    else:
        wanted = sorted(n for n in hrefs if n > max(1, last_page - TAIL_PAGES))

    pages_read = [1]
    for n in wanted:
        time.sleep(PAUSE_SECONDS)
        try:
            text = load(page, hrefs[n])
        except Exception as exc:
            print(f"  page {n} failed: {exc}", file=sys.stderr)
            continue

        rows = parse_screenings(text)
        print(f"  page {n}: {len(rows)} rows")
        merge(all_rows, rows)
        pages_read.append(n)

        # Later pages expose links to pages that were hidden behind the "..."
        if not DEEP_SCAN:
            newer_last, more = read_pagination(page)
            last_page = max(last_page, newer_last)
            hrefs.update({k: v for k, v in more.items() if k not in hrefs})

    return {
        "label": label,
        "last_page": last_page,
        "pages_read": pages_read,
        "screenings": dict(sorted(all_rows.items())),
    }


def scrape_info_article(page) -> str:
    body = load(page, article_url(INFO_ARTICLE), need_rows=False)
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
        json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": message,
              "parse_mode": "HTML"},
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
    ok_count = 0
    new_state: dict = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
            locale="en-GB",
            timezone_id="Europe/London",
        )
        page = ctx.new_page()

        for label, permalink in LISTINGS.items():
            print(f"[{label}]")
            try:
                result = scrape_listing(page, label, permalink)
                ok_count += 1
            except Exception as exc:
                print(f"  scrape failed: {exc}", file=sys.stderr)
                if label in state:
                    new_state[label] = state[label]
                time.sleep(PAUSE_SECONDS * 2)
                continue

            print(f"  total: {len(result['screenings'])} screenings")

            prev = state.get(label, {})
            prev_screenings = prev.get("screenings", {})
            prev_last_page = prev.get("last_page", 0)

            extended = [
                k for k, v in result["screenings"].items()
                if date.fromisoformat(v["date"]) > CUTOFF_DATE and k not in prev_screenings
            ]
            if extended:
                lines = "\n".join(f"  - {pretty(k)}" for k in sorted(extended)[:25])
                alerts.append(f"<b>NEW DATES: {label}</b>\n"
                              f"{len(extended)} screening(s) past "
                              f"{CUTOFF_DATE:%d %b}:\n{lines}")

            bookable = [
                k for k, v in result["screenings"].items()
                if not v["sold_out"]
                and (k not in prev_screenings or prev_screenings[k].get("sold_out", True))
            ]
            if bookable:
                lines = "\n".join(f"  - {pretty(k)}" for k in sorted(bookable)[:25])
                alerts.append(f"<b>BOOKABLE: {label}</b>\n"
                              f"{len(bookable)} screening(s) showing "
                              f"availability:\n{lines}")

            if prev_last_page and result["last_page"] > prev_last_page:
                alerts.append(f"<b>LISTING GREW: {label}</b>\n"
                              f"Pages went from {prev_last_page} to "
                              f"{result['last_page']}.")

            new_state[label] = result
            time.sleep(PAUSE_SECONDS)

        try:
            info_hash = scrape_info_article(page)
            new_state["info_hash"] = info_hash
            ok_count += 1
            print(f"info article hash: {info_hash}")
            if state.get("info_hash") and state["info_hash"] != info_hash:
                alerts.append("<b>BOOKING INFO PAGE CHANGED</b>\n"
                              "BFI edited the Odyssey booking-information "
                              "article. On-sale dates are posted there first.")
        except Exception as exc:
            print(f"info article check failed: {exc}", file=sys.stderr)
            new_state["info_hash"] = state.get("info_hash")

        browser.close()

    if alerts:
        body = "\n\n".join(alerts)
        body += (f'\n\n<a href="{article_url(LISTINGS["Odyssey 70mm"])}">'
                 f'Open the BFI listing</a>')
        send_telegram(body)
        print(f"Sent {len(alerts)} alert block(s).")
    else:
        print("No change.")

    save_state(new_state)

    # Go red if nothing at all could be read, so a broken tracker is visible
    # instead of quietly reporting "No change" forever.
    if ok_count == 0:
        print("Every check failed. Treating this run as broken.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
