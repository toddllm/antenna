#!/usr/bin/env bash
# Antenna live feed matrix.
#
# Headless local validation against a small set of public feeds that cover
# real-world parser paths: RSS, Atom, Substack RSS, and JSON Feed.
#
# Usage:
#   bash scripts/live_feed_matrix.sh
#   PYTHON=/absolute/path/to/venv/bin/python bash scripts/live_feed_matrix.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WORK="$(mktemp -d -t antenna-live-matrix.XXXXXX)"
DEFAULT_PY="python3"
if [ -x "$ROOT/.venv/bin/python" ]; then
    DEFAULT_PY="$ROOT/.venv/bin/python"
fi
PY_INPUT="${PYTHON:-$DEFAULT_PY}"

cleanup() {
    rc=$?
    if [ "${KEEP:-0}" != "1" ]; then
        rm -rf "$WORK"
    else
        echo "  (kept work dir: $WORK)"
    fi
    exit $rc
}
trap cleanup EXIT

say() { printf "\n\033[1;36m▶ %s\033[0m\n" "$*"; }
ok()  { printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
bad() { printf "  \033[1;31m✗\033[0m %s\n" "$*"; exit 1; }

resolve_python() {
    case "$1" in
        /*) printf '%s\n' "$1" ;;
        */*) printf '%s\n' "$ROOT/$1" ;;
        *) command -v "$1" || true ;;
    esac
}

PY="$(resolve_python "$PY_INPUT")"
[ -n "$PY" ] && [ -x "$PY" ] || bad "python executable not found: $PY_INPUT"

say "Antenna live feed matrix"
echo "  repo:     $ROOT"
echo "  work dir: $WORK"
echo "  python:   $($PY --version 2>&1)"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

if ! "$PY" - <<'PY' >/dev/null 2>&1
import feedparser
import jinja2
import mcp
import yaml
PY
then
    bad "python env is missing Antenna deps. Try: $ROOT/.venv/bin/pip install -e \"$ROOT\""
fi

cat > "$WORK/antenna.yaml" <<'YAML'
database: antenna.db
outbox: outbox

smtp:
  host: smtp.example.invalid
  port: 587
  username: smoke@example.invalid
  password: "unused-we-only-dry-run"
  use_tls: true

email:
  from_address: smoke@example.invalid
  from_name_template: "{feed_title}"
  to_address: smoke+antenna@example.invalid

default_mode: per_post
first_run_entries: 1
poll_delay_seconds: 0.2

feeds: []
rules:
  - match: "*"
YAML

export ANTENNA_CONFIG="$WORK/antenna.yaml"
ANT=( "$PY" -m antenna.cli )

say "init"
( cd "$WORK" && "${ANT[@]}" init >/dev/null )
ok "db created"

FEEDS=(
  "rss|HNRSS frontpage|https://hnrss.org/frontpage|rss_or_atom"
  "rss|PyPI: feedparser releases|https://pypi.org/rss/project/feedparser/releases.xml|rss_or_atom"
  "atom|Home Assistant|https://www.home-assistant.io/atom.xml|rss_or_atom"
  "substack|The Innermost Loop|https://theinnermostloop.substack.com/feed|rss_or_atom"
  "json|Daring Fireball|https://daringfireball.net/feeds/json|json_feed"
)

for spec in "${FEEDS[@]}"; do
    IFS='|' read -r kind title url expected_format <<<"$spec"
    say "$kind :: $title"
    ( cd "$WORK" && "${ANT[@]}" add-feed "$url" --title "$title" >/dev/null )
    SID=$("$PY" - "$WORK/antenna.db" "$url" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
row = conn.execute("SELECT id FROM sources WHERE url = ?", (sys.argv[2],)).fetchone()
print(row[0] if row else "")
PY
)
    [ -n "$SID" ] || bad "could not resolve source id for $url"
    REPORT="$WORK/source-$SID.json"
    ( cd "$WORK" && "${ANT[@]}" sync --source-id "$SID" --dry-run --json > "$REPORT" )
    "$PY" - "$REPORT" "$expected_format" "$title" <<'PY'
import json, sys
report = json.load(open(sys.argv[1], "r", encoding="utf-8"))
expected_format = sys.argv[2]
title = sys.argv[3]
assert report["status"] == "ok", report
assert report["fetch"]["errors"] == 0, report
assert report["fetch"]["new_posts"] >= 1, report
assert report["email"]["emails_failed"] == 0, report
assert report["email"]["emails_sent"] >= 1, report
per_feed = report["fetch"]["per_feed"][0]
assert per_feed["format"] == expected_format, report
print(
    f"  format={per_feed['format']} new_posts={report['fetch']['new_posts']} "
    f"emails_sent={report['email']['emails_sent']} title={title}"
)
PY
    ok "$title passed"
done

say "ALL GREEN"
echo "  work dir: $WORK (pass KEEP=1 to keep)"
