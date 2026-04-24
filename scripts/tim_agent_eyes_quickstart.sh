#!/usr/bin/env bash
# Tim-facing guided quickstart for the experimental Agent Eyes branch.
#
# This intentionally uses dry-run email only. It proves that a real local
# agent-eyes binary can extract ASCO page state and route it through Antenna.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ANTENNA_HOME="${ANTENNA_TIM_HOME:-$HOME/antenna-tim}"
AGENT_EYES_BIN="${AGENT_EYES_BIN:-agent-eyes}"
SOURCE_URL="${ANTENNA_TIM_URL:-https://www.asco.org/annual-meeting}"
SOURCE_TITLE="${ANTENNA_TIM_TITLE:-ASCO Annual Meeting}"
RUN_REPO_SMOKE="${RUN_REPO_SMOKE:-0}"

usage() {
    cat <<'USAGE'
Usage:
  OPENAI_API_KEY=... scripts/tim_agent_eyes_quickstart.sh --agent-eyes /path/to/agent-eyes

Options:
  --agent-eyes PATH     Local agent-eyes binary. Defaults to AGENT_EYES_BIN or agent-eyes on PATH.
  --home PATH           Antenna config/database/outbox directory. Defaults to ~/antenna-tim.
  --url URL             Hard source URL to extract. Defaults to ASCO Annual Meeting.
  --title TITLE         Source title. Defaults to "ASCO Annual Meeting".
  --with-repo-smoke     Also run scripts/smoke_test.sh before the Agent Eyes live path.
  -h, --help            Show this help.

Environment:
  OPENAI_API_KEY        Required for the real Agent Eyes extraction.
  ANTENNA_TIM_HOME      Optional default for --home.
  AGENT_EYES_BIN        Optional default for --agent-eyes.
USAGE
}

say() { printf "\n\033[1;36m▶ %s\033[0m\n" "$*"; }
ok()  { printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
bad() { printf "  \033[1;31m✗\033[0m %s\n" "$*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --agent-eyes)
            [ "$#" -ge 2 ] || bad "--agent-eyes requires a path"
            AGENT_EYES_BIN="$2"
            shift 2
            ;;
        --home)
            [ "$#" -ge 2 ] || bad "--home requires a path"
            ANTENNA_HOME="$2"
            shift 2
            ;;
        --url)
            [ "$#" -ge 2 ] || bad "--url requires a URL"
            SOURCE_URL="$2"
            shift 2
            ;;
        --title)
            [ "$#" -ge 2 ] || bad "--title requires a title"
            SOURCE_TITLE="$2"
            shift 2
            ;;
        --with-repo-smoke)
            RUN_REPO_SMOKE=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            bad "unknown argument: $1"
            ;;
    esac
done

resolve_executable() {
    case "$1" in
        /*|*/*)
            local expanded="${1/#\~/$HOME}"
            if [ -x "$expanded" ]; then
                printf '%s\n' "$expanded"
            fi
            ;;
        *)
            command -v "$1" || true
            ;;
    esac
}

DEFAULT_PY="python3"
if [ -x "$ROOT/.venv/bin/python" ]; then
    DEFAULT_PY="$ROOT/.venv/bin/python"
fi
PY="${PYTHON:-$DEFAULT_PY}"

say "Antenna + Agent Eyes guided quickstart"
echo "  repo:          $ROOT"
echo "  antenna home:  $ANTENNA_HOME"
echo "  source:        $SOURCE_TITLE <$SOURCE_URL>"

[ -n "${OPENAI_API_KEY:-}" ] || bad "OPENAI_API_KEY is not set. Export it in this shell before running the real extraction."
AGENT_EYES_PATH="$(resolve_executable "$AGENT_EYES_BIN")"
[ -n "$AGENT_EYES_PATH" ] || bad "agent-eyes binary not found or not executable: $AGENT_EYES_BIN"
echo "  agent-eyes:    $AGENT_EYES_PATH"

if ! "$PY" - <<'PY' >/dev/null 2>&1
import feedparser
import jinja2
import mcp
import yaml
PY
then
    bad "Python env is missing Antenna deps. From the repo, run: python3 -m venv .venv && source .venv/bin/activate && pip install -e ."
fi

if [ "$RUN_REPO_SMOKE" = "1" ]; then
    say "repo smoke test"
    bash "$ROOT/scripts/smoke_test.sh"
    ok "repo smoke passed"
fi

mkdir -p "$ANTENNA_HOME/logs"
CONFIG="$ANTENNA_HOME/antenna.yaml"

say "write Tim test config"
cat > "$CONFIG" <<YAML
database: antenna.db
outbox: outbox

smtp:
  host: smtp.example.invalid
  port: 587
  username: smoke@example.invalid
  password: "unused-dry-run-only"
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
  binary: $AGENT_EYES_PATH
  model: gpt-4o-mini
  timeout_seconds: 120
  sources:
    - url: $SOURCE_URL
      title: "$SOURCE_TITLE"
      tags: [agent-eyes-test]
      mode: state
      schema: [title, dates, location, important_links, page_type]
      stable_fields: [title, dates, location, page_type]
      instruction: >
        Extract the official conference title, official meeting dates, meeting
        location, public attendee links, and whether this is a conference
        landing page, listing page, or rolling feed. Ignore unrelated banner
        announcements, sign-in links, API endpoints, scripts, analytics links,
        and duplicate/internal technical links.
      settle_ms: 5000
      stealth: false
YAML
ok "config written: $CONFIG"

export ANTENNA_CONFIG="$CONFIG"
ANT=( "$PY" -m antenna.cli )

say "init"
( cd "$ANTENNA_HOME" && "${ANT[@]}" init )
ok "database initialized"

say "doctor"
( cd "$ANTENNA_HOME" && "${ANT[@]}" doctor )

say "real Agent Eyes extraction"
( cd "$ANTENNA_HOME" && "${ANT[@]}" fetch-agent-eyes --experimental-agent-eyes )
ok "hard source fetched"

say "recent posts"
( cd "$ANTENNA_HOME" && "${ANT[@]}" recent-posts --limit 5 )

say "dry-run digest"
( cd "$ANTENNA_HOME" && "${ANT[@]}" sync --experimental-agent-eyes --mode digest --dry-run --json > "$ANTENNA_HOME/tim-sync.json" )
DIGEST="$(ls -1t "$ANTENNA_HOME/outbox"/*digest*.html 2>/dev/null | head -n 1 || true)"
[ -n "$DIGEST" ] || bad "dry-run digest was not written"
ok "dry-run digest: $DIGEST"

say "summary"
"$PY" - "$ANTENNA_HOME/tim-sync.json" "$ANTENNA_HOME/antenna.db" <<'PY'
import json
import sqlite3
import sys

report = json.load(open(sys.argv[1], "r", encoding="utf-8"))
conn = sqlite3.connect(sys.argv[2])
conn.row_factory = sqlite3.Row
post = conn.execute("SELECT title, body_text FROM posts ORDER BY id DESC LIMIT 1").fetchone()
print(f"  sync_status: {report['status']}")
print(f"  agent_eyes_errors: {report['agent_eyes']['errors']}")
print(f"  emails_sent_dry_run: {report['email']['emails_sent']}")
print(f"  latest_title: {post['title']}")
print("  latest_excerpt:")
print("  " + (post["body_text"] or "").replace("\n", "\n  ")[:500])
if report["agent_eyes"]["errors"] != 0:
    raise SystemExit(1)
PY

ok "Tim quickstart completed"
