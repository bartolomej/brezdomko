# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright>=1.47", "beautifulsoup4>=4.12"]
# ///
"""nepremicnine.net helper used by the nepremicnine-scout skill.

nepremicnine.net sits behind a Cloudflare challenge that blocks plain HTTP clients and
headless Chrome, so pages are loaded in a headed system Chrome (wrap in `xvfb-run -a`
when there is no display, e.g. from cron).

Each alert lives in config/alerts/<name>/ (alert.json: Discord webhook + searches;
criteria.md: requirements) and keeps its state in data/<name>/. Every command except
`alerts` needs --alert NAME; paths below are relative to data/<name>/.

Commands:
  alerts               List configured alerts and whether they're ready to run.
  scan [--last DAYS]   Fetch the alert's searches, write unseen listings to run/new.json.
                       --last backfills everything posted in the last 1/2/3/7 days instead of
                       stopping at the first already-seen page.
  list                 Print one compact line per listing in run/new.json.
  detail URL...        Print each listing's full text (description, attributes) as JSON.
       [--photos N]    Also save the first N gallery photos to run/photos/<id>/.
  mark-seen ID...      Record listing IDs as processed (seen.json) so later scans skip them.
       [--from-file F]
  notify FILE          Post matches ([{"id", "reason"}]) to the alert's Discord webhook.
"""

import argparse
import asyncio
import json
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[3]  # repo root (.claude/skills/<name>/nep.py)
ALERTS = ROOT / "config" / "alerts"
BASE = "https://www.nepremicnine.net"


class Alert:
    """One alert: its config in config/alerts/<name>/ and its state in data/<name>/."""

    def __init__(self, name: str):
        self.name = name
        self.config_dir = ALERTS / name
        if not (self.config_dir / "alert.json").exists():
            sys.exit(f"unknown alert {name!r}: {self.config_dir / 'alert.json'} not found")
        self.config = json.loads((self.config_dir / "alert.json").read_text())
        self.criteria = self.config_dir / "criteria.md"
        data = ROOT / "data" / name
        self.seen_file = data / "seen.json"
        self.run = data / "run"
        self.new_file = self.run / "new.json"
        self.photos = self.run / "photos"

    @property
    def webhook(self) -> str:
        return self.config.get("discord_webhook_url", "")

    def problems(self) -> list[str]:
        out = []
        if not self.webhook.startswith("https://discord.com/api/webhooks/"):
            out.append("no discord_webhook_url in alert.json")
        if not self.config.get("searches"):
            out.append("no searches in alert.json")
        if not self.criteria.exists():
            out.append("criteria.md missing")
        return out

    def seen(self) -> dict:
        return json.loads(self.seen_file.read_text()) if self.seen_file.exists() else {}


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
                # Listings without photos get a relative placeholder (/images/n-1.jpg).
                "image": img["data-src"] if img and img["data-src"].startswith("https://") else None,
            }
        )
    nxt = soup.select_one(".paging_next a.next, a.next")
    return listings, (BASE + nxt["href"] if nxt and nxt["href"].startswith("/") else nxt and nxt["href"])


def cmd_alerts(args):
    names = sorted(d.name for d in ALERTS.iterdir() if (d / "alert.json").exists() and d.name != "example")
    for name in names:
        problems = Alert(name).problems()
        print(f"{name}\t{'ready' if not problems else 'not ready: ' + '; '.join(problems)}")


def cmd_scan(args):
    alert = Alert(args.alert)
    found = asyncio.run(scan(alert, args.last))
    shutil.rmtree(alert.photos, ignore_errors=True)  # photos are per-run scratch
    alert.run.mkdir(parents=True, exist_ok=True)
    alert.new_file.write_text(json.dumps(found, ensure_ascii=False, indent=1))
    print(f"[scan] {len(found)} new listing(s) written to {alert.new_file.relative_to(ROOT)}")


async def scan(alert: Alert, last: int | None = None) -> list[dict]:
    searches = alert.config["searches"]
    seen = alert.seen()
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
    lid = listing_id(url)
    gallery = []
    for a in soup.select(f'a[data-fancybox="gallery_{lid}"]'):
        src = a.get("data-src") or a.get("href")
        if src and src.startswith("https://img.nepremicnine.net/") and src not in gallery:
            gallery.append(src)
    for tag in soup(["script", "style", "noscript", "button"]):
        tag.decompose()
    main = soup.select_one(".description-details") or soup.select_one("#content")
    if not main:
        return {"id": listing_id(url), "url": url, "error": "could not find the listing description"}
    for tag in main.select("[class*=slick], [class*=gallery], [class*=slider]"):
        tag.decompose()
    return {"id": lid, "url": url, "title": text(soup.select_one("h1")), "text": text(main)[:6000], "gallery": gallery}


