#!/usr/bin/env python3
"""
Stage 2 of the nightly sweep: ask a model which flagged pages are actually dead.

This is the part stage 1 cannot do. A crawler knows a page has not been edited
since 2019. It does not know whether that matters. The city charter has not
changed since 2019 and should not have. A grant page saying "applications close
15 March 2021" for a programme that ended is a different animal entirely.

Stale is a date. Dead is a judgement. Only the second one needs a model, which
is why only flagged pages get sent — typically a few percent of the site.

Usage:
    export ANTHROPIC_API_KEY=sk-...
    python3 triage.py sweep.json --out digest.md

    # Same script against Grok:
    export XAI_API_KEY=xai-...
    python3 triage.py sweep.json --provider xai
"""

import argparse
import json
import os
import re
import sys
import textwrap
import time
from datetime import datetime

import requests


ENV_FILE_KEYS = set()     # names defined in .env, the only keys a custom provider may use


def load_env(path=os.environ.get("ENV_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    """Read KEY=value lines from .env next to this script. A key already set in
    the environment wins, so `export` still overrides the file."""
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.removeprefix("export ").partition("=")
                ENV_FILE_KEYS.add(key.strip())
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    except FileNotFoundError:
        pass


load_env()

PROVIDERS = {
    "anthropic": {
        "url": "https://api.anthropic.com/v1/messages",
        "model": "claude-sonnet-4-6",
        "key_env": "ANTHROPIC_API_KEY",
    },
    "xai": {
        "url": "https://api.x.ai/v1/chat/completions",
        "model": "grok-4",
        "key_env": "XAI_API_KEY",
    },
}

SYSTEM = """You review pages on a government website and decide whether each one \
is still doing its job or has quietly died.

Return ONLY a JSON array. One object per page, in the order given:

  {"url": "...", "verdict": "live" | "dead" | "unclear",
   "reason": "<one sentence, max 20 words>",
   "evidence": "<the exact phrase from the page that decided it, or null>"}

Rules that matter:
- "dead" means the page describes something that has ended, expired, or passed:
  a closed application window, a programme that was sunset, an event in the past
  presented as upcoming, a deadline that has gone by.
- "live" means the content is still true even if the page is old. Charters,
  ordinances, contact pages, historical records and reference material are
  normally live. Age alone is NEVER evidence of death.
- "unclear" when the text genuinely does not say. Use it freely. A wrong "dead"
  costs a web team more than an honest "unclear".
- Quote evidence verbatim from the supplied text. Never paraphrase it and never
  invent a phrase that is not there.
- A page can be dead even with recent edits, and live despite years of neglect.
  Judge the content, not the timestamp."""


def build_prompt(batch, today):
    lines = [f"Today is {today}. Review these {len(batch)} pages.\n"]
    for i, p in enumerate(batch, 1):
        lines.append(f"--- PAGE {i} ---")
        lines.append(f"url: {p['url']}")
        lines.append(f"title: {p.get('title') or '(none)'}")
        lines.append(f"last modified: {p.get('last_modified') or 'unknown'}")
        if p.get("forms"):
            lines.append(f"forms on page: {len(p['forms'])}")
        if p.get("mailtos"):
            lines.append(f"email addresses: {', '.join(p['mailtos'][:5])}")
        if p.get("expired_events"):
            names = [e.get("name") or "?" for e in p["expired_events"][:3]]
            lines.append(f"expired events: {names}")
        body = textwrap.shorten(p.get("text", ""), width=2200, placeholder=" …")
        lines.append(f"text: {body}\n")
    return "\n".join(lines)


# A custom provider is any endpoint that speaks the OpenAI chat-completions format:
# OpenAI, Groq, OpenRouter, or a local model in Ollama or LM Studio. Its key, if it
# needs one, is read from .env by name. The built-in providers' keys are off limits,
# so a custom endpoint can never be handed the Anthropic or xAI key.
CUSTOM_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*_(API_KEY|TOKEN)$")
RESERVED_KEYS = {p["key_env"] for p in PROVIDERS.values()}


# Well-known services that speak the same format, offered by name in the dashboard.
# The address and key name are fixed; the project only chooses the model.
COMPATIBLE = {
    "gemini": {"label": "Google Gemini", "url": "https://generativelanguage.googleapis.com/v1beta/openai", "key_env": "GEMINI_API_KEY"},
    "openai": {"label": "OpenAI", "url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY"},
    "groq": {"label": "Groq", "url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY"},
    "openrouter": {"label": "OpenRouter", "url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY"},
    "ollama": {"label": "Ollama (local)", "url": "http://localhost:11434/v1", "key_env": ""},
    "lmstudio": {"label": "LM Studio (local)", "url": "http://localhost:1234/v1", "key_env": ""},
}
RETRY_STATUSES = (429, 500, 502, 503)   # busy or rate-limited: worth another try


def custom_key_problem(key_env):
    if not key_env:
        return None
    if not CUSTOM_KEY_RE.match(key_env):
        return "The key setting must be a name like OPENAI_API_KEY or GROQ_API_KEY (ending in _API_KEY or _TOKEN)."
    if key_env in RESERVED_KEYS:
        return f"{key_env} belongs to a built-in provider and can't be used for a custom endpoint."
    return None


def custom_provider(url, model, key_env=""):
    problem = custom_key_problem(key_env)
    if problem:
        raise ValueError(problem)
    url = (url or "").strip().rstrip("/")
    if url and not url.endswith("/chat/completions"):
        url += "/chat/completions"
    return {"name": "custom", "url": url, "model": (model or "").strip(), "key_env": key_env,
            "style": "openai", "timeout": 300}     # local models can be slow


def compatible_provider(name, model):
    """A named OpenAI-compatible service with its fixed address and key name."""
    preset = COMPATIBLE[name]
    cfg = custom_provider(preset["url"], model, preset["key_env"])
    cfg["name"] = name
    return cfg


def list_models(provider):
    """Model ids the service offers for chat, best effort. Raises requests errors."""
    cfg = resolve(provider)
    key = os.environ.get(cfg["key_env"]) if cfg["key_env"] else None
    base = cfg["url"].removesuffix("/chat/completions")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    r = requests.get(base + "/models", headers=headers, timeout=20)
    r.raise_for_status()
    skip = ("embed", "tts", "audio", "image", "imagen", "veo", "whisper", "dall-e", "moderation",
            "transcribe", "realtime", "live", "aqa", "robotics")
    ids = {m.get("id", "").removeprefix("models/") for m in r.json().get("data", [])}
    return sorted(i for i in ids if i and not any(w in i.lower() for w in skip))


def resolve(provider):
    """A provider name ("anthropic", "xai") or a custom provider dict, as one config."""
    if isinstance(provider, dict):
        return provider
    style = "anthropic" if provider == "anthropic" else "openai"
    return {"name": provider, **PROVIDERS[provider], "style": style}


def chat(provider, system, prompt, timeout=120):
    """Send one system + user message and return the reply text."""
    cfg = resolve(provider)
    if cfg["name"] not in PROVIDERS and cfg["key_env"] and cfg["key_env"] not in ENV_FILE_KEYS:
        # Only keys put in .env on purpose, never whatever the shell happens to hold.
        sys.exit(f"Add {cfg['key_env']} to the .env file and restart the dashboard.")
    key = os.environ.get(cfg["key_env"]) if cfg["key_env"] else None
    if cfg["key_env"] and not key:
        sys.exit(f"Set {cfg['key_env']} in .env or the environment before running.")
    timeout = cfg.get("timeout", timeout)

    if cfg["style"] == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        body = {"model": cfg["model"], "max_tokens": 4000, "system": system,
                "messages": [{"role": "user", "content": prompt}]}
    else:
        headers = {"content-type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = {"model": cfg["model"], "max_tokens": 4000,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": prompt}]}

    for attempt in range(3):
        r = requests.post(cfg["url"], headers=headers, json=body, timeout=timeout)
        if r.status_code == 400 and "max_completion_tokens" in r.text and "max_tokens" in body:
            # Newer OpenAI models refuse max_tokens and want max_completion_tokens.
            body["max_completion_tokens"] = body.pop("max_tokens")
            r = requests.post(cfg["url"], headers=headers, json=body, timeout=timeout)
        if r.status_code not in RETRY_STATUSES or attempt == 2:
            break
        wait = r.headers.get("Retry-After", "")
        time.sleep(min(int(wait), 30) if wait.isdigit() else (5, 15)[attempt])
    r.raise_for_status()
    data = r.json()

    if cfg["style"] == "anthropic":
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return data["choices"][0]["message"].get("content") or ""


def call_model(provider, prompt, timeout=120, system=SYSTEM):
    text = chat(provider, system, prompt, timeout)
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []


def digest(sweep, verdicts, stale_months):
    by_url = {v["url"]: v for v in verdicts if isinstance(v, dict) and "url" in v}
    dead, unclear = [], []
    for f in sweep["findings"]:
        v = by_url.get(f["url"])
        if not v:
            continue
        entry = {**f, **v}
        if v.get("verdict") == "dead":
            dead.append(entry)
        elif v.get("verdict") == "unclear":
            unclear.append(entry)

    broken = [(f["url"], b) for f in sweep["findings"] for b in f["broken_links"]]
    expired = [(f["url"], e) for f in sweep["findings"] for e in f["expired_events"]]

    L = []
    A = L.append
    A(f"# Nightly sweep — {sweep['base']}")
    A("")
    A(f"Run {sweep['swept_at'][:16].replace('T', ' ')} UTC &middot; "
      f"{sweep['pages_crawled']} pages crawled, {sweep['links_checked']} links checked")
    A("")
    A("## What needs a decision")
    A("")
    if dead:
        A(f"**{len(dead)} pages look dead.** Each one describes something that has ended.")
        A("")
        A("| Page | Last edited | Why | Evidence |")
        A("| --- | --- | --- | --- |")
        for d in dead:
            lm = (d.get("last_modified") or "unknown")[:10]
            ev = (d.get("evidence") or "").replace("|", "/")[:70]
            A(f"| [{d['title'][:48] or d['url']}]({d['url']}) | {lm} | "
              f"{d.get('reason', '')[:60]} | {ev} |")
        A("")
    else:
        A("No pages judged dead in this run.")
        A("")
    if unclear:
        A(f"**{len(unclear)} pages are unclear** and need a human who knows the programme.")
        A("")
        for u in unclear[:15]:
            A(f"- [{u['title'][:60] or u['url']}]({u['url']}) — {u.get('reason', '')}")
        A("")
    A("## Mechanical fixes")
    A("")
    A(f"- **{len(broken)} broken links**")
    for url, b in broken[:15]:
        A(f"  - `{b['status']}` {b['url']}  _(on {url})_")
    if len(broken) > 15:
        A(f"  - …and {len(broken) - 15} more")
    A("")
    A(f"- **{len(expired)} expired events still published**")
    for url, e in expired[:10]:
        A(f"  - {e.get('name') or '(untitled)'} ended {str(e.get('endDate'))[:10]} — {url}")
    A("")
    stale_n = sum(1 for f in sweep["findings"] if f["stale"])
    A(f"- **{stale_n} pages untouched for {stale_months}+ months** "
      f"(most are fine — see the table above for the ones that are not)")
    A("")
    A("---")
    A("")
    A("Nothing on the site was changed. This agent has no write access.")
    return "\n".join(L), dead, unclear


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sweep_json")
    ap.add_argument("--out", default="digest.md")
    ap.add_argument("--provider", choices=list(PROVIDERS), default="anthropic")
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--stale-months", type=int, default=18)
    ap.add_argument("--dry-run", action="store_true",
                    help="Build the digest with no verdicts, to check wiring")
    args = ap.parse_args()

    sweep = json.load(open(args.sweep_json))
    candidates = sweep["findings"][: args.limit]
    print(f"{len(sweep['findings'])} flagged pages, triaging {len(candidates)}")

    verdicts = []
    if not args.dry_run:
        today = datetime.now().strftime("%Y-%m-%d")
        for i in range(0, len(candidates), args.batch_size):
            batch = candidates[i:i + args.batch_size]
            print(f"  batch {i // args.batch_size + 1}: {len(batch)} pages…", flush=True)
            try:
                verdicts.extend(call_model(args.provider, build_prompt(batch, today)))
            except requests.RequestException as e:
                print(f"    skipped: {e}")

    text, dead, unclear = digest(sweep, verdicts, args.stale_months)
    with open(args.out, "w") as fh:
        fh.write(text)

    print(f"\n  {len(dead)} dead, {len(unclear)} unclear")
    print(f"  wrote {args.out}")
    if dead:
        print("\n  The number for your cold open:")
        print(f"    {len(dead)} pages on this site describe something that has ended.")


if __name__ == "__main__":
    sys.exit(main())
