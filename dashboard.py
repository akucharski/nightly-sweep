#!/usr/bin/env python3
"""
A local dashboard for the nightly sweep: saved projects, live runs, run history.

It drives the same code as sweep.py and triage.py, in-process, so it can show
each page as it is fetched and each verdict as it arrives. On top of those it
adds two analyses: outdated wording (past dates still written as upcoming) and
contradictions (related pages that state incompatible facts). Like the scripts,
it only reads the site. Nothing is changed.

Usage:
    python3 dashboard.py            # then open http://127.0.0.1:8765
    python3 dashboard.py --port 9000

Projects and every run's results are saved under ./projects (see store.py).
"""

import argparse
import hashlib
import html
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

import requests

import analysis
import auth
import store
import triage
import throttle
from sweep import USER_AGENT, Sweeper, parse_lastmod

HERE = Path(__file__).resolve().parent
STAGES = ["discover", "crawl", "links", "signals", "triage", "contradictions", "digest"]

# A page that answers with one of these is gone, not crawled.
DEAD_STATUSES = (403, 404)


def provider_of(o):
    """The AI provider config for a project's settings."""
    if o["provider"] == "custom":
        return triage.custom_provider(o["custom_url"], o["custom_model"], o["custom_key_env"])
    if o["provider"] in triage.COMPATIBLE:
        return triage.compatible_provider(o["provider"], o["custom_model"])
    return triage.resolve(o["provider"])


def ai_problem(cfg):
    """Why the AI steps can't run with this provider, or None."""
    if not cfg["url"]:
        return "The custom provider needs an endpoint URL"
    if not cfg["model"]:
        return "Choose a model for the AI provider"
    return key_problem(cfg)


def key_problem(cfg):
    """Built-in providers may use the environment; every other key must be in .env."""
    builtin = cfg["name"] in triage.PROVIDERS
    if cfg["key_env"] and (not os.environ.get(cfg["key_env"]) or (not builtin and cfg["key_env"] not in triage.ENV_FILE_KEYS)):
        return f"No {cfg['key_env']} set in .env"
    return None


def env_keys():
    """Names (never values) of .env keys a custom provider may use, for the form."""
    return sorted(k for k in triage.ENV_FILE_KEYS
                  if os.environ.get(k) and triage.CUSTOM_KEY_RE.match(k) and k not in triage.RESERVED_KEYS)


def analysis_key(finding, cfg):
    """Fingerprint of everything the model is shown about a page. If it matches the
    last analysis, the verdict still holds. Passed dates are part of it, so an
    unchanged page whose deadline has just gone by is looked at again."""
    material = [
        cfg["name"], cfg["url"], cfg["model"], triage.SYSTEM,
        finding["title"], finding["text"], len(finding["forms"]), finding["mailtos"][:5],
        [m["date"] for m in finding["outdated_mentions"]],
        [[e.get("name"), e.get("endDate")] for e in finding["expired_events"]],
    ]
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def has_key(provider):
    return bool(os.environ.get(triage.PROVIDERS[provider]["key_env"]))


