#!/usr/bin/env python3
"""
Read-only MCP connector onto this Sweeper installation's saved results.

Lets an MCP-aware agent (Claude Desktop, Claude Code, or anything else that
speaks MCP) ask questions about sweeps that have already run, without ever
touching the crawler, the AI keys, or a live site. Every tool here only reads
files under ./projects; none can start a sweep, change a setting, or write
anything. That is the claw: Read open, everything else closed, enforced by
what functions exist rather than by a check inside them.

This talks over stdio, so it only runs when a client on this same machine
spawns it, under your own OS user. There is no network listener and no
separate authentication: your filesystem permissions are the only gate.

Usage (the client spawns this; you don't run it by hand):
    .venv/bin/python mcp_server.py

Claude Desktop (~/Library/Application Support/Claude/claude_desktop_config.json):
    { "mcpServers": { "nightly-sweep": {
        "command": "/absolute/path/to/Sweeper/.venv/bin/python",
        "args": ["/absolute/path/to/Sweeper/mcp_server.py"] } } }

Claude Code:
    claude mcp add nightly-sweep -- /absolute/path/to/Sweeper/.venv/bin/python /absolute/path/to/Sweeper/mcp_server.py
"""

from __future__ import annotations

from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

import store

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

mcp = MCPServer(
    name="nightly-sweep",
    title="Nightly Sweep",
    instructions=(
        "Saved results from a website accuracy crawler: stale pages, past dates still written as "
        "upcoming, broken links, dead pages, and contradictions between pages, each judged live or "
        "dead by a model with a verbatim quote as evidence. This server is read-only: it cannot "
        "start a sweep, change a project, or touch the live site. Start with list_projects, then "
        "list_runs for one project, then get_run_report for a specific run. Every URL and quote "
        "here came from a real crawl; never invent one in your answer."
    ),
)


def _project_or_error(project_id: str) -> dict | None:
    return store.get_project(project_id)


def _run_or_error(project_id: str, run_id: str) -> dict | None:
    try:
        return store.load_run(project_id, run_id)
    except ValueError:
        return None


@mcp.tool(annotations=READ)
def list_projects() -> list[dict]:
    """List every saved project: its site, settings, and a summary of each of its runs.
    Start here. Use a project's "id" with the other tools."""
    out = []
    for p in store.list_projects():
        out.append({
            "id": p["id"], "name": p["name"], "site": p["site"],
            "ai_provider": p["settings"]["provider"],
            "runs": [{"run_id": r["id"], "started": r["started"], "status": r["status"],
                      "pages_crawled": r["counts"]["pages"]} for r in p["runs"]],
        })
    return out


@mcp.tool(annotations=READ)
def list_runs(project_id: str) -> dict:
    """Every saved run of one project, newest first, with headline counts. Use a run's
    "run_id" with get_run_report, get_findings, get_digest or compare_runs."""
    project = _project_or_error(project_id)
    if not project:
        return {"error": f"No project {project_id!r}. Call list_projects for valid ids."}
    return {"project": project["name"], "site": project["site"], "runs": store.list_runs(project_id)}


@mcp.tool(annotations=READ)
def get_run_report(project_id: str, run_id: str) -> dict:
    """The full results of one run: every flagged page (stale / outdated wording / broken
    links / expired events), every contradiction, and which pages the AI judged dead,
    unclear or live, with its reasons and quoted evidence. This is the main tool for
    answering "what did this sweep find"."""
    r = _run_or_error(project_id, run_id)
    if not r:
        return {"error": f"No run {run_id!r} for project {project_id!r}. Call list_runs to see valid ids."}
    findings = []
    for f in r["findings"]:
        v = r["verdicts"].get(f["url"], {})
        findings.append({
            "url": f["url"], "title": f["title"], "section": f["section"],
            "last_modified": f["last_modified"], "age_days": f["age_days"], "stale": f["stale"],
            "outdated_mentions": [m["quote"] for m in f["outdated_mentions"]],
            "broken_links": [{"url": b["url"], "status": b["status"]} for b in f["broken_links"]],
            "expired_events": f["expired_events"],
            "ai_verdict": v.get("verdict"), "ai_reason": v.get("reason"), "ai_evidence": v.get("evidence"),
        })
    return {
        "project": r["project_name"], "site": r["opts"]["site"], "run_id": r["run_id"],
        "started": r["started"], "status": r["status"], "ai_model": r.get("model"), "counts": r["counts"],
        "dead_pages": r["dead_pages"],      # HTTP 404/403, with which pages link to them
        "findings": findings,
        "contradictions": r["contradictions"],
    }