def download_photos(dest_dir: Path, lid: str, urls: list[str]) -> list[str]:
    """Save photos to run/photos/<id>/ so Claude can view them with its Read tool."""
    out = dest_dir / lid
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, u in enumerate(urls, 1):
        dest = out / f"{i:02d}.jpg"
        if not dest.exists():
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0", "Referer": BASE + "/"})
            dest.write_bytes(urllib.request.urlopen(req, timeout=20).read())
        paths.append(str(dest.relative_to(ROOT)))
    return paths


def cmd_detail(args):
    alert = Alert(args.alert)
    pages = asyncio.run(fetch_many(args.urls))
    for url, html in zip(args.urls, pages):
        d = parse_detail(url, html)
        gallery = d.pop("gallery", [])
        d["photo_count"] = len(gallery)
        if args.photos and gallery:
            try:
                d["photos"] = download_photos(alert.photos, d["id"], gallery[: args.photos])
            except Exception as e:
                d["photos_error"] = str(e)
        print(json.dumps(d, ensure_ascii=False, indent=1))


def cmd_list(args):
    listings = json.loads(Alert(args.alert).new_file.read_text())
    print(f"{len(listings)} listing(s)")
    for l in listings:
        print(" | ".join([l["id"], l["type"], l["price_text"], l["title"], ", ".join(l["facts"]), l["summary"], l["url"]]))


def cmd_mark_seen(args):
    alert = Alert(args.alert)
    seen = alert.seen()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ids = list(args.ids)
    if args.from_file:
        ids += [l["id"] for l in json.loads(Path(args.from_file).read_text())]
    for i in ids:
        seen.setdefault(i, now)
    alert.seen_file.parent.mkdir(parents=True, exist_ok=True)
    alert.seen_file.write_text(json.dumps(seen, indent=1, sort_keys=True))
    print(f"marked {len(ids)} listing(s) seen ({len(seen)} total)")


def cmd_notify(args):
    alert = Alert(args.alert)
    hook = alert.webhook
    if not hook:
        sys.exit(f"no discord_webhook_url in {alert.config_dir / 'alert.json'}")
    # Card data comes from the scan output so the model only has to supply id + reason.
    scanned = alert.new_file
    by_id = {l["id"]: l for l in json.loads(scanned.read_text())} if scanned.exists() else {}
    matches = [{**m, **by_id.get(m["id"], {}), "reason": m.get("reason", "")} for m in json.loads(Path(args.file).read_text())]
    failed = []
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
        if (m.get("image") or "").startswith("https://"):
            embed["image"] = {"url": m["image"]}  # full-width banner under the text
        payload = {"username": "Nepremičninko", "embeds": [embed]}
        req = urllib.request.Request(
            hook,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "nepremicninko/1.0"},
        )
        for attempt in range(3):
            try:
                urllib.request.urlopen(req, timeout=20).read()
                print(f"sent {m['id']}")
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:  # rate limited: wait as Discord asks
                    time.sleep(float(e.headers.get("Retry-After", 2)))
                    continue
                failed.append(m["id"])
                print(f"FAILED {m['id']}: HTTP {e.code} {e.read()[:300]!r}")
                break
            except Exception as e:
                failed.append(m["id"])
                print(f"FAILED {m['id']}: {e}")
                break
        time.sleep(0.5)
    if failed:
        sys.exit(f"{len(failed)} of {len(matches)} failed to send: {' '.join(failed)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-a", "--alert", help="alert name (a folder in config/alerts/)")
    sub = ap.add_subparsers(required=True)
    sub.add_parser("alerts").set_defaults(fn=cmd_alerts)
    sc = sub.add_parser("scan")
    sc.add_argument("--last", type=int, choices=[1, 2, 3, 7], help="backfill listings posted in the last N days")
    sc.set_defaults(fn=cmd_scan)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    d = sub.add_parser("detail")
    d.add_argument("urls", nargs="+")
    d.add_argument("--photos", type=int, default=0, metavar="N", help="also download the first N gallery photos")
    d.set_defaults(fn=cmd_detail)
    m = sub.add_parser("mark-seen")
    m.add_argument("ids", nargs="*")
    m.add_argument("--from-file", help="JSON list of listings (e.g. scan output)")
    m.set_defaults(fn=cmd_mark_seen)
    n = sub.add_parser("notify")
    n.add_argument("file", help='JSON list of matches: [{"id": ..., "reason": ...}]')
    n.set_defaults(fn=cmd_notify)
    args = ap.parse_args()
    if args.fn is not cmd_alerts and not args.alert:
        ap.error("--alert NAME is required (see the `alerts` command)")
    args.fn(args)


if __name__ == "__main__":
    main()
