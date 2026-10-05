#!/usr/bin/env python3
"""
Stage 1 of the nightly sweep: crawl a public site and collect signals.

No model is involved here. This stage answers "what is stale?" using nothing
but HTTP and HTML. It is deliberately boring, deterministic and cheap, and it
is the part that needs no procurement and no API key.

Usage:
    python3 sweep.py https://www.example.gov --out sweep.json
    python3 sweep.py https://www.example.gov --max-pages 2000 --delay 0.5

Writes sweep.json, which stage 2 (triage.py) reads.
"""

import argparse
import json
import re
import sys
import time
import urllib.robotparser
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

# Identify yourself. On a government site this matters: the web team should be
# able to see in their logs exactly who you are and how to reach you.
USER_AGENT = (
    "NightlySweep/1.0 (site health audit; "
    "contact: webteam@example.gov; respects robots.txt)"
)

SKIP_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".zip", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".mp4", ".mp3", ".mov", ".avi", ".css", ".js",
)


def normalise(url):
    """Strip fragments and trailing slashes so we do not crawl the same page twice."""
    url, _ = urldefrag(url)
    if url.endswith("/") and len(urlparse(url).path) > 1:
        url = url[:-1]
    return url


def parse_lastmod(raw):
    """A sitemap <lastmod> or Last-Modified header as an aware datetime, or None."""
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M%z",
                "%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            dt = datetime.strptime(raw.strip().replace("Z", "+0000"), fmt)
        except ValueError:
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def same_site(url, root_netloc):
    return urlparse(url).netloc.lower().replace("www.", "") == root_netloc