class Run:
    """All state for one run. The browser polls snapshot(); result() is what gets saved."""

    def __init__(self, project, run_id):
        self.project_id = project["id"]
        self.project_name = project["name"]
        self.run_id = run_id
        self.opts = {"site": project["site"], **project["settings"]}
        self.pace = throttle.profile(project["settings"])
        try:
            self.model = provider_of(self.opts)["model"]
        except (ValueError, KeyError):
            self.model = ""
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.status = "running"          # running | done | failed | cancelled
        self.saved = False
        self.error = None
        self.started = time.time()
        self.started_iso = datetime.now(timezone.utc).isoformat()
        self.finished = None
        self.stage = "discover"
        self.stage_state = {s: "pending" for s in STAGES}
        self.counts = {
            # pages = valid pages only; dead_pages = 403/404; failed = other errors
            "fetched": 0, "pages": 0, "dead_pages": 0, "failed": 0,
            "sitemap_urls": 0, "links_total": 0, "links_done": 0,
            "flagged": 0, "stale": 0, "outdated": 0, "broken": 0, "expired": 0,
            "batches_total": 0, "batches_done": 0, "dead": 0, "unclear": 0, "live": 0, "reused": 0,
            "groups_total": 0, "groups_done": 0, "contradictions": 0, "contra_dropped": 0,
            "pauses": 0, "backoffs": 0,
        }
        self.log = []                    # [{t, kind, msg}]
        self.findings = []               # flagged pages, slimmed for the UI
        self.verdicts = {}               # url -> verdict dict
        self.dead_pages = []             # [{url, status, linked_from}]
        self.inventory = []              # every valid page: age, section, staleness
        self.groups = []                 # [{topic, urls}] compared for contradictions
        self.contradictions = []
        self.digest_text = None

    # ---------- bookkeeping ----------

    def emit(self, kind, msg):
        with self.lock:
            self.log.append({"t": time.time(), "kind": kind, "msg": msg})
            del self.log[:-400]

    def on_pace(self, kind, msg):
        if kind == "pause":
            self.bump("pauses")
        elif kind == "slow":
            self.bump("backoffs")
        self.emit("warn" if kind == "slow" else "pace", msg)

    def enter(self, stage):
        with self.lock:
            if self.stage_state.get(self.stage) == "active":
                self.stage_state[self.stage] = "done"
            self.stage = stage
            self.stage_state[stage] = "active"

    def skip(self, stage):
        with self.lock:
            self.stage_state[stage] = "skipped"

    def bump(self, key, n=1):
        with self.lock:
            self.counts[key] += n

    def check(self):
        if self.cancel.is_set():
            raise throttle.Cancelled()

    def snapshot(self, since=0, inventory=True):
        with self.lock:
            return {
                "project_id": self.project_id,
                "project_name": self.project_name,
                "run_id": self.run_id,
                "status": self.status,
                "saved": self.saved,
                "error": self.error,
                "opts": self.opts,
                "pace": self.pace["label"],
                "model": self.model,
                "started": self.started_iso,
                "elapsed": (self.finished or time.time()) - self.started,
                "stage": self.stage,
                "stages": self.stage_state,
                "counts": dict(self.counts),
                "log": [e for e in self.log if e["t"] > since],
                "findings": self.findings,
                "verdicts": self.verdicts,
                "dead_pages": self.dead_pages,
                "groups": self.groups,
                "contradictions": self.contradictions,
                "inv_len": len(self.inventory),
                "inventory": self.inventory if inventory else None,
                "digest": bool(self.digest_text),
            }

    def summary(self):
        c = self.counts
        return {
            "id": self.run_id, "site": self.opts["site"], "started": self.started_iso,
            "elapsed": (self.finished or time.time()) - self.started, "status": self.status,
            "counts": {k: c[k] for k in ("pages", "stale", "outdated", "dead_pages", "broken",
                                          "expired", "contradictions", "flagged", "dead", "unclear", "reused")},
        }


class LiveSweeper(Sweeper):
    """Sweeper that paces its requests, reports progress to a Run, and can be stopped."""

    def __init__(self, run):
        self.run = run
        o = run.opts
        super().__init__(o["site"], max_pages=o["max_pages"], delay=0)  # the pacer replaces delay
        floor = 0.0
        try:
            floor = float(self.robots.crawl_delay(USER_AGENT) or 0)
        except (TypeError, ValueError, AttributeError):
            pass
        if floor:
            run.emit("info", f"robots.txt asks for {floor:g}s between requests; honouring it")
        pacer = throttle.Pacer(self.root_netloc, run.pace, floor=floor,
                               cancel=run.cancel, on_event=run.on_pace)
        pacer.after(self.root_netloc, 200)   # robots.txt was just fetched; space the next request
        session = throttle.PacedSession(pacer)
        session.headers.update(self.session.headers)
        self.session = session

    def fetch_page(self, url):
        self.run.check()
        rec = super().fetch_page(url)
        if rec is not None:
            self.run.bump("fetched")
            st = rec.get("status")
            if st in DEAD_STATUSES:
                self.run.bump("dead_pages")
                self.run.emit("bad", f"DEAD {st}  {url}")
            elif rec.get("error") or not isinstance(st, int) or st >= 400:
                self.run.bump("failed")
                self.run.emit("warn", f"FAILED {st or 'no response'}  {url}")
            else:
                self.run.bump("pages")
                self.run.emit("page", f"{st}  {url}")
        return rec

    def check_links(self):
        # Same as Sweeper.check_links, with a progress counter and a stop check.
        targets = set()
        for rec in self.pages.values():
            targets.update(rec["internal_links"])
            targets.update(rec["external_links"])
        targets -= set(self.pages)
        with self.run.lock:
            self.run.counts["links_total"] = len(targets)
        self.run.emit("info", f"Checking {len(targets)} distinct link targets")

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
                self.run.bump("links_done")
                if isinstance(status, str) or status >= 400:
                    self.run.emit("bad", f"broken link {status}  {u}")
        self.run.check()

        for url, rec in self.pages.items():
            self.link_status[url] = rec["status"]


