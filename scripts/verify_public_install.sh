#!/usr/bin/env bash
# Verify Antenna can be staged as a clean public tree and installed from scratch.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WORK="$(mktemp -d -t antenna-public-verify.XXXXXX)"
STAGE_DIR="$WORK/repo"
DEFAULT_PY="python3"
if command -v python3.12 >/dev/null 2>&1; then
    DEFAULT_PY="$(command -v python3.12)"
elif [ -x "$ROOT/.venv/bin/python" ]; then
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

say "verify public install"
echo "  work dir: $WORK"
echo "  python:   $($PY --version 2>&1)"

"$ROOT/scripts/stage_public_tree.sh" "$STAGE_DIR"

say "create fresh venv"
"$PY" -m venv "$STAGE_DIR/.venv"
ok "venv created"

say "install from staged source tree"
"$STAGE_DIR/.venv/bin/pip" install --upgrade pip >/dev/null
( cd "$STAGE_DIR" && "$STAGE_DIR/.venv/bin/pip" install -e . )
ok "editable install succeeded"

say "check CLI"
( cd "$STAGE_DIR" && "$STAGE_DIR/.venv/bin/antenna" --help | sed -n '1,8p' )
ok "CLI is available in the fresh venv"

say "run unit tests"
( cd "$STAGE_DIR" && "$STAGE_DIR/.venv/bin/pip" install pytest >/dev/null )
( cd "$STAGE_DIR" && "$STAGE_DIR/.venv/bin/python" -m pytest tests -q )
ok "unit tests passed"

say "run smoke test"
( cd "$STAGE_DIR" && PYTHON="$STAGE_DIR/.venv/bin/python" bash scripts/smoke_test.sh )
ok "smoke test passed"

say "ALL GREEN"
echo "  staged repo: $STAGE_DIR"
echo "  pass KEEP=1 to inspect it afterward"
