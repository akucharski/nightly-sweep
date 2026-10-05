#!/usr/bin/env python3
"""
The nightly sweep, as an agent.

The dashboard is a pipeline: code decides every step and calls a model at one
fixed point. Here Claude runs the sweep. It decides which pages are worth reading,
reads them through read-only tools, judges them, and hands a report to a person.
Its instructions are nightly-sweep.md, nearly word for word, and its claws are
its tool list:

    Read      open    read_sitemap, fetch_page   public pages on this one site
    Browse    open    check_links                links found on pages it has read
    Write     closed  no tool can change the site, so there is nothing to ask for
    Shell     closed  no tool runs a command
    Schedule  open    cron runs this file at 03:00 (see README)

Every action passes through one function, Harness.call_tool, which enforces the
claws and writes a line to the audit log. Claims are checked by code, not trusted:
a finding whose quote isn't on the page is discarded before anyone sees it.

Usage:
    python3 agent.py https://www.example.gov
    python3 agent.py https://www.example.gov --max-pages 40 --speed polite --mail-to web@example.gov
"""

import argparse
import json
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import anthropic
import requests

import analysis
import throttle
import triage  # noqa: F401  (loads .env, including ANTHROPIC_API_KEY)
from sweep import USER_AGENT, Sweeper, normalise, parse_lastmod, same_site

HERE = Path(__file__).resolve().parent
SPEC = HERE / "nightly-sweep.md"
RUNS = HERE / "agent-runs"
MODEL = "claude-opus-5"
FALLBACK_MODELS = ("claude-opus-5", "claude-opus-5-5", "claude-fable-5-1")   # accept fallbacks: "default"
PRICES = {"claude-opus-5": (5.00, 25.00)}   # $ per million input / output tokens, for the cost line

# The claws. Closed claws have no tools, and a tool that isn't listed doesn't exist for the agent.
CLAWS = [
    ("Read", "open", ["read_sitemap", "fetch_page"], "public pages on this one site, robots.txt enforced"),
    ("Browse", "open", ["check_links"], "HTTP status of links found on pages it has read"),
    ("Write", "closed", [], "no tool can change the site"),
    ("Shell", "closed", [], "no tool runs a command"),
    ("Schedule", "open", [], "cron starts the run; the agent can't schedule anything itself"),
]
REPORT_TOOL = "submit_report"   # not a claw: how the agent hands its findings to a person

TOOLS = [
    {
        "name": "read_sitemap",
        "description": "Read the site's sitemap.xml (following sitemap indexes). Returns every listed page with "
                       "its last-modified date, oldest first. Free: doesn't count against the page budget.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "fetch_page",
        "description": "Read one public page on the site being swept. Returns HTTP status, title, last-modified "
                       "date and age, whether it's stale, past dates still written as upcoming, expired events, "
                       "forms, contact emails, its links, and the page text. Off-site addresses and pages "
                       "robots.txt disallows are refused. Each new page uses one unit of the page budget.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Absolute URL on the site being swept"}},
            "required": ["url"], "additionalProperties": False,
        },
    },
    {
        "name": "check_links",
        "description": "Check the HTTP status of up to 25 links. Only links that appeared on pages you have "
                       "already read can be checked. Returns each link's status code or error.",
        "input_schema": {
            "type": "object",
            "properties": {"urls": {"type": "array", "items": {"type": "string"}, "maxItems": 25}},
            "required": ["urls"], "additionalProperties": False,
        },
    },
    {
        "name": REPORT_TOOL,
        "description": "Hand your findings to a person. Call exactly once, at the end. Quotes must be copied "
                       "word for word from fetch_page text; any that can't be found on the page are discarded.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Two or three sentences for the web team."},
                "decisions": {
                    "type": "array", "description": "Pages that need a human decision: judged dead or unclear.",
                    "items": {"type": "object", "properties": {
                        "url": {"type": "string"},
                        "verdict": {"type": "string", "enum": ["dead", "unclear"]},
                        "reason": {"type": "string", "description": "One sentence, at most 20 words."},
                        "quote": {"type": "string", "description": "The exact sentence from the page that decided it."},
                    }, "required": ["url", "verdict", "reason", "quote"], "additionalProperties": False},
                },
                "contradictions": {
                    "type": "array",
                    "items": {"type": "object", "properties": {
                        "topic": {"type": "string"},
                        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                        "page_a": {"type": "string"}, "quote_a": {"type": "string"},
                        "page_b": {"type": "string"}, "quote_b": {"type": "string"},
                        "explanation": {"type": "string"},
                    }, "required": ["topic", "severity", "page_a", "quote_a", "page_b", "quote_b", "explanation"],
                        "additionalProperties": False},
                },
                "live": {"type": "array", "items": {"type": "string"},
                         "description": "URLs you read and judged still true."},
            },
            "required": ["summary", "decisions", "contradictions", "live"], "additionalProperties": False,
        },
    },
]

