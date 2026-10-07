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
                       (default data/run/new.json).
  detail URL           Print a listing's full text (description, attributes) as JSON.
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

    async def get(self, url: str) -> str:
        await self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        # Wait for the Cloudflare interstitial ("Just a moment" / "Trenutek") to clear.
        for _ in range(30):
            title = (await self._page.title()).lower()
            if "moment" not in title and "trenutek" not in title:
                break
            await self._page.wait_for_timeout(1000)
        else:
            raise RuntimeError(f"Cloudflare challenge did not clear for {url}")
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
    found = asyncio.run(scan())
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(found, ensure_ascii=False, indent=1))
    print(f"[scan] {len(found)} new listing(s) written to {args.out}")


async def scan() -> list[dict]:
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
            url = url + query
            for n in range(1, s.get("max_pages", 3) + 1):
                listings, next_url = parse_results(await browser.get(url), s["name"])
                new = [l for l in listings if l["id"] not in seen and l["id"] not in found]
                for l in new:
                    found[l["id"]] = l
                print(f"[scan] {s['name']} p{n}: {len(listings)} listings, {len(new)} new")
                # Results are newest-first, so a page with nothing new means we've caught up.
                if not next_url or not new:
                    break
                url = next_url if "?" in next_url else next_url + query
    return list(found.values())


async def fetch_one(url: str) -> str:
    async with Browser() as browser:
        return await browser.get(url)


def cmd_detail(args):
    html = asyncio.run(fetch_one(args.url))
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "button"]):
        tag.decompose()
    main = soup.select_one(".description-details") or soup.select_one("#content")
    if not main:
        sys.exit("could not find the listing description on the page")
    for tag in main.select("[class*=slick], [class*=gallery], [class*=slider]"):
        tag.decompose()
    body = text(main)
    json.dump(
        {
            "id": listing_id(args.url),
            "url": args.url,
            "title": text(soup.select_one("h1")),
            "text": body[:12000],
        },
        sys.stdout,
        ensure_ascii=False,
        indent=1,
    )
    print()


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
    matches = json.loads(Path(args.file).read_text())
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
            embed["thumbnail"] = {"url": m["image"]}
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
    sc.set_defaults(fn=cmd_scan)
    d = sub.add_parser("detail")
    d.add_argument("url")
    d.set_defaults(fn=cmd_detail)
    m = sub.add_parser("mark-seen")
    m.add_argument("ids", nargs="*")
    m.add_argument("--from-file", help="JSON list of listings (e.g. scan output)")
    m.set_defaults(fn=cmd_mark_seen)
    n = sub.add_parser("notify")
    n.add_argument("file", help="JSON list of matches: scan entries plus a 'reason' field")
    n.set_defaults(fn=cmd_notify)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