def build_digest(sweep, verdicts, o, dead_pages, findings, contradictions):
    text, _, _ = triage.digest(sweep, verdicts, o["stale_months"])
    extra = []
    outdated = [f for f in findings if f["outdated_mentions"]]
    if outdated:
        extra += [f"## Outdated wording ({len(outdated)} pages)", "",
                  "Past dates still written as if they are ahead of the reader.", ""]
        for f in outdated:
            m = f["outdated_mentions"][0]
            more = len(f["outdated_mentions"]) - 1
            extra.append(f"- [{f['title'][:60] or f['url']}]({f['url']}) — \"{m['quote']}\""
                         + (f" (+{more} more)" if more else ""))
        extra.append("")
    if contradictions:
        extra += [f"## Contradictions ({len(contradictions)})", ""]
        for c in sorted(contradictions, key=lambda c: ("high", "medium", "low").index(c["severity"])):
            extra.append(f"- **{c['topic']}** ({c['severity']}): \"{c['quote_a']}\" "
                         f"([{c['title_a'][:40] or 'page'}]({c['page_a']})) vs \"{c['quote_b']}\" "
                         f"([{c['title_b'][:40] or 'page'}]({c['page_b']})). {c['explanation']}")
        extra.append("")
    if dead_pages:
        extra += [f"## Dead pages ({len(dead_pages)})", "",
                  "These returned 404 or 403. Fix or remove the links that point at them.", ""]
        for d in dead_pages:
            n = len(d["linked_from"])
            extra.append(f"- `{d['status']}` {d['url']} — linked from {n} page{'s' if n != 1 else ''}")
        extra.append("")
    if extra:
        text = text.replace("\n---\n", "\n" + "\n".join(extra) + "\n---\n", 1)
    return text


