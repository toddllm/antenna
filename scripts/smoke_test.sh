#!/usr/bin/env bash
# Antenna Phase 0 smoke test.
#
# Proves: feeds fetch, posts are stored, search works, digest renders,
# and the MCP server can answer one real query over stdio.
#
# Runs against a throwaway config in a temp dir. Does NOT touch your
# real antenna.db or send real email. Safe to run any time.
#
# Usage:
#   bash scripts/smoke_test.sh         # uses default test feeds
#   VERBOSE=1 bash scripts/smoke_test.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WORK="$(mktemp -d -t antenna-smoke.XXXXXX)"
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

say "Antenna smoke test"
echo "  repo:     $ROOT"
echo "  work dir: $WORK"
echo "  python:   $($PY --version 2>&1)"

# Make `antenna` importable from the repo without installing.
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

if ! "$PY" - <<'PY' >/dev/null 2>&1
import feedparser
import jinja2
import mcp
import yaml
PY
then
    bad "python env is missing Antenna deps. Try: $ROOT/.venv/bin/pip install -e \"$ROOT\" or rerun with PYTHON=/path/to/venv/bin/python"
fi

# 1. Write a minimal config pointing at a couple of reliable feeds.
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

default_mode: digest
first_run_entries: 3
poll_delay_seconds: 0.2

feeds:
  # Public RSS feeds that don't rate-limit CI / sandbox traffic.
  # Swap these for your real feeds when running locally.
  - url: https://pypi.org/rss/project/mcp/releases.xml
    title: "PyPI: mcp releases"
    tags: [python]
  - url: https://pypi.org/rss/project/feedparser/releases.xml
    title: "PyPI: feedparser releases"
    tags: [python]

rules:
  - match: "*"
YAML
ok "wrote test config"

export ANTENNA_CONFIG="$WORK/antenna.yaml"
ANT=( "$PY" -m antenna.cli )

# 2. init
say "init"
( cd "$WORK" && "${ANT[@]}" init )
[ -f "$WORK/antenna.db" ] || bad "antenna.db was not created"
ok "db created"

# 3. list-sources
say "list-sources"
( cd "$WORK" && "${ANT[@]}" list-sources )

# 4. fetch
say "fetch (hitting real feeds — needs network)"
if ! ( cd "$WORK" && "${ANT[@]}" -v fetch ); then
    bad "fetch failed. Check network or try different feeds."
fi

# 5. verify posts landed
say "verify posts in DB"
N_POSTS=$("$PY" - <<PY
import sqlite3, pathlib
c = sqlite3.connect(pathlib.Path("$WORK/antenna.db"))
print(c.execute("SELECT COUNT(*) FROM posts").fetchone()[0])
PY
)
echo "  posts: $N_POSTS"
[ "$N_POSTS" -gt 0 ] || bad "no posts were stored — fetch silently no-op'd"
ok "posts stored: $N_POSTS"

# 6. recent-posts
say "recent-posts"
( cd "$WORK" && "${ANT[@]}" recent-posts --limit 5 )

# 7. doctor — quick install/feed-health snapshot
say "doctor"
( cd "$WORK" && "${ANT[@]}" doctor )
( cd "$WORK" && "${ANT[@]}" doctor --json > "$WORK/doctor.json" )
grep -q '"configured_feeds": 2' "$WORK/doctor.json" || bad "doctor JSON did not report the configured feeds"
grep -q '"database_feeds": 2' "$WORK/doctor.json" || bad "doctor JSON did not report the seeded DB feeds"
ok "doctor reports config + DB health"

# 8. search — (a) sanity-check FTS5 is populated, (b) try a few likely terms.
say "search (FTS5)"
FTS_COUNT=$("$PY" - <<PY
import sqlite3, pathlib
c = sqlite3.connect(pathlib.Path("$WORK/antenna.db"))
print(c.execute("SELECT COUNT(*) FROM posts_fts").fetchone()[0])
PY
)
echo "  posts_fts rows: $FTS_COUNT  (posts: $N_POSTS)"
[ "$FTS_COUNT" = "$N_POSTS" ] || bad "FTS5 row count != posts row count — index drift"
ok "FTS5 index populated"