OPERATING_NOTES = """

## How this run works

You are the agent this document describes, running now against {site}. Today is {today}.

Your claws are your tools:

- read_sitemap and fetch_page (Read): public pages on {host} only. robots.txt is enforced for you.
- check_links (Browse): the status of links that appeared on pages you have read.
- submit_report: hands your findings to a person. Call it exactly once, at the end.

No tool can change the site or run a command. That is deliberate; don't look for one.

Budget: you can read up to {max_pages} pages and check up to {max_links} links. The sitemap is free.
Requests are paced for you, so reading several pages in one turn is fine and saves time.

Choosing pages. Start with read_sitemap. Its dates are free arithmetic, so use them to spend your
budget where judgement matters:
- old pages about time-bound things: applications, grants, programmes, events, registrations,
  hearings, deadlines, closures, seasonal services, elections, budgets;
- pages where fetch_page reports past dates written as upcoming, or expired events;
- pages with forms;
- pages on the same subject in different sections (fees, hours, contacts), where contradictions hide.
If there's no sitemap, start at the homepage and follow internal links.

Check the links on the pages you read, or at least on the ones you flag.

Report with submit_report:
- decisions: every page you judge dead or unclear, with the exact sentence that decided it, copied
  from the fetch_page text.
- contradictions: two statements, on two pages or one, that can't both be true about the same thing,
  each quoted word for word.
- live: the pages you read and judged still true.
The harness checks every quote against the page and discards any it can't find. It works out broken
links, expired events and staleness from your tool calls, so don't list those. The harness also
emails the digest; you don't send anything.
"""


def log(msg=""):
    print(msg, flush=True)


