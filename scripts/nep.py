# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright>=1.47", "beautifulsoup4>=4.12"]
# ///
"""nepremicnine.net helper used by the nepremicnine-scout skill.

nepremicnine.net sits behind a Cloudflare challenge that blocks plain HTTP clients and
headless Chrome, so pages are loaded in a headed system Chrome (wrap in `xvfb-run -a`
when there is no display, e.g. from cron).

Commands:
  scan [-o FILE]       Fetch every search in config/searches.json, write unseen listings as JSON
       [--last DAYS]   (default data/run/new.json). --last backfills everything posted in the
                       last 1/2/3/7 days instead of stopping at the first already-seen page.
  list [FILE]          Print one compact line per listing in FILE (default data/run/new.json).
  detail URL...        Print each listing's full text (description, attributes) as JSON.
  mark-seen ID...      Record listing IDs as processed so later scans skip them.
  notify FILE          Post matches (JSON list) to the Discord webhook in DISCORD_WEBHOOK_URL.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
SEARCHES = ROOT / "config" / "searches.json"
SEEN = ROOT / "data" / "seen.json"
BASE = "https://www.nepremicnine.net"


def load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def load_seen() -> dict:
    return json.loads(SEEN.read_text()) if SEEN.exists() else {}


class Browser:
    """Headed system Chrome that gets past the Cloudflare interstitial."""

    async def __aenter__(self):
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            channel="chrome",
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--window-size=1280,900"],
        )
        self._page = await self._browser.new_page(locale="sl-SI")
        return self

    async def __aexit__(self, *exc):
        await self._browser.close()
        await self._pw.stop()

    async def get(self, url: str, wait_for: str | None = None) -> str:
        await self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        # Wait for the Cloudflare interstitial ("Just a moment" / "Trenutek") to clear.
        for _ in range(30):
            title = (await self._page.title()).lower()
            if "moment" not in title and "trenutek" not in title:
                break
            await self._page.wait_for_timeout(1000)
        else:
            raise RuntimeError(f"Cloudflare challenge did not clear for {url}")
        if wait_for:
            try:
                await self._page.wait_for_selector(wait_for, timeout=15_000)
            except Exception:
                pass  # caller decides what a missing element means
        html = await self._page.content()
        await self._page.wait_for_timeout(1500)  # be polite between requests
        return html


def listing_id(url: str) -> str | None:
    m = re.search(r"_(\d+)/?$", url)
    return m.group(1) if m else None


def text(el) -> str:
    return " ".join(el.get_text(" ", strip=True).split()) if el else ""


def parse_results(html: str, search_name: str) -> tuple[list[dict], str | None]:
    soup = BeautifulSoup(html, "html.parser")
    listings = []
    for box in soup.select("div.property-box"):
        details = box.select_one(".property-details")
        url = details.get("data-href") if details else None
        if not url or not (lid := listing_id(url)):
            continue
        price = box.select_one('meta[itemprop="price"]')
        seller = box.select_one('[itemprop="seller"] meta[itemprop="name"]')
        img = box.select_one("img[data-src]")
        listings.append(
            {
                "id": lid,
                "url": url,
                "search": search_name,
                "title": text(box.select_one("h2")),
                "type": text(details.select_one("span.font-roboto")),
                "summary": text(details.select_one('[itemprop="description"]')),
                "facts": [text(li) for li in details.select('[itemprop="disambiguatingDescription"] li')],
                "price_eur": float(price["content"]) if price and price.get("content") else None,
                "price_text": text(details.select_one("h6")),
                "seller": seller["content"] if seller else None,
                "image": img["data-src"] if img else None,
            }
        )
    nxt = soup.select_one(".paging_next a.next, a.next")
    return listings, (BASE + nxt["href"] if nxt and nxt["href"].startswith("/") else nxt and nxt["href"])


def cmd_scan(args):
    found = asyncio.run(scan(args.last))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(found, ensure_ascii=False, indent=1))
    print(f"[scan] {len(found)} new listing(s) written to {args.out}")


async def scan(last: int | None = None) -> list[dict]:
    searches = json.loads(SEARCHES.read_text())
    seen = load_seen()
    found: dict[str, dict] = {}
    async with Browser() as browser:
        for s in searches:
            url = s["url"]
            query = "?s=16"  # newest first
            if "?" in url:
                url, query = url.split("?", 1)
                query = "?" + query
            if last:
                query += f"&last={last}"
            url = url + query
            # The site's age filter bounds a backfill, so allow many more pages.
            max_pages = 40 if last else s.get("max_pages", 3)
            for n in range(1, max_pages + 1):
                listings, next_url = parse_results(await browser.get(url), s["name"])
                new = [l for l in listings if l["id"] not in seen and l["id"] not in found]
                for l in new:
                    found[l["id"]] = l
                print(f"[scan] {s['name']} p{n}: {len(listings)} listings, {len(new)} new")
                # Results are newest-first, so a page with nothing new means we've caught up.
                if not next_url or (not new and not last):
                    break
                url = next_url if "?" in next_url else next_url + query
    return list(found.values())


async def fetch_many(urls: list[str]) -> list[str]:
    async with Browser() as browser:
        pages = []
        for u in urls:
            html = await browser.get(u, wait_for=".description-details")
            if "description-details" not in html:
                html = await browser.get(u, wait_for=".description-details")  # one retry
            pages.append(html)
        return pages


def parse_detail(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "button"]):
        tag.decompose()
    main = soup.select_one(".description-details") or soup.select_one("#content")
    if not main:
        return {"id": listing_id(url), "url": url, "error": "could not find the listing description"}
    for tag in main.select("[class*=slick], [class*=gallery], [class*=slider]"):
        tag.decompose()
    return {"id": listing_id(url), "url": url, "title": text(soup.select_one("h1")), "text": text(main)[:6000]}


def cmd_detail(args):
    pages = asyncio.run(fetch_many(args.urls))
    for url, html in zip(args.urls, pages):
        print(json.dumps(parse_detail(url, html), ensure_ascii=False, indent=1))


def cmd_list(args):
    listings = json.loads(Path(args.file).read_text())
    print(f"{len(listings)} listing(s)")
    for l in listings:
        print(" | ".join([l["id"], l["type"], l["price_text"], l["title"], ", ".join(l["facts"]), l["summary"], l["url"]]))


def cmd_mark_seen(args):
    seen = load_seen()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ids = list(args.ids)
    if args.from_file:
        ids += [l["id"] for l in json.loads(Path(args.from_file).read_text())]
    for i in ids:
        seen.setdefault(i, now)
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    SEEN.write_text(json.dumps(seen, indent=1, sort_keys=True))
    print(f"marked {len(ids)} listing(s) seen ({len(seen)} total)")


def cmd_notify(args):
    load_env()
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not hook:
        sys.exit("DISCORD_WEBHOOK_URL is not set (put it in .env)")
    # Card data comes from the scan output so the model only has to supply id + reason.
    scanned = Path(args.scan)
    by_id = {l["id"]: l for l in json.loads(scanned.read_text())} if scanned.exists() else {}
    matches = [{**m, **by_id.get(m["id"], {}), "reason": m.get("reason", "")} for m in json.loads(Path(args.file).read_text())]
    for m in matches:
        embed = {
            "title": m["title"][:256],
            "url": m["url"],
            "description": m.get("reason", "")[:4000],
            "color": 0x2E7D32,
            "fields": [
                {"name": k, "value": str(v)[:1024], "inline": True}
                for k, v in (("Cena", m.get("price_text")), ("Tip", m.get("type")), ("Podatki", ", ".join(m.get("facts") or [])))
                if v
            ],
        }
        if m.get("image"):
            embed["image"] = {"url": m["image"]}  # full-width banner under the text
        payload = {"username": "Nepremičninko", "embeds": [embed]}
        req = urllib.request.Request(
            hook,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "nepremicninko/1.0"},
        )
        urllib.request.urlopen(req, timeout=20).read()
        print(f"sent {m['id']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(required=True)
    sc = sub.add_parser("scan")
    sc.add_argument("-o", "--out", default=str(ROOT / "data" / "run" / "new.json"))
    sc.add_argument("--last", type=int, choices=[1, 2, 3, 7], help="backfill listings posted in the last N days")
    sc.set_defaults(fn=cmd_scan)
    ls = sub.add_parser("list")
    ls.add_argument("file", nargs="?", default=str(ROOT / "data" / "run" / "new.json"))
    ls.set_defaults(fn=cmd_list)
    d = sub.add_parser("detail")
    d.add_argument("urls", nargs="+")
    d.set_defaults(fn=cmd_detail)
    m = sub.add_parser("mark-seen")
    m.add_argument("ids", nargs="*")
    m.add_argument("--from-file", help="JSON list of listings (e.g. scan output)")
    m.set_defaults(fn=cmd_mark_seen)
    n = sub.add_parser("notify")
    n.add_argument("file", help='JSON list of matches: [{"id": ..., "reason": ...}]')
    n.add_argument("--scan", default=str(ROOT / "data" / "run" / "new.json"), help="scan output to take card data from")
    n.set_defaults(fn=cmd_notify)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