# Try a handful of terms likely to be present in the PyPI test feeds; pass on any hit.
FOUND=0
for term in "rss" "parser" "protocol" "model" "feed*" "mcp"; do
    OUT=$( cd "$WORK" && "${ANT[@]}" search "$term" --limit 3 2>&1 || true )
    if echo "$OUT" | grep -q "^  \["; then
        echo "  matched term: $term"
        echo "$OUT" | head -n 4
        FOUND=1
        break
    fi
done
[ $FOUND -eq 1 ] || bad "FTS5 search returned nothing for any test term"
ok "FTS5 search works end-to-end"

# 9a. add-feed (CLI path for adding a source manually)
say "add-feed"
( cd "$WORK" && "${ANT[@]}" add-feed "https://pypi.org/rss/project/jinja2/releases.xml" --title "PyPI: jinja2" --tags python,smoke )
AF_COUNT=$("$PY" - <<PY
import sqlite3, pathlib
c = sqlite3.connect(pathlib.Path("$WORK/antenna.db"))
print(c.execute("SELECT COUNT(*) FROM sources WHERE url LIKE '%jinja2%'").fetchone()[0])
PY
)
[ "$AF_COUNT" = "1" ] || bad "add-feed did not add the expected source"
ok "add-feed added one source"

# 9b. sync (source-scoped one-shot fetch + delivery)
say "sync (source-scoped, dry-run)"
( cd "$WORK" && "${ANT[@]}" sync --source-id 3 --mode per_post --dry-run --json > "$WORK/sync.json" )
"$PY" - "$WORK/sync.json" <<'PY'
import json, sys
report = json.load(open(sys.argv[1], "r", encoding="utf-8"))
assert report["source_id"] == 3, report
assert report["fetch"]["new_posts"] >= 1, report
assert report["email"]["emails_sent"] >= 1, report
assert report["email"]["emails_failed"] == 0, report
PY
ok "sync fetches + delivers in one headless step"

# 9c. import-opml (OPML import path)
say "import-opml"
cat > "$WORK/tiny.opml" <<'OPML'
<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>smoke opml</title></head>
  <body>
    <outline type="rss" text="PyPI: click" title="PyPI: click"
             xmlUrl="https://pypi.org/rss/project/click/releases.xml"
             category="python"/>
  </body>
</opml>
OPML
( cd "$WORK" && "${ANT[@]}" import-opml "$WORK/tiny.opml" )
IO_COUNT=$("$PY" - <<PY
import sqlite3, pathlib
c = sqlite3.connect(pathlib.Path("$WORK/antenna.db"))
print(c.execute("SELECT COUNT(*) FROM sources WHERE url LIKE '%click%'").fetchone()[0])
PY
)
[ "$IO_COUNT" = "1" ] || bad "import-opml did not add the expected source"
ok "import-opml added one source"