class Harness:
    """The agent's only way to touch the world. Enforces the claws; logs every action."""

    def __init__(self, site, out, max_pages, max_links, stale_months, speed):
        self.site, self.out = site, out
        self.max_pages, self.max_links, self.stale_months = max_pages, max_links, stale_months
        self.open_tools = {t for _, state, tools, _ in CLAWS if state == "open" for t in tools} | {REPORT_TOOL}
        self.s = Sweeper(site, max_pages=max_pages, delay=0)
        floor = 0.0
        try:
            floor = float(self.s.robots.crawl_delay(USER_AGENT) or 0)
        except (TypeError, ValueError, AttributeError):
            pass
        pacer = throttle.Pacer(self.s.root_netloc, throttle.PROFILES[speed], floor=floor,
                               cancel=threading.Event(), on_event=lambda kind, m: log(f"    pace: {m}"))
        pacer.after(self.s.root_netloc, 200)
        session = throttle.PacedSession(pacer)
        session.headers.update(self.s.session.headers)
        self.s.session = session
        self.sitemap = None
        self.pages = {}            # url -> full page record (for quote checks)
        self.link_status = {}      # url -> status
        self.known_links = set()   # links seen on pages read: the only ones check_links may touch
        self.report = None
        self.today = datetime.now(timezone.utc)
        self.audit = open(out / "audit.jsonl", "w")

    # ---------- the one gate ----------

    def call_tool(self, name, args):
        if name not in self.open_tools:
            result, error = {"error": f"There is no tool called {name}. The claws open for this run: "
                                      f"{', '.join(sorted(self.open_tools))}."}, True
        else:
            try:
                result = getattr(self, "tool_" + name)(**args)
                error = "error" in result
            except TypeError as e:
                result, error = {"error": f"Bad arguments for {name}: {e}"}, True
        self.audit.write(json.dumps({"t": datetime.now(timezone.utc).isoformat(), "tool": name, "input": args,
                                     "error": result.get("error") if error else None,
                                     "summary": {k: v for k, v in result.items() if k in ("status", "title", "budget_left", "checked")}}) + "\n")
        self.audit.flush()
        return result, error

    # ---------- Read ----------

    def tool_read_sitemap(self):
        if self.sitemap is None:
            self.sitemap = self.s.from_sitemap()
        if not self.sitemap:
            return {"pages": [], "note": "No usable sitemap. Start from the homepage with fetch_page and follow internal links."}
        dated = sorted(self.sitemap.items(), key=lambda kv: (parse_lastmod(kv[1]) or self.today).isoformat())
        lines = [f"{(parse_lastmod(lm).date().isoformat() if parse_lastmod(lm) else 'no date'):10}  {u}" for u, lm in dated]
        log(f"  read_sitemap -> {len(lines)} pages")
        return {"total": len(lines), "shown": min(len(lines), 400), "pages": lines[:400]}

    def tool_fetch_page(self, url):
        url = normalise(url.strip())
        if urlparse(url).scheme not in ("http", "https") or not same_site(url, self.s.root_netloc):
            return {"error": f"Refused: {url} is off-site. The Read claw covers {self.s.root_netloc} only."}
        if not self.s.allowed(url):
            return {"error": f"Refused: robots.txt disallows {url}."}
        if url not in self.pages:
            if len(self.pages) >= self.max_pages:
                return {"error": f"Page budget used ({self.max_pages}). Stop reading and call {REPORT_TOOL}."}
            rec = self.s.fetch_page(url)
            if rec is None:
                return {"error": f"{url} isn't an HTML page."}
            self.pages[url] = rec
            for link in rec["internal_links"] + rec["external_links"]:
                self.known_links.add(link)
            log(f"  fetch_page {urlparse(url).path or '/'}  -> {rec.get('status') or rec.get('error')}  "
                f"({len(self.pages)}/{self.max_pages})")
        rec = self.pages[url]
        dt = parse_lastmod((self.sitemap or {}).get(url) or rec.get("lastmod"))
        age = (self.today - dt).days if dt else None
        return {
            "url": url, "status": rec.get("status"), "title": rec.get("title"),
            "last_modified": dt.date().isoformat() if dt else None, "age_days": age,
            "stale": age is not None and age > 30 * self.stale_months,
            "past_dates_written_as_upcoming": [m["quote"] for m in analysis.outdated_mentions(rec.get("text", ""), self.today)],
            "expired_events": [e for e in rec.get("events", []) if (parse_lastmod(str(e.get("endDate"))) or self.today) < self.today],
            "forms": len(rec.get("forms", [])), "emails": rec.get("mailtos", [])[:5],
            "internal_links": rec.get("internal_links", [])[:40], "internal_link_count": len(rec.get("internal_links", [])),
            "external_link_count": len(rec.get("external_links", [])),
            "text": rec.get("text", "")[:3000],
            "budget_left": self.max_pages - len(self.pages),
        }

    # ---------- Browse ----------

    def tool_check_links(self, urls):
        out, refused = {}, []
        for u in urls[:25]:
            u = normalise(u.strip())
            if u not in self.known_links:
                refused.append(u)
                continue
            if u in self.link_status:
                out[u] = self.link_status[u]
                continue
            if len(self.link_status) >= self.max_links:
                return {"checked": out, "error": f"Link budget used ({self.max_links}). Call {REPORT_TOOL} when ready."}
            try:
                r = self.s.session.head(u, timeout=20, allow_redirects=True)
                if r.status_code >= 400 or r.status_code == 405:
                    r = self.s.session.get(u, timeout=20, stream=True)
                self.link_status[u] = r.status_code
            except requests.RequestException as e:
                self.link_status[u] = type(e).__name__
            out[u] = self.link_status[u]
        bad = sum(1 for v in out.values() if isinstance(v, str) or v >= 400)
        log(f"  check_links {len(out)} links -> {bad} broken")
        result = {"checked": out}
        if refused:
            result["refused"] = {"urls": refused, "why": "Only links found on pages you have read can be checked."}
        return result

    # ---------- the hand-off ----------

    def tool_submit_report(self, summary, decisions, contradictions, live):
        self.report = {"summary": summary, "decisions": decisions, "contradictions": contradictions, "live": live}
        log(f"  {REPORT_TOOL}: {len(decisions)} decisions, {len(contradictions)} contradictions, {len(live)} live")
        return {"received": True}