def execute(run):
    o = run.opts
    out = store.run_dir(run.project_id, run.run_id)
    final = "failed"
    try:
        cfg = provider_of(o)
        problem = ai_problem(cfg)
        ai_ok = not problem
        if (o["ai"] or o["contradictions"]) and problem:
            run.emit("warn", f"{problem}; "
                             "only earlier verdicts for unchanged pages can be reused, and the contradiction check is skipped")

        # 1. Discover
        run.enter("discover")
        run.emit("info", f"Sweeping {o['site']} at {run.pace['label']} pace")
        s = LiveSweeper(run)
        sitemap = s.from_sitemap()
        with run.lock:
            run.counts["sitemap_urls"] = len(sitemap)
        run.emit("info", f"Sitemap gave {len(sitemap)} URLs" if sitemap
                 else "No usable sitemap; crawling from the homepage")
        run.check()

        # 2. Crawl
        run.enter("crawl")
        s.crawl(list(sitemap)[: o["max_pages"]])
        run.emit("info", f"Fetched {len(s.pages)} pages")

        # 3. Links
        if o["link_check"]:
            run.enter("links")
            s.check_links()
        else:
            run.skip("links")

        # 4. Signals: staleness, outdated wording, dead pages
        run.enter("signals")
        swept_at = datetime.now(timezone.utc)
        valid = {u for u, r in s.pages.items()
                 if isinstance(r.get("status"), int) and r["status"] < 400 and not r.get("error")}
        dead_pages = [{
            "url": u,
            "status": r["status"],
            "linked_from": sorted(p for p in valid if u in s.pages[p]["internal_links"]),
        } for u, r in s.pages.items() if r.get("status") in DEAD_STATUSES]

        cutoff_days = 30 * o["stale_months"]
        inventory, mentions = {}, {}
        for u in sorted(valid):
            rec = s.pages[u]
            dt = parse_lastmod(sitemap.get(u) or rec.get("lastmod"))
            age = (swept_at - dt).days if dt else None
            m = analysis.outdated_mentions(rec["text"], swept_at)
            if m:
                mentions[u] = m
            inventory[u] = {
                "url": u, "title": rec["title"], "section": analysis.section_of(u),
                "last_modified": dt.isoformat() if dt else None, "age_days": age,
                "stale": age is not None and age > cutoff_days, "outdated": len(m),
            }

        # Dead and failed pages are reported on their own, not as content to judge.
        findings = [f for f in s.signals(sitemap, stale_months=o["stale_months"]) if f["url"] in valid]
        flagged = {f["url"] for f in findings}
        for f in findings:
            f["outdated_mentions"] = mentions.get(f["url"], [])
        for u, m in mentions.items():
            if u not in flagged:
                rec, inv = s.pages[u], inventory[u]
                findings.append({
                    "url": u, "title": rec["title"], "last_modified": inv["last_modified"],
                    "stale": inv["stale"], "broken_links": [], "expired_events": [],
                    "forms": rec["forms"], "mailtos": rec["mailtos"], "text": rec["text"][:3000],
                    "outdated_mentions": m,
                })
        # Pages that say something has ended go to the model first.
        findings.sort(key=lambda f: (not (f["expired_events"] or f["outdated_mentions"]),
                                     not f["stale"], f["last_modified"] or ""))

        sweep = {
            "base": o["site"],
            "swept_at": swept_at.isoformat(),
            "pages_crawled": len(valid),
            "links_checked": len(s.link_status),
            "dead_pages": dead_pages,
            "findings": findings,
        }
        (out / "sweep.json").write_text(json.dumps(sweep, indent=2))
        with run.lock:
            run.dead_pages = dead_pages
            run.inventory = list(inventory.values())
            run.findings = [{
                "url": f["url"],
                "title": f["title"],
                "section": analysis.section_of(f["url"]),
                "last_modified": f["last_modified"],
                "age_days": inventory[f["url"]]["age_days"],
                "stale": f["stale"],
                "broken_links": f["broken_links"],
                "expired_events": f["expired_events"],
                "outdated_mentions": f["outdated_mentions"],
                "forms": len(f["forms"]),
            } for f in findings]
            run.counts["flagged"] = len(findings)
            run.counts["stale"] = sum(1 for i in inventory.values() if i["stale"])
            run.counts["outdated"] = len(mentions)
            run.counts["broken"] = sum(len(f["broken_links"]) for f in findings)
            run.counts["expired"] = sum(len(f["expired_events"]) for f in findings)
        run.emit("flag", f"{len(findings)} pages flagged: {run.counts['stale']} stale, "
                         f"{len(mentions)} with outdated wording")

        # 5. Triage. A page the model has already judged, unchanged since, keeps its verdict.
        today = swept_at.strftime("%Y-%m-%d")
        verdicts = []
        cache = store.load_analysis(run.project_id) if o["reuse_analysis"] else {}
        candidates = findings[: o["limit"]] if o["ai"] else []
        keys = {f["url"]: analysis_key(f, cfg) for f in candidates}
        reuse = [f for f in candidates if cache.get(f["url"], {}).get("key") == keys[f["url"]]]
        fresh = [f for f in candidates if f not in reuse] if ai_ok else []
        if reuse or fresh:
            run.enter("triage")

            def record(v, reused_from=None):
                verdict = v.get("verdict", "unclear")
                v = {**v, "reused_from": reused_from} if reused_from else v
                verdicts.append(v)
                with run.lock:
                    run.verdicts[v["url"]] = v
                    if verdict in ("dead", "unclear", "live"):
                        run.counts[verdict] += 1
                    if reused_from:
                        run.counts["reused"] += 1
                return verdict

            for f in reuse:
                c = cache[f["url"]]
                record({**c["verdict"], "url": f["url"]}, reused_from=c["analyzed_at"])
            if reuse:
                run.emit("ok", f"Reused {len(reuse)} earlier verdict{'s' if len(reuse) > 1 else ''} "
                               "for pages unchanged since they were analysed")
            skipped = len(candidates) - len(reuse) - len(fresh)
            if skipped:
                run.emit("warn", f"{skipped} changed or new pages need a fresh look, but there is no API key")

            batches = [fresh[i:i + 8] for i in range(0, len(fresh), 8)]
            with run.lock:
                run.counts["batches_total"] = len(batches)
            for n, batch in enumerate(batches, 1):
                run.check()
                run.emit("info", f"Asking {cfg['model']} about batch {n}/{len(batches)} "
                                 f"({len(batch)} new or changed pages)")
                try:
                    got = triage.call_model(cfg, triage.build_prompt(batch, today))
                except requests.RequestException as e:
                    run.emit("warn", f"Batch {n} skipped: {e}")
                    got = []
                for v in got:
                    if not isinstance(v, dict) or "url" not in v:
                        continue
                    verdict = record(v)
                    if v["url"] in keys:
                        cache[v["url"]] = {
                            "key": keys[v["url"]], "analyzed_at": swept_at.isoformat(), "run_id": run.run_id,
                            "verdict": {k: v.get(k) for k in ("verdict", "reason", "evidence")},
                        }
                    kind = {"dead": "bad", "unclear": "warn"}.get(verdict, "ok")
                    run.emit(kind, f"{verdict.upper()}  {v['url']} — {v.get('reason', '')}")
                run.bump("batches_done")
                store.save_analysis(run.project_id, cache)   # keep what we paid for, even if stopped
        else:
            run.skip("triage")

        # 6. Contradictions
        contradictions = []
        if o["contradictions"] and ai_ok:
            run.enter("contradictions")
            pages = [{"url": u, "title": s.pages[u]["title"], "text": s.pages[u]["text"],
                      "last_modified": inventory[u]["last_modified"]} for u in sorted(valid)]
            groups = analysis.related_groups(pages, max_groups=o["contra_groups"])
            with run.lock:
                run.counts["groups_total"] = len(groups)
                run.groups = [{"topic": g["topic"], "urls": [p["url"] for p in g["pages"]]}
                              for g in groups]
            run.emit("info", f"Comparing {len(groups)} groups of related pages" if groups
                     else "No pages similar enough to compare")
            for n, g in enumerate(groups, 1):
                run.check()
                run.emit("info", f"Group {n}/{len(groups)}: {g['topic']} ({len(g['pages'])} pages)")
                try:
                    raw = triage.call_model(cfg, analysis.contradiction_prompt(g, today),
                                            system=analysis.CONTRADICTION_SYSTEM)
                except requests.RequestException as e:
                    run.emit("warn", f"Group {n} skipped: {e}")
                    raw = []
                found, dropped = analysis.verify_contradictions(raw, g)
                contradictions += found
                with run.lock:
                    run.contradictions = list(contradictions)
                    run.counts["contradictions"] = len(contradictions)
                    run.counts["contra_dropped"] += dropped
                    run.counts["groups_done"] += 1
                for c in found:
                    run.emit("bad" if c["severity"] == "high" else "warn",
                             f"CONTRADICTION ({c['severity']}) {c['topic']}: {c['page_a']} vs {c['page_b']}")
                if dropped:
                    run.emit("pace", f"Discarded {dropped} claim{'s' if dropped > 1 else ''} "
                                     "whose quotes were not on the page")
        else:
            run.skip("contradictions")

        # 7. Digest
        run.enter("digest")
        text = build_digest(sweep, verdicts, o, dead_pages, findings, contradictions)
        (out / "digest.md").write_text(text)
        with run.lock:
            run.digest_text = text
            run.stage_state["digest"] = "done"
        run.emit("ok", "Done.")
        final = "done"

    except throttle.Cancelled:
        final = "cancelled"
        with run.lock:
            run.stage_state[run.stage] = "stopped"
        run.emit("warn", "Run stopped")
    except SystemExit as e:          # triage.call_model exits when the key is missing
        run.error = str(e)
        with run.lock:
            run.stage_state[run.stage] = "failed"
        run.emit("bad", str(e))
    except Exception as e:
        run.error = f"{type(e).__name__}: {e}"
        with run.lock:
            run.stage_state[run.stage] = "failed"
        run.emit("bad", run.error)
    finally:
        run.finished = time.time()
        with run.lock:
            run.status = final
        try:
            result = run.snapshot()
            result["digest_text"] = run.digest_text
            store.save_run(run.project_id, run.run_id, result, run.summary())
        except Exception as e:      # never leave the UI waiting on a save that failed
            run.emit("bad", f"Could not save run: {e}")
        with run.lock:
            run.saved = True