# 9d. render-digest — exercise both the populated and empty branches of the template
say "render-digest (populated, last 30d)"
( cd "$WORK" && "${ANT[@]}" render-digest --since 30d )
DIGEST_FILE=$(ls -1 "$WORK/outbox"/*digest*.html 2>/dev/null | head -n 1 || true)
[ -n "$DIGEST_FILE" ] || bad "no digest HTML was written to outbox/"
# Sanity: digest should render without runtime errors (e.g. missing Jinja filters).
grep -q "Delivered by Antenna" "$DIGEST_FILE" || bad "digest HTML missing footer marker — template broken?"
ok "populated digest rendered: $(basename "$DIGEST_FILE")"

say "render-digest (future cutoff — should hit the 'Nothing new' branch)"
( cd "$WORK" && "${ANT[@]}" render-digest --since 2099-01-01T00:00:00Z )
EMPTY_DIGEST=$(ls -1t "$WORK/outbox"/*digest*.html 2>/dev/null | head -n 1 || true)
[ -n "$EMPTY_DIGEST" ] || bad "no digest HTML was written for the empty-window case"
grep -q "Nothing new" "$EMPTY_DIGEST" || bad "empty digest did not render the 'Nothing new' branch"
ok "empty digest branch renders"

# 10a. send-email --mode per_post --dry-run
say "send-email --mode per_post --dry-run"
( cd "$WORK" && "${ANT[@]}" send-email --mode per_post --dry-run )
N_POST_FILES=$(ls -1 "$WORK/outbox"/*post*.html 2>/dev/null | wc -l | tr -d ' ')
echo "  per-post HTML files in outbox: $N_POST_FILES"
[ "$N_POST_FILES" -gt 0 ] || bad "per-post dry-run wrote zero files"
ok "per-post dry-run works"

# 10b. send-email --mode digest --dry-run  (multi-post path; writes one digest file)
say "send-email --mode digest --dry-run"
( cd "$WORK" && "${ANT[@]}" send-email --mode digest --dry-run )
DIGEST_DRY=$(ls -1t "$WORK/outbox"/*digest*.html 2>/dev/null | head -n 1 || true)
[ -n "$DIGEST_DRY" ] || bad "digest dry-run wrote no digest file"
grep -q "Delivered by Antenna" "$DIGEST_DRY" || bad "digest dry-run file missing footer marker"
ok "digest dry-run works"

# 11. MCP smoke: start serve-mcp, send one JSON-RPC call on stdin, check reply.
say "serve-mcp (one JSON-RPC call over stdio)"
MCP_OUT="$WORK/mcp.out"
MCP_ERR="$WORK/mcp.err"

timeout 30 "$PY" - "$WORK" > "$MCP_OUT" 2> "$MCP_ERR" <<'PY'
"""
Drive the Antenna MCP server via stdio: initialize, then call list_sources
and search_posts. Uses the MCP Python client so we speak the real framed
protocol the way a Claude Desktop / Claude Code client would.
"""
import asyncio, json, os, sys
from pathlib import Path

work = Path(sys.argv[1])
os.environ["ANTENNA_CONFIG"] = str(work / "antenna.yaml")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def parse_list(result):
    """FastMCP may return a tool's list result as either one JSON blob or
    one TextContent per item. Handle both."""
    # Prefer structuredContent when present.
    sc = getattr(result, "structuredContent", None)
    if sc is not None:
        # Some servers wrap list results as {"result": [...]}.
        if isinstance(sc, dict) and "result" in sc and isinstance(sc["result"], list):
            return sc["result"]
        if isinstance(sc, list):
            return sc
    texts = [c.text for c in result.content if hasattr(c, "text")]
    # Try joint parse first.
    joined = "\n".join(texts).strip()
    try:
        data = json.loads(joined)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        pass
    # Fall back: parse each block individually.
    out = []
    for t in texts:
        try:
            out.append(json.loads(t))
        except json.JSONDecodeError:
            out.append(t)
    return out


async def main():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "antenna.cli", "serve-mcp"],
        env={**os.environ},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print("TOOLS:", json.dumps(names))

            result = await session.call_tool("list_sources", {})
            sources = parse_list(result)
            print("N_SOURCES:", len(sources))
            if sources:
                print("FIRST_SOURCE_URL:", sources[0].get("url", "(n/a)"))

            result2 = await session.call_tool(
                "search_posts", {"query": "rss", "limit": 5}
            )
            hits = parse_list(result2)
            print("N_SEARCH_HITS:", len(hits))

asyncio.run(main())
PY

cat "$MCP_OUT"
if ! grep -q '^TOOLS:' "$MCP_OUT"; then
    echo "--- MCP stderr ---"
    tail -n 40 "$MCP_ERR"
    bad "MCP server did not respond to list_tools"
fi
for t in list_sources subscribe unsubscribe recent_posts search_posts get_post; do
    grep -q "\"$t\"" "$MCP_OUT" || bad "MCP tool missing: $t"
done
grep -q '^N_SOURCES: 4'      "$MCP_OUT" || { tail -n 40 "$MCP_ERR"; bad "list_sources did not return 4 rows (2 feeds + add-feed + import-opml)"; }
grep -q '^N_SEARCH_HITS: '   "$MCP_OUT" || { tail -n 40 "$MCP_ERR"; bad "search_posts did not return hit count"; }
ok "MCP server answered list_tools + list_sources + search_posts"

say "ALL GREEN"
echo "  work dir: $WORK (pass KEEP=1 to keep)"