class Sweeper:
    def __init__(self, base, max_pages=1000, delay=0.4, timeout=20, workers=8):
        self.base = normalise(base)
        self.root_netloc = urlparse(self.base).netloc.lower().replace("www.", "")
        self.max_pages = max_pages
        self.delay = delay
        self.timeout = timeout
        self.workers = workers

        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

        self.pages = {}          # url -> page record
        self.link_status = {}    # url -> status code or error string
        self.robots = self._load_robots()

    # ---------- politeness ----------

    def _load_robots(self):
        rp = urllib.robotparser.RobotFileParser()
        robots_url = urljoin(self.base, "/robots.txt")
        try:
            r = self.session.get(robots_url, timeout=self.timeout)
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
                print(f"  robots.txt loaded from {robots_url}")
            else:
                rp.parse([])
        except requests.RequestException:
            rp.parse([])
        return rp

    def allowed(self, url):
        try:
            return self.robots.can_fetch(USER_AGENT, url)
        except Exception:
            return True

    # ---------- discovery ----------

    def from_sitemap(self):
        """Sitemaps are the cheapest source of truth, and lastmod is free."""
        found = {}
        queue = [urljoin(self.base, "/sitemap.xml")]
        seen_maps = set()

        while queue:
            sm = queue.pop(0)
            if sm in seen_maps:
                continue
            seen_maps.add(sm)
            try:
                r = self.session.get(sm, timeout=self.timeout)
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.text, "xml")
            except requests.RequestException:
                continue

            # A sitemap index points at more sitemaps.
            for node in soup.find_all("sitemap"):
                loc = node.find("loc")
                if loc:
                    queue.append(loc.get_text(strip=True))

            for node in soup.find_all("url"):
                loc = node.find("loc")
                if not loc:
                    continue
                url = normalise(loc.get_text(strip=True))
                lastmod = node.find("lastmod")
                found[url] = lastmod.get_text(strip=True) if lastmod else None

        if found:
            print(f"  sitemap gave {len(found)} URLs")
        return found

    def crawl(self, seeds):
        """Breadth-first from the homepage, for sites with no usable sitemap."""
        queue = deque([self.base] + list(seeds))
        seen = set()

        while queue and len(self.pages) < self.max_pages:
            url = normalise(queue.popleft())
            if url in seen:
                continue
            seen.add(url)

            if not same_site(url, self.root_netloc):
                continue
            if url.lower().endswith(SKIP_EXTENSIONS):
                continue
            if not self.allowed(url):
                continue

            rec = self.fetch_page(url)
            if rec is None:
                continue
            self.pages[url] = rec

            for link in rec["internal_links"]:
                if link not in seen:
                    queue.append(link)

            time.sleep(self.delay)
            if len(self.pages) % 25 == 0:
                print(f"  {len(self.pages)} pages…", flush=True)

    # ---------- per-page extraction ----------

    def fetch_page(self, url):
        try:
            r = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as e:
            return {"url": url, "error": str(e), "status": None,
                    "internal_links": [], "external_links": [], "mailtos": [],
                    "forms": [], "events": [], "lastmod": None, "title": "",
                    "text": ""}

        ctype = r.headers.get("Content-Type", "")
        if "text/html" not in ctype:
            return None

        soup = BeautifulSoup(r.text, "html.parser")

        # schema.org Events carry an explicit endDate, which is the one
        # machine-readable "this is over" signal most sites already publish.
        # Read this BEFORE stripping <script>, or it disappears with them.
        events = []
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            for item in (data if isinstance(data, list) else [data]):
                if not isinstance(item, dict):
                    continue
                if "Event" in str(item.get("@type", "")):
                    events.append({
                        "name": item.get("name"),
                        "endDate": item.get("endDate") or item.get("startDate"),
                    })

        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        internal, external, mailtos = [], [], []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith("mailto:"):
                mailtos.append(href[7:].split("?")[0])
                continue
            if href.startswith(("tel:", "javascript:", "#")):
                continue
            absolute = normalise(urljoin(url, href))
            if not absolute.startswith(("http://", "https://")):
                continue
            if same_site(absolute, self.root_netloc):
                if not absolute.lower().endswith(SKIP_EXTENSIONS):
                    internal.append(absolute)
            else:
                external.append(absolute)

        forms = []
        for f in soup.find_all("form"):
            action = f.get("action") or ""
            forms.append({
                "action": urljoin(url, action) if action else url,
                "method": (f.get("method") or "get").lower(),
                "fields": len(f.find_all(["input", "select", "textarea"])),
            })

        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

        return {
            "url": url,
            "status": r.status_code,
            "title": (soup.title.get_text(strip=True) if soup.title else ""),
            "lastmod": r.headers.get("Last-Modified"),
            "internal_links": sorted(set(internal)),
            "external_links": sorted(set(external)),
            "mailtos": sorted(set(mailtos)),
            "forms": forms,
            "events": events,
            "text": text[:6000],
            "error": None,
        }

    # ---------- link checking ----------

    def check_links(self):
        targets = set()
        for rec in self.pages.values():
            targets.update(rec["internal_links"])
            targets.update(rec["external_links"])
        targets -= set(self.pages)
        print(f"  checking {len(targets)} distinct link targets…")

        def probe(u):
            try:
                r = self.session.head(u, timeout=self.timeout, allow_redirects=True)
                if r.status_code >= 400 or r.status_code == 405:
                    r = self.session.get(u, timeout=self.timeout, stream=True)
                return u, r.status_code
            except requests.RequestException as e:
                return u, type(e).__name__

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for u, status in pool.map(probe, targets):
                self.link_status[u] = status

        for url, rec in self.pages.items():
            self.link_status[url] = rec["status"]

    # ---------- signals ----------

    def signals(self, sitemap_lastmod, stale_months=18):
        cutoff = datetime.now(timezone.utc) - timedelta(days=30 * stale_months)
        out = []

        for url, rec in self.pages.items():
            dt = parse_lastmod(sitemap_lastmod.get(url) or rec.get("lastmod"))
            modified = dt.isoformat() if dt else None
            stale = bool(dt and dt < cutoff)

            broken = []
            for link in rec["internal_links"] + rec["external_links"]:
                st = self.link_status.get(link)
                if isinstance(st, str) or (isinstance(st, int) and st >= 400):
                    broken.append({"url": link, "status": st})

            expired = []
            for ev in rec["events"]:
                end = ev.get("endDate")
                if not end:
                    continue
                try:
                    dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < datetime.now(timezone.utc):
                        expired.append(ev)
                except ValueError:
                    continue

            # A page is only worth a model's attention if something is off.
            if stale or broken or expired or (rec["forms"] and stale):
                out.append({
                    "url": url,
                    "title": rec["title"],
                    "last_modified": modified,
                    "stale": stale,
                    "broken_links": broken,
                    "expired_events": expired,
                    "forms": rec["forms"],
                    "mailtos": rec["mailtos"],
                    "text": rec["text"][:3000],
                })

        out.sort(key=lambda r: (not r["stale"], r["last_modified"] or ""))
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--out", default="sweep.json")
    ap.add_argument("--max-pages", type=int, default=1000)
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("--stale-months", type=int, default=18)
    ap.add_argument("--no-link-check", action="store_true")
    args = ap.parse_args()

    print(f"Sweeping {args.base}")
    s = Sweeper(args.base, max_pages=args.max_pages, delay=args.delay)

    sitemap = s.from_sitemap()
    s.crawl(list(sitemap)[: args.max_pages])
    print(f"  fetched {len(s.pages)} pages")

    if not args.no_link_check:
        s.check_links()

    findings = s.signals(sitemap, stale_months=args.stale_months)

    payload = {
        "base": args.base,
        "swept_at": datetime.now(timezone.utc).isoformat(),
        "pages_crawled": len(s.pages),
        "links_checked": len(s.link_status),
        "findings": findings,
    }
    with open(args.out, "w") as fh:
        json.dump(payload, fh, indent=2)

    stale = sum(1 for f in findings if f["stale"])
    broken = sum(len(f["broken_links"]) for f in findings)
    expired = sum(len(f["expired_events"]) for f in findings)
    print(f"\n  {len(findings)} pages flagged")
    print(f"    {stale} not touched in {args.stale_months}+ months")
    print(f"    {broken} broken links")
    print(f"    {expired} expired events still published")
    print(f"\n  wrote {args.out}  →  now run: python3 triage.py {args.out}")


if __name__ == "__main__":
    sys.exit(main())