CURRENT = {"run": None}
CONFIG = {"redirect": "http://127.0.0.1:8765/auth/google/callback"}
COOKIE, OAUTH_COOKIE = "sweep_session", "sweep_oauth"
PUBLIC = ("/", "/about", "/login", "/logout", "/auth/google", "/auth/google/callback", "/auth/link", "/favicon.ico")
HOME = "/dashboard"     # where signing in takes you; "/" is the public product page
STATIC = HERE / "static"
STATIC_TYPES = {".woff2": "font/woff2", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
                ".png": "image/png", ".txt": "text/plain; charset=utf-8"}


def login_page(error=None, notice=None, username="", next_url=HOME):
    google = auth.google_config(CONFIG["redirect"])
    has_users = auth.has_users()
    e = html.escape
    msg = (f'<div class="msg error" role="alert" tabindex="-1" id="err">{e(error)}</div>' if error else
           f'<div class="msg notice" role="status" tabindex="-1">{e(notice)}</div>' if notice else "")
    invalid = ' aria-invalid="true" aria-describedby="err"' if error else ""
    form = "" if not has_users else f"""
  <form method="post" action="/login">
    <input type="hidden" name="next" value="{e(next_url)}">
    <label for="username">Username</label>
    <input id="username" name="username" autocomplete="username" autocapitalize="none" spellcheck="false" required value="{e(username)}"{invalid}{"" if username else " autofocus"}>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required{invalid}{" autofocus" if username else ""}>
    <button type="submit">Sign in</button>
  </form>"""
    g = ""
    if google["enabled"]:
        g = (('<p class="or">or</p>' if form else "") +
             f'<a class="google" href="/auth/google?next={e(quote(next_url))}">'
             '<svg aria-hidden="true" width="18" height="18" viewBox="0 0 48 48"><path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.7 32.7 29.2 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.4-.4-3.5z"/><path fill="#FF3D00" d="m6.3 14.7 6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z"/><path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.2 0-9.6-3.3-11.3-7.9l-6.5 5C9.5 39.6 16.2 44 24 44z"/><path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C37 39.2 44 34 44 24c0-1.3-.1-2.4-.4-3.5z"/></svg>'
             "Sign in with Google</a>")
    setup = "" if has_users or google["enabled"] else (
        '<div class="setup"><p><strong>No accounts yet.</strong> Create the first one from a terminal in the project folder:</p>'
        "<p><code>.venv/bin/python manage.py add-user yourname --admin</code></p><p>Then reload this page.</p></div>")
    page = (HERE / "login.html").read_text()
    for k, v in (("{{MESSAGE}}", msg), ("{{FORM}}", form), ("{{GOOGLE}}", g), ("{{SETUP}}", setup)):
        page = page.replace(k, v)
    return page


