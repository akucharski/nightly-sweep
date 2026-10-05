"""
Saved projects and their run history, as plain JSON under ./projects.

    projects/<project-id>/project.json            name, site, settings
    projects/<project-id>/runs/<run-id>/result.json   everything the dashboard shows
    projects/<project-id>/runs/<run-id>/summary.json  headline counts, for lists and trends
    projects/<project-id>/runs/<run-id>/sweep.json    the stage-1 output, same shape as sweep.py's
    projects/<project-id>/runs/<run-id>/digest.md     the human digest
    projects/<project-id>/analysis.json           AI verdicts per page, reused while a page is unchanged
"""

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import triage
from throttle import PROFILES

ROOT = Path(__file__).resolve().parent / "projects"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

DEFAULT_SETTINGS = {
    "max_pages": 200,
    "stale_months": 18,
    "speed": "polite",
    "custom_min": 2.0,
    "custom_max": 5.0,
    "link_check": True,
    "ai": True,
    "contradictions": True,
    "reuse_analysis": True,
    "contra_groups": 12,
    "provider": "anthropic",
    "custom_url": "",
    "custom_model": "",
    "custom_key_env": "",
    "limit": 120,
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _dir(pid, rid=None):
    if not ID_RE.match(pid or "") or (rid is not None and not ID_RE.match(rid)):
        raise ValueError("bad id")
    d = ROOT / pid
    return d / "runs" / rid if rid else d


def _read(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data if isinstance(data, str) else json.dumps(data, indent=2))
    os.replace(tmp, path)


def clean_settings(raw):
    s = dict(DEFAULT_SETTINGS)
    raw = raw or {}

    def num(key, lo, hi, cast=int):
        try:
            s[key] = min(max(cast(raw.get(key, s[key])), lo), hi)
        except (TypeError, ValueError):
            pass

    num("max_pages", 1, 5000)
    num("stale_months", 1, 240)
    num("contra_groups", 1, 40)
    num("limit", 1, 1000)
    num("custom_min", 0.2, 120, float)
    num("custom_max", 0.2, 300, float)
    s["custom_max"] = max(s["custom_max"], s["custom_min"])
    if raw.get("speed") in list(PROFILES) + ["custom"]:
        s["speed"] = raw["speed"]
    if raw.get("provider") in ("anthropic", "xai", "custom", *triage.COMPATIBLE):
        s["provider"] = raw["provider"]
    url = str(raw.get("custom_url") or "").strip()[:300]
    s["custom_url"] = url if url.startswith(("http://", "https://")) else ""
    s["custom_model"] = str(raw.get("custom_model") or "").strip()[:120]
    key_env = str(raw.get("custom_key_env") or "").strip().upper()[:64]
    s["custom_key_env"] = "" if triage.custom_key_problem(key_env) else key_env
    for key in ("link_check", "ai", "contradictions", "reuse_analysis"):
        if key in raw:
            s[key] = bool(raw[key])
    return s


def list_runs(pid):
    runs_dir = _dir(pid) / "runs"
    if not runs_dir.is_dir():
        return []
    out = [s for d in runs_dir.iterdir() if (s := _read(d / "summary.json"))]
    return sorted(out, key=lambda r: r.get("started", ""), reverse=True)


def get_project(pid):
    try:
        p = _read(_dir(pid) / "project.json")
    except ValueError:
        return None
    if p:
        p["settings"] = clean_settings(p.get("settings"))
    return p


def list_projects():
    if not ROOT.is_dir():
        return []
    out = []
    for d in ROOT.iterdir():
        if ID_RE.match(d.name) and (p := get_project(d.name)):
            p["runs"] = list_runs(d.name)
            p["remembered"] = len(load_analysis(d.name))
            out.append(p)
    return sorted(out, key=lambda p: p.get("name", "").lower())


def save_project(data):
    name = (data.get("name") or "").strip()[:80]
    site = (data.get("site") or "").strip()
    if not site.startswith(("http://", "https://")):
        raise ValueError("Site URL must start with http:// or https://")
    pid = data.get("id")
    existing = get_project(pid) if pid else None
    if pid and not existing:
        raise ValueError("No such project")
    if not existing:
        base = re.sub(r"[^a-z0-9]+", "-", (name or site.split("//", 1)[-1]).lower()).strip("-")[:48] or "project"
        pid, n = base, 2
        while (ROOT / pid).exists():
            pid, n = f"{base}-{n}", n + 1
    settings = data.get("settings") or {}
    if settings.get("provider") in triage.COMPATIBLE and not str(settings.get("custom_model") or "").strip():
        raise ValueError("Choose a model for the AI provider (use Load models to see what's available)")
    if settings.get("provider") == "custom":
        problem = triage.custom_key_problem(str(settings.get("custom_key_env") or "").strip().upper())
        if problem:
            raise ValueError(problem)
        if not str(settings.get("custom_url") or "").strip().startswith(("http://", "https://")):
            raise ValueError("The custom provider's endpoint URL must start with http:// or https://")
        if not str(settings.get("custom_model") or "").strip():
            raise ValueError("Enter the model name for the custom provider")
    project = {
        "id": pid,
        "name": name or site.split("//", 1)[-1].rstrip("/"),
        "site": site,
        "settings": clean_settings(data.get("settings")),
        "created": existing["created"] if existing else _now(),
        "updated": _now(),
    }
    _write(_dir(pid) / "project.json", project)
    return project


def delete_project(pid):
    d = _dir(pid)
    if d.is_dir():
        shutil.rmtree(d)


def run_dir(pid, rid):
    d = _dir(pid, rid)
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_run(pid, rid, result, summary):
    d = run_dir(pid, rid)
    _write(d / "result.json", result)
    _write(d / "summary.json", summary)


def load_run(pid, rid):
    return _read(_dir(pid, rid) / "result.json")


def load_digest(pid, rid):
    path = _dir(pid, rid) / "digest.md"
    return path.read_text() if path.exists() else None


def load_analysis(pid):
    return _read(_dir(pid) / "analysis.json") or {}


def save_analysis(pid, cache):
    _write(_dir(pid) / "analysis.json", cache)