def run_agent(h, model, effort, max_turns):
    client = anthropic.Anthropic()
    host = h.s.root_netloc
    system = SPEC.read_text() + OPERATING_NOTES.format(site=h.site, host=host, today=h.today.date().isoformat(),
                                                       max_pages=h.max_pages, max_links=h.max_links)
    tools = [t for t in TOOLS if t["name"] in h.open_tools]
    messages = [{"role": "user", "content": f"Sweep {h.site} now and report what needs a person's attention."}]
    extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if model in FALLBACK_MODELS else {}
    if effort:
        extra["output_config"] = {"effort": effort}
    usage = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
    nudged = False

    for turn in range(1, max_turns + 1):
        resp = client.beta.messages.create(
            model=model, max_tokens=16000, system=system, tools=tools, messages=messages,
            thinking={"type": "adaptive", "display": "summarized"},
            cache_control={"type": "ephemeral"},     # each turn re-sends the history; caching makes that cheap
            **extra,
        )
        u = resp.usage
        usage["input"] += u.input_tokens or 0
        usage["output"] += u.output_tokens or 0
        usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0

        if resp.stop_reason == "refusal":
            log(f"  The model declined this turn ({getattr(resp.stop_details, 'category', None)}). Stopping.")
            break
        for block in resp.content:
            if block.type == "thinking" and getattr(block, "thinking", ""):
                log("  thinking: " + " ".join(block.thinking.split())[:220])
            elif block.type == "text" and block.text.strip():
                log("  agent: " + " ".join(block.text.split())[:220])

        messages.append({"role": "assistant", "content": resp.content})
        calls = [b for b in resp.content if b.type == "tool_use"]
        if not calls:
            if h.report is None and not nudged and resp.stop_reason == "end_turn":
                nudged = True
                messages.append({"role": "user", "content": f"Call {REPORT_TOOL} now with what you have."})
                continue
            break
        results = []
        for b in calls:   # all results go back in one message, as the API expects
            out, is_error = h.call_tool(b.name, b.input)
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out),
                            **({"is_error": True} if is_error else {})})
        messages.append({"role": "user", "content": results})
        if h.report is not None:
            break
    else:
        log(f"  Stopped after {max_turns} turns without a report.")
    return usage


def verify(h):
    """Keep only claims whose quotes really are on the pages the agent read."""
    rep = h.report or {"summary": "The agent didn't submit a report.", "decisions": [], "contradictions": [], "live": []}
    text = {u: analysis._norm(r.get("text", "")) for u, r in h.pages.items()}

    def found(url, quote):
        url = normalise(url or "")
        return url in text and len(analysis._norm(quote)) >= 4 and analysis._norm(quote) in text[url]

    decisions = [d for d in rep["decisions"] if found(d["url"], d["quote"])]
    contradictions = [c for c in rep["contradictions"] if found(c["page_a"], c["quote_a"]) and found(c["page_b"], c["quote_b"])]
    dropped = (len(rep["decisions"]) - len(decisions)) + (len(rep["contradictions"]) - len(contradictions))
    return rep, decisions, contradictions, dropped