def landing_page(user):
    """The public product page; the top-right button depends on whether you're signed in."""
    button = ('<a class="btn btn-navy" href="/dashboard">Open dashboard</a>' if user
              else '<a class="btn btn-navy" href="/login">Sign in</a>')
    return (HERE / "landing.html").read_text().replace("<!--ACCOUNT-->", button)


def safe_next(url):
    return url if url and url.startswith("/") and not url.startswith("//") and "\\" not in url else HOME


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", headers=(), cache="no-store"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        # same-origin, not no-referrer: with no-referrer browsers label our own form posts
        # "Origin: null", which the cross-site check below would refuse. Addresses (and the
        # tokens in one-time sign-in links) still never go to other sites.
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location, headers=()):
        self.send(303, b"", "text/plain", [("Location", location), *headers])

    def cookie(self, name):
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except cookies.CookieError:
            return None
        return jar[name].value if name in jar else None

    def set_cookie(self, name, value, max_age):
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" or os.environ.get("COOKIE_SECURE") else ""
        return ("Set-Cookie", f"{name}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}")

    def sign_in(self, username, method, next_url):
        token = auth.create_session(username, method)
        self.redirect(safe_next(next_url), [self.set_cookie(COOKIE, token, auth.SESSION_MAX)])

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if (self.headers.get("Content-Type") or "").startswith("application/x-www-form-urlencoded"):
            return {k: v[0] for k, v in parse_qs(raw.decode()).items()}
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def same_origin(self):
        """Refuse posts that a browser says came from another site. Browsers send Origin on
        every POST; when it is "null" (privacy settings, some redirects) fall back to Referer.
        Requests with neither, such as curl, are not browser cross-site requests."""
        host = self.headers.get("Host")
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            return urlparse(origin).netloc == host
        referer = self.headers.get("Referer")
        if referer:
            return urlparse(referer).netloc == host
        return origin != "null"

    def running(self):
        run = CURRENT["run"]
        return run if run and not run.saved else None

    def gate(self, path):
        """The signed-in user, or None after answering with a redirect or 401."""
        user = auth.session_user(self.cookie(COOKIE))
        if user:
            return user
        if path.startswith("/api/"):
            self.send(401, {"error": "Your session has ended. Sign in again."})
        else:
            self.redirect("/login?" + urlencode({"next": self.path}))
        return None

    # ---------- GET ----------

    def do_GET(self):
        path, _, query = self.path.partition("?")
        q = {k: v[0] for k, v in parse_qs(query).items()}
        if path in PUBLIC:
            return self.public_get(path, q)
        if path.startswith("/static/"):
            return self.static_file(path)
        user = self.gate(path)
        if not user:
            return
        if path == HOME:
            return self.send(200, (HERE / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/docs":
            return self.send(200, (HERE / "docs.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/me":
            return self.send(200, user)
        if path == "/api/state":
            run = CURRENT["run"]
            try:
                since = float(q.get("since", 0))
            except ValueError:
                since = 0.0
            snap = run.snapshot(since, inventory=q.get("inv") == "1") if run else None
            return self.send(200, {"run": snap})
        if path == "/api/projects":
            keys = {p: has_key(p) for p in triage.PROVIDERS}
            return self.send(200, {"projects": store.list_projects(), "profiles": throttle.PROFILES,
                                   "keys": keys, "env_keys": env_keys(),
                                   "models": {p: c["model"] for p, c in triage.PROVIDERS.items()},
                                   "compatible": triage.COMPATIBLE})
        m = re.fullmatch(r"/api/projects/([a-z0-9-]+)/runs/([a-z0-9-]+)(/digest)?", path)
        if m:
            try:
                if m.group(3):
                    text = store.load_digest(m.group(1), m.group(2))
                    return self.send(200, text, "text/markdown; charset=utf-8") if text \
                        else self.send(404, {"error": "no digest for this run"})
                result = store.load_run(m.group(1), m.group(2))
            except ValueError:
                result = None
            return self.send(200, result) if result else self.send(404, {"error": "run not found"})
        self.send(404, {"error": "not found"})

    def static_file(self, path):
        target = (STATIC / path[len("/static/"):]).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file() or target.suffix not in STATIC_TYPES:
            return self.send(404, {"error": "not found"})
        return self.send(200, target.read_bytes(), STATIC_TYPES[target.suffix], cache="public, max-age=86400")

    def public_get(self, path, q):
        if path == "/":
            return self.send(200, landing_page(auth.session_user(self.cookie(COOKIE))), "text/html; charset=utf-8")
        if path == "/about":
            return self.redirect("/")
        if path == "/favicon.ico":
            return self.send(204, b"", "image/x-icon")
        if path == "/login":
            if auth.session_user(self.cookie(COOKIE)):
                return self.redirect(safe_next(q.get("next")))
            notice = {"signed_out": "You've signed out."}.get(q.get("notice"))
            return self.send(200, login_page(notice=notice, next_url=safe_next(q.get("next"))),
                             "text/html; charset=utf-8")
        if path == "/logout":
            return self.redirect("/login")
        if path == "/auth/link":
            name = auth.use_login_link(q.get("token"))
            if not name:
                return self.send(400, login_page(error="That sign-in link has expired or was already used. "
                                                       "Ask an admin for a new one."), "text/html; charset=utf-8")
            return self.sign_in(name, "admin link", HOME)
        google = auth.google_config(CONFIG["redirect"])
        if not google["enabled"]:
            return self.send(404, login_page(error="Google sign-in isn't set up on this dashboard."),
                             "text/html; charset=utf-8")
        if path == "/auth/google":
            url, state = auth.google_start(google, safe_next(q.get("next")))
            return self.redirect(url, [self.set_cookie(OAUTH_COOKIE, state, 600)])
        if path == "/auth/google/callback":
            clear = [self.set_cookie(OAUTH_COOKIE, "", 0)]
            if q.get("error"):
                return self.send(400, login_page(error="Google sign-in was cancelled."), "text/html; charset=utf-8", clear)
            if not q.get("state") or q.get("state") != self.cookie(OAUTH_COOKIE):
                return self.send(400, login_page(error=(
                    "This sign-in didn't start in this browser. Open the dashboard at "
                    f"{CONFIG['redirect'].rsplit('/auth/', 1)[0]} and try again.")), "text/html; charset=utf-8", clear)
            name, next_url, error = auth.google_finish(google, q.get("code"), q.get("state"))
            if error:
                return self.send(403, login_page(error=error), "text/html; charset=utf-8", clear)
            token = auth.create_session(name, "google")
            return self.redirect(safe_next(next_url), clear + [self.set_cookie(COOKIE, token, auth.SESSION_MAX)])

    # ---------- POST ----------

    def do_POST(self):
        if not self.same_origin():
            return self.send(403, {"error": "Cross-site request refused"})
        body = self.body()
        if self.path == "/login":
            user, error = auth.authenticate(body.get("username"), body.get("password") or "")
            if not user:
                return self.send(401, login_page(error=error, username=body.get("username") or "",
                                                 next_url=safe_next(body.get("next"))), "text/html; charset=utf-8")
            return self.sign_in(user["username"], "password", body.get("next"))
        if self.path == "/logout":
            auth.end_session(self.cookie(COOKIE))
            return self.redirect("/login?notice=signed_out", [self.set_cookie(COOKIE, "", 0)])

        if not self.gate(self.path):
            return
        if self.path == "/api/projects":
            try:
                return self.send(200, {"project": store.save_project(body)})
            except ValueError as e:
                return self.send(400, {"error": str(e)})

        m = re.fullmatch(r"/api/projects/([a-z0-9-]+)/delete", self.path)
        if m:
            live = self.running()
            if live and live.project_id == m.group(1):
                return self.send(409, {"error": "Stop the running sweep before deleting this project"})
            try:
                store.delete_project(m.group(1))
            except ValueError:
                return self.send(400, {"error": "bad project id"})
            return self.send(200, {"ok": True})

        if self.path == "/api/start":
            if self.running():
                return self.send(409, {"error": "A sweep is already running"})
            project = store.get_project(body.get("project_id") or "")
            if not project:
                return self.send(404, {"error": "Save the project first"})
            run = Run(project, datetime.now().strftime("%Y%m%d-%H%M%S"))
            CURRENT["run"] = run
            threading.Thread(target=execute, args=(run,), daemon=True).start()
            return self.send(200, {"ok": True, "run_id": run.run_id})

        if self.path == "/api/list-models":
            o = store.clean_settings(body.get("settings"))
            try:
                cfg = provider_of(o)
            except ValueError as e:
                return self.send(200, {"ok": False, "message": str(e)})
            if not cfg["url"] or cfg.get("style") != "openai":
                return self.send(200, {"ok": False, "message": "Enter the endpoint URL first."})
            problem = key_problem(cfg)
            if problem:
                return self.send(200, {"ok": False, "message": problem + "."})
            try:
                models = triage.list_models(cfg)
            except requests.HTTPError as e:
                detail = re.sub(r"\s+", " ", e.response.text)[:200]
                return self.send(200, {"ok": False, "message": f"The service answered {e.response.status_code}: {detail}"})
            except (requests.RequestException, ValueError, AttributeError) as e:
                return self.send(200, {"ok": False, "message": f"Couldn't list models: {type(e).__name__}"})
            return self.send(200, {"ok": True, "models": models})

        if self.path == "/api/test-provider":
            o = store.clean_settings(body.get("settings"))
            try:
                cfg = provider_of(o)
            except ValueError as e:
                return self.send(200, {"ok": False, "message": str(e)})
            problem = ai_problem(cfg)
            if problem:
                return self.send(200, {"ok": False, "message": problem + "."})
            started = time.time()
            try:
                reply = triage.chat(cfg, "You are a connection test. Reply with exactly: []",
                                    "Reply with an empty JSON array.", timeout=60)
            except SystemExit as e:
                return self.send(200, {"ok": False, "message": str(e)})
            except requests.HTTPError as e:
                detail = re.sub(r"\s+", " ", e.response.text)[:200]
                return self.send(200, {"ok": False, "message": f"{cfg['url']} answered {e.response.status_code}: {detail}"})
            except (requests.RequestException, KeyError, IndexError, ValueError) as e:
                return self.send(200, {"ok": False, "message": f"Couldn't reach {cfg['url']}: {type(e).__name__}"})
            return self.send(200, {"ok": True, "message": f"{cfg['model']} answered in {time.time() - started:.1f}s: "
                                                          f"{reply.strip()[:60] or '(empty reply)'}"})

        if self.path == "/api/stop":
            run = self.running()
            if run:
                run.cancel.set()
                run.emit("warn", "Stopping after the current request…")
            return self.send(200, {"ok": True})

        self.send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    CONFIG["redirect"] = f"http://{shown}:{args.port}/auth/google/callback"
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Nightly sweep dashboard on http://{shown}:{args.port}  (Ctrl-C to quit)")
    google = auth.google_config(CONFIG["redirect"])
    if google["enabled"]:
        print(f"  Google sign-in on. Redirect URI to register with Google: {google['redirect']}")
    if not auth.has_users() and not google["enabled"]:
        print("  No accounts yet. Create one:  .venv/bin/python manage.py add-user <name> --admin")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