@mcp.tool(annotations=READ)
def get_digest(project_id: str, run_id: str) -> str:
    """The human-readable Markdown report for one run — the same text the nightly email
    sends. Good for summarizing a run in prose rather than querying its structured data."""
    text = store.load_digest(project_id, run_id)
    return text if text else f"No digest for {project_id}/{run_id}. Call list_runs to see valid ids."


Category = Literal["stale", "outdated", "dead", "unclear", "live", "broken_links", "expired_events", "dead_pages"]


@mcp.tool(annotations=READ)
def get_findings(project_id: str, run_id: str, category: Category, limit: int = 50) -> list[dict]:
    """One category of finding from a run, instead of everything get_run_report returns.
    category: "stale" (not edited recently), "outdated" (past dates still read as upcoming),
    "dead"/"unclear"/"live" (the AI's verdict on a page), "broken_links", "expired_events",
    or "dead_pages" (addresses answering 404/403)."""
    r = _run_or_error(project_id, run_id)
    if not r:
        return [{"error": f"No run {run_id!r} for project {project_id!r}."}]
    if category == "dead_pages":
        return r["dead_pages"][:limit]
    out = []
    for f in r["findings"]:
        v = r["verdicts"].get(f["url"], {})
        if category == "stale" and f["stale"]:
            out.append({"url": f["url"], "title": f["title"], "age_days": f["age_days"], "last_modified": f["last_modified"]})
        elif category == "outdated" and f["outdated_mentions"]:
            out.append({"url": f["url"], "title": f["title"], "quotes": [m["quote"] for m in f["outdated_mentions"]]})
        elif category == "broken_links" and f["broken_links"]:
            out.append({"url": f["url"], "title": f["title"], "broken_links": f["broken_links"]})
        elif category == "expired_events" and f["expired_events"]:
            out.append({"url": f["url"], "title": f["title"], "events": f["expired_events"]})
        elif category in ("dead", "unclear", "live") and v.get("verdict") == category:
            out.append({"url": f["url"], "title": f["title"], "reason": v.get("reason"), "evidence": v.get("evidence")})
        if len(out) >= limit:
            break
    return out


@mcp.tool(annotations=READ)
def compare_runs(project_id: str, run_id_a: str, run_id_b: str) -> dict:
    """What changed between two runs of the same project (any order; the earlier one by
    "started" is treated as "before"). New problems, problems that no longer appear
    (fixed or the page changed), and the count trend. Use list_runs to find run ids."""
    ra, rb = _run_or_error(project_id, run_id_a), _run_or_error(project_id, run_id_b)
    if not ra or not rb:
        return {"error": "One or both run ids weren't found for this project. Call list_runs to see valid ids."}
    before, after = (ra, rb) if ra["started"] <= rb["started"] else (rb, ra)

    def issues(run):
        out = {}
        for f in run["findings"]:
            tags = []
            if f["stale"]:
                tags.append("stale")
            if f["outdated_mentions"]:
                tags.append("outdated wording")
            if f["broken_links"]:
                tags.append("broken links")
            v = run["verdicts"].get(f["url"], {}).get("verdict")
            if v in ("dead", "unclear"):
                tags.append(f"ai: {v}")
            if tags:
                out[f["url"]] = tags
        return out

    before_issues, after_issues = issues(before), issues(after)
    new = sorted(set(after_issues) - set(before_issues))
    resolved = sorted(set(before_issues) - set(after_issues))
    changed = sorted(u for u in set(before_issues) & set(after_issues) if before_issues[u] != after_issues[u])
    return {
        "before": {"run_id": before["run_id"], "started": before["started"], "counts": before["counts"]},
        "after": {"run_id": after["run_id"], "started": after["started"], "counts": after["counts"]},
        "new_problem_pages": [{"url": u, "issues": after_issues[u]} for u in new],
        "resolved_pages": [{"url": u, "was": before_issues[u]} for u in resolved],
        "changed_pages": [{"url": u, "was": before_issues[u], "now": after_issues[u]} for u in changed],
    }


if __name__ == "__main__":
    mcp.run()
