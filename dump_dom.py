#!/usr/bin/env python3
"""
One-off diagnostic. Loads the Odyssey listing and prints the real markup for
the pagination controls and the date-search form, so we can target them
properly instead of guessing at URL parameters.

Run this the same way as the tracker, then paste the log output.
"""

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

URL = ("https://whatson.bfi.org.uk/imax/Online/default.asp"
       "?BOparam::WScontent::loadArticle::permalink=odyssey-the-film-imax-70mm-2026")

SDH_URL = ("https://whatson.bfi.org.uk/imax/Online/default.asp"
           "?BOparam::WScontent::loadArticle::permalink=odyssey-the-film-imax-70mm-2026-sdh")

ROWS_PRESENT_JS = r"() => /\d{4}\s+\d{1,2}:\d{2}/.test(document.body.innerText)"


def banner(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def inspect(page, url, label):
    banner(f"{label}: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    try:
        page.wait_for_function(ROWS_PRESENT_JS, timeout=25_000)
        print("rows appeared: yes")
    except PWTimeout:
        print("rows appeared: NO (widget did not render screening rows)")

    page.wait_for_timeout(2_000)

    # How many screening rows are visible right now
    count = page.evaluate(
        r"() => (document.body.innerText.match(/\d{4}\s+\d{1,2}:\d{2}/g) || []).length"
    )
    print(f"screening-like rows visible: {count}")

    # 1. Anything that looks like a pagination container
    banner(f"{label}: pagination containers")
    html = page.evaluate("""() => {
        const out = [];
        const els = document.querySelectorAll(
            '[class*="pag" i], [id*="pag" i], nav, [class*="page" i]'
        );
        els.forEach(el => {
            const t = (el.innerText || '').trim();
            if (t.length && t.length < 200 && /\\d/.test(t)) {
                out.push({
                    tag: el.tagName,
                    cls: el.className && el.className.toString().slice(0, 120),
                    id: el.id,
                    text: t.replace(/\\s+/g, ' ').slice(0, 160),
                    html: el.outerHTML.slice(0, 900)
                });
            }
        });
        return out.slice(0, 6);
    }""")
    if not html:
        print("(none found)")
    for h in html:
        print(f"\n<{h['tag']}> class={h['cls']!r} id={h['id']!r}")
        print(f"  text: {h['text']}")
        print(f"  html: {h['html']}")

    # 2. Every clickable whose label is a number or a next/prev arrow
    banner(f"{label}: numeric / arrow clickables")
    clicks = page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('a, button').forEach(el => {
            const t = (el.innerText || el.textContent || '').trim();
            const aria = el.getAttribute('aria-label') || '';
            const title = el.getAttribute('title') || '';
            if (/^\\d{1,3}$/.test(t) || /next|prev|›|‹|»|«|>|</i.test(t + aria + title)) {
                out.push({
                    text: t.slice(0, 30),
                    aria: aria.slice(0, 40),
                    title: title.slice(0, 40),
                    cls: (el.className || '').toString().slice(0, 80),
                    href: (el.getAttribute('href') || '').slice(0, 160),
                    onclick: (el.getAttribute('onclick') || '').slice(0, 160)
                });
            }
        });
        return out.slice(0, 30);
    }""")
    if not clicks:
        print("(none found)")
    for c in clicks:
        print(f"  text={c['text']!r} aria={c['aria']!r} title={c['title']!r}")
        print(f"    class={c['cls']!r}")
        print(f"    href={c['href']!r}")
        print(f"    onclick={c['onclick']!r}")

    # 3. The date-search form, which may let us skip pagination entirely
    banner(f"{label}: form inputs")
    inputs = page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('input, select').forEach(el => {
            out.push({
                tag: el.tagName,
                type: el.type || '',
                name: (el.name || '').slice(0, 80),
                id: (el.id || '').slice(0, 80),
                ph: (el.placeholder || '').slice(0, 60),
                aria: (el.getAttribute('aria-label') || '').slice(0, 60)
            });
        });
        return out.slice(0, 25);
    }""")
    for i in inputs:
        print(f"  <{i['tag']} type={i['type']!r}> name={i['name']!r} id={i['id']!r} "
              f"placeholder={i['ph']!r} aria={i['aria']!r}")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
            locale="en-GB",
            timezone_id="Europe/London",
        )
        page = ctx.new_page()
        inspect(page, URL, "MAIN")
        inspect(page, SDH_URL, "SDH")
        browser.close()


if __name__ == "__main__":
    main()
