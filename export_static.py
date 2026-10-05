#!/usr/bin/env python3
"""
Export a saved run as a read-only static site: the dashboard's charts, report and
exports for that run, plus the Documents guide. No server, no sign-in, no secrets.

    python3 export_static.py <project-id>                 # latest finished run
    python3 export_static.py <project-id> --run 20261004-172412 --out public

Publish the output folder anywhere static, for example: sf publish public
(re-exporting keeps public/.spacefast, so later publishes update the same Space)
"""

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import store
import throttle

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--run", help="run id (default: latest finished run)")
    ap.add_argument("--out", default="public")
    a = ap.parse_args()

    project = store.get_project(a.project)
    if not project:
        sys.exit(f"error: no project {a.project!r}. Projects: {', '.join(p['id'] for p in store.list_projects()) or 'none'}")
    runs = store.list_runs(project["id"])
    run_id = a.run or next((r["id"] for r in runs if r["status"] == "done"), None)
    result = store.load_run(project["id"], run_id) if run_id else None
    if not result:
        sys.exit("error: no finished run to export" + (f" (looked for {run_id})" if run_id else ""))

    project["runs"] = runs                       # summaries only, for the trend chart
    data = {"project": project, "result": result, "profiles": throttle.PROFILES,
            "published": datetime.now(timezone.utc).isoformat()}
    payload = json.dumps(data).replace("</", "<\\/")   # can't close the script tag early

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.iterdir():          # clear the last export, but keep .spacefast/ so
        if old.name == ".spacefast":   # `sf publish` keeps updating the same Space
            continue
        shutil.rmtree(old) if old.is_dir() else old.unlink()

    def localise(html):
        html = html.replace('href="/docs"', 'href="docs.html"').replace('href="/dashboard"', 'href="index.html"')
        start = html.find('<form method="post" action="/logout">')
        if start != -1:
            html = html[:start] + html[html.index("</form>", start) + len("</form>"):]
        return html

    page = localise((HERE / "dashboard.html").read_text())
    page = page.replace("<script>\nconst $ = ", f"<script>window.SWEEP_STATIC = {payload};</script>\n<script>\nconst $ = ", 1)
    (out / "index.html").write_text(page)
    (out / "docs.html").write_text(localise((HERE / "docs.html").read_text()))
    print(f"Exported {project['name']} run {run_id} ({result['counts']['pages']} pages) to {out}/")


if __name__ == "__main__":
    main()