def digest(h, rep, decisions, contradictions, dropped, model):
    title = lambda u: (h.pages.get(normalise(u), {}).get("title") or u)[:70]
    broken = [(u, l, h.link_status[l]) for u, r in h.pages.items() for l in r["internal_links"] + r["external_links"]
              if l in h.link_status and (isinstance(h.link_status[l], str) or h.link_status[l] >= 400)]
    expired = [(u, e) for u, r in h.pages.items() for e in r.get("events", [])
               if (parse_lastmod(str(e.get("endDate"))) or h.today) < h.today]
    sm = h.sitemap or {}
    stale = sum(1 for lm in sm.values() if parse_lastmod(lm) and (h.today - parse_lastmod(lm)).days > 30 * h.stale_months)

    L = [f"# Nightly sweep — {h.site}", "",
         f"Run {h.today.strftime('%Y-%m-%d %H:%M')} UTC by an agent ({model}). It read {len(h.pages)} pages "
         f"and checked {len(h.link_status)} links.", "", rep["summary"], "", "## What needs a decision", ""]
    if decisions:
        for d in sorted(decisions, key=lambda d: d["verdict"]):
            L.append(f"- **{d['verdict']}**: [{title(d['url'])}]({d['url']}). {d['reason']}  \n  > {d['quote']}")
    else:
        L.append("No pages judged dead or unclear.")
    L += ["", "## Contradictions", ""]
    if contradictions:
        for c in contradictions:
            L.append(f"- **{c['topic']}** ({c['severity']}): \"{c['quote_a']}\" ([{title(c['page_a'])}]({c['page_a']})) "
                     f"vs \"{c['quote_b']}\" ([{title(c['page_b'])}]({c['page_b']})). {c['explanation']}")
    else:
        L.append("None found among the pages read.")
    L += ["", "## Mechanical fixes", "", f"- **{len(broken)} broken links**"]
    L += [f"  - `{st}` {link}  _(on {page})_" for page, link, st in broken[:25]]
    L += [f"- **{len(expired)} expired events still published**"]
    L += [f"  - {e.get('name') or '(untitled)'} ended {str(e.get('endDate'))[:10]} — {page}" for page, e in expired[:10]]
    if sm:
        L.append(f"- **{stale} of {len(sm)} sitemap pages untouched for {h.stale_months}+ months** (age alone isn't a problem)")
    L += ["", "---", ""]
    if dropped:
        L.append(f"{dropped} claim{'s' if dropped > 1 else ''} the agent made couldn't be matched word for word to the page and "
                 f"{'were' if dropped > 1 else 'was'} discarded.")
    L.append("Nothing on the site was changed. This agent has no write access.")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Run the nightly sweep as an agent.")
    ap.add_argument("site")
    ap.add_argument("--max-pages", type=int, default=40, help="pages the agent may read (default 40)")
    ap.add_argument("--max-links", type=int, default=300, help="links the agent may check (default 300)")
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--stale-months", type=int, default=18)
    ap.add_argument("--speed", choices=list(throttle.PROFILES), default="polite")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"],
                    help="how hard the model thinks; lower is cheaper (default: the model's own)")
    ap.add_argument("--mail-to", help="email the digest here when done (uses the mail command)")
    a = ap.parse_args()
    if not a.site.startswith(("http://", "https://")):
        sys.exit("error: the site must start with http:// or https://")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = RUNS / f"{urlparse(a.site).netloc.replace('www.', '')}-{stamp}"
    out.mkdir(parents=True)

    log(f"Nightly sweep agent · {a.site} · {a.model}\n")
    log("Claws:")
    for name, state, tools, why in CLAWS:
        log(f"  {name:9} {state:7} {', '.join(tools) or '-':26} {why}")
    log("")

    h = Harness(a.site, out, a.max_pages, a.max_links, a.stale_months, a.speed)
    try:
        usage = run_agent(h, a.model, a.effort, a.max_turns)
    except KeyboardInterrupt:
        log("\n  Interrupted; writing what was found so far.")
        usage = None
    except anthropic.AuthenticationError:
        sys.exit("error: ANTHROPIC_API_KEY is missing or invalid (set it in .env)")
    except anthropic.APIStatusError as e:
        sys.exit(f"error: the API answered {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        sys.exit("error: couldn't reach the API")

    rep, decisions, contradictions, dropped = verify(h)
    text = digest(h, rep, decisions, contradictions, dropped, a.model)
    (out / "digest.md").write_text(text)
    (out / "report.json").write_text(json.dumps({
        "site": a.site, "model": a.model, "pages_read": len(h.pages), "links_checked": len(h.link_status),
        "report_as_submitted": rep, "verified_decisions": decisions, "verified_contradictions": contradictions,
        "discarded_claims": dropped, "usage": usage}, indent=2))
    h.audit.close()

    log(f"\nVerified {len(decisions)} decisions and {len(contradictions)} contradictions"
        + (f"; discarded {dropped} unverifiable" if dropped else "") + ".")
    if usage:
        tokens = f"{usage['input'] + usage['cache_write'] + usage['cache_read']:,} in / {usage['output']:,} out"
        price = PRICES.get(a.model)
        if price:
            cost = (usage["input"] * price[0] + usage["cache_write"] * price[0] * 1.25
                    + usage["cache_read"] * price[0] * 0.1 + usage["output"] * price[1]) / 1e6
            tokens += f", about ${cost:.2f}"
        log(f"Tokens: {tokens}")
    log(f"Wrote {out.relative_to(HERE)}/digest.md, report.json and audit.jsonl")

    if a.mail_to:
        if shutil.which("mail"):
            subprocess.run(["mail", "-s", f"Site sweep (agent) — {h.today.date()}", a.mail_to], input=text.encode(), check=False)
            log(f"Emailed the digest to {a.mail_to}")
        else:
            log("No mail command on this machine; the digest is in the run folder.")


if __name__ == "__main__":
    main()
