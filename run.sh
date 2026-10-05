#!/usr/bin/env bash
# Nightly site sweep. Crawl, triage, mail. Changes nothing on the site.
set -euo pipefail

SITE="${SITE:?set SITE, e.g. https://www.example.gov}"
TO="${TO:?set TO, e.g. webteam@example.gov}"
OUT="${OUT:-/var/lib/nightly-sweep}"
STAMP="$(date +%Y-%m-%d)"

mkdir -p "$OUT"
cd "$(dirname "$0")"

python3 sweep.py  "$SITE" --out "$OUT/sweep-$STAMP.json" --max-pages 2000
python3 triage.py "$OUT/sweep-$STAMP.json" --out "$OUT/digest-$STAMP.md"

if command -v mail >/dev/null; then
  mail -s "Site sweep — $STAMP" "$TO" < "$OUT/digest-$STAMP.md"
else
  echo "No mail command; digest is at $OUT/digest-$STAMP.md"
fi
