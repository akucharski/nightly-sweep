#!/usr/bin/env python3
"""
Export the product landing page as a standalone static site.

    python3 export_landing.py                                   # -> ./public-landing
    python3 export_landing.py --app-url https://sweep.example.gov

On its own host the page has no dashboard behind it, so its Sign in and
Documentation links point at --app-url (default: the local dashboard). Fonts and
the Promet Source logo are copied alongside. Re-exporting keeps .spacefast/, so
`sf publish public-landing` keeps updating the same Space.
"""

import argparse
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-url", default="http://127.0.0.1:8765", help="where the dashboard runs")
    ap.add_argument("--out", default="public-landing")
    a = ap.parse_args()
    app = a.app_url.rstrip("/")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.iterdir():
        if old.name != ".spacefast":
            shutil.rmtree(old) if old.is_dir() else old.unlink()

    page = (HERE / "landing.html").read_text()
    page = (page.replace('url("/static/', 'url("static/').replace('src="/static/', 'src="static/')
                .replace('href="/login"', f'href="{app}/login"').replace('href="/docs"', f'href="{app}/docs"')
                .replace('<a class="brand" href="/"', '<a class="brand" href="./"')
                .replace("<!--ACCOUNT-->", f'<a class="btn btn-navy" href="{app}/login">Sign in</a>'))
    (out / "index.html").write_text(page)
    shutil.copytree(HERE / "static" / "fonts", out / "static" / "fonts")
    shutil.copy(HERE / "static" / "promet-logo.svg", out / "static" / "promet-logo.svg")
    print(f"Exported the landing page to {out}/ (Sign in -> {app}/login)")


if __name__ == "__main__":
    main()
