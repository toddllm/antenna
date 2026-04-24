#!/usr/bin/env bash
# Experimental Agent Eyes bridge smoke test.
#
# Proves the Antenna-side integration without requiring a real Agent Eyes
# install, browser session, OpenAI API key, or third-party website. The script
# creates a fake agent-eyes executable that returns deterministic JSON, then
# verifies fetch-agent-eyes, dedupe, sync, dry-run digest output, and doctor.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WORK="$(mktemp -d -t antenna-agent-eyes-smoke.XXXXXX)"
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

say "Antenna Agent Eyes bridge smoke test"
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

cat > "$WORK/fake-agent-eyes" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" != "extract" ]; then
  echo "expected extract subcommand" >&2
  exit 1
fi
cat <<'JSON'
{
  "title": "ASCO Annual Meeting",
  "dates": "May 29 - June 2, 2026",
  "location": "Chicago, IL",
  "important_links": ["https://www.asco.org/annual-meeting"],
  "page_type": "conference landing page"
}
JSON
SH
chmod +x "$WORK/fake-agent-eyes"

cat > "$WORK/antenna.yaml" <<YAML
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

default_mode: digest
first_run_entries: 3
poll_delay_seconds: 0

feeds: []

rules:
  - match: "*"

experimental_agent_eyes:
  binary: $WORK/fake-agent-eyes
  model: gpt-4o-mini
  timeout_seconds: 10
  sources:
    - url: https://www.asco.org/annual-meeting
      title: "ASCO Annual Meeting"
      tags: [oncology, conference]
      mode: state
      schema: [title, dates, location, important_links, page_type]
      stable_fields: [title, dates, location, page_type]
      instruction: >
        Extract the conference title, dates, location, important links, and
        whether this is a conference landing page or a rolling news feed.
YAML

export ANTENNA_CONFIG="$WORK/antenna.yaml"
export OPENAI_API_KEY="fake-key-used-only-for-doctor-preflight"
ANT=( "$PY" -m antenna.cli )

say "init"
( cd "$WORK" && "${ANT[@]}" init )
[ -f "$WORK/antenna.db" ] || bad "antenna.db was not created"
ok "db created"

say "doctor preflight"
( cd "$WORK" && "${ANT[@]}" doctor --json > "$WORK/doctor.json" )
"$PY" - "$WORK/doctor.json" <<'PY'
import json, sys
report = json.load(open(sys.argv[1], "r", encoding="utf-8"))
assert report["agent_eyes"]["enabled"] is True, report
assert report["agent_eyes"]["binary_found"] is True, report
assert report["agent_eyes"]["openai_api_key_present"] is True, report
assert report["agent_eyes"]["sources_configured"] == 1, report
PY
ok "doctor sees Agent Eyes binary + key"

say "fetch-agent-eyes"
( cd "$WORK" && "${ANT[@]}" fetch-agent-eyes --experimental-agent-eyes --json > "$WORK/agent-eyes-1.json" )
"$PY" - "$WORK/agent-eyes-1.json" "$WORK/antenna.db" <<'PY'
import json, sqlite3, sys
summary = json.load(open(sys.argv[1], "r", encoding="utf-8"))
assert summary["polled"] == 1, summary
assert summary["new_posts"] == 1, summary
assert summary["errors"] == 0, summary
conn = sqlite3.connect(sys.argv[2])
conn.row_factory = sqlite3.Row
post = conn.execute("SELECT title, body_text FROM posts").fetchone()
assert post["title"] == "ASCO Annual Meeting", dict(post)
assert "conference landing page" in post["body_text"], dict(post)
source = conn.execute("SELECT tags_json FROM sources").fetchone()
assert "agent-eyes" in source["tags_json"], dict(source)
PY
ok "snapshot inserted as an Antenna post"

say "dedupe"
( cd "$WORK" && "${ANT[@]}" fetch-agent-eyes --experimental-agent-eyes --json > "$WORK/agent-eyes-2.json" )
"$PY" - "$WORK/agent-eyes-2.json" <<'PY'
import json, sys
summary = json.load(open(sys.argv[1], "r", encoding="utf-8"))
assert summary["polled"] == 1, summary
assert summary["new_posts"] == 0, summary
assert summary["errors"] == 0, summary
PY
ok "stable fields prevent duplicate snapshots"

say "sync dry-run digest"
( cd "$WORK" && "${ANT[@]}" sync --experimental-agent-eyes --mode digest --dry-run --json > "$WORK/sync.json" )
"$PY" - "$WORK/sync.json" <<'PY'
import json, sys
report = json.load(open(sys.argv[1], "r", encoding="utf-8"))
assert report["status"] == "ok", report
assert report["agent_eyes"]["errors"] == 0, report
assert report["email"]["emails_sent"] == 1, report
assert report["email"]["posts_marked_delivered"] == 1, report
PY
DIGEST_FILE=$(ls -1 "$WORK/outbox"/*digest*.html 2>/dev/null | head -n 1 || true)
[ -n "$DIGEST_FILE" ] || bad "no digest dry-run HTML was written"
grep -q "ASCO Annual Meeting" "$DIGEST_FILE" || bad "digest does not include Agent Eyes snapshot"
ok "dry-run digest includes Agent Eyes snapshot"

say "normal fetch skips Agent Eyes source"
( cd "$WORK" && "${ANT[@]}" -v fetch > "$WORK/fetch.txt" )
grep -q "skipped: Agent Eyes source" "$WORK/fetch.txt" || bad "normal fetch did not skip Agent Eyes source"
ok "normal RSS fetch leaves Agent Eyes source alone"

say "ALL GREEN"
