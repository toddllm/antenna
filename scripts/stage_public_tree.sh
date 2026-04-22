#!/usr/bin/env bash
# Stage a clean Antenna source tree for a public repo or fresh-install test.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${1:-$ROOT/dist/public-tree-$STAMP}"

say() { printf "\n\033[1;36m▶ %s\033[0m\n" "$*"; }
ok()  { printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
bad() { printf "  \033[1;31m✗\033[0m %s\n" "$*"; exit 1; }

SECRET_PATTERNS=(
    'AKIA''[0-9A-Z]{16}'
    'aws_secret_access_''key'
    'AWS_SECRET_ACCESS_''KEY'
    'ghp_''[A-Za-z0-9]{30,}'
    'github_pat_''[A-Za-z0-9_]{20,}'
    'sk-''[A-Za-z0-9_-]{20,}'
    'xoxb-''[A-Za-z0-9-]{20,}'
    'BEGIN RSA PRIVATE'' KEY'
    'BEGIN OPENSSH PRIVATE'' KEY'
)

if [ -e "$OUT_DIR" ]; then
    bad "output path already exists: $OUT_DIR"
fi

say "stage public tree"
mkdir -p "$OUT_DIR/scripts"

cp "$ROOT/.gitignore" "$OUT_DIR/"
cp "$ROOT/LICENSE" "$ROOT/README.md" "$ROOT/pyproject.toml" "$ROOT/requirements.txt" \
   "$ROOT/antenna.example.yaml" "$OUT_DIR/"
cp -R "$ROOT/antenna" "$OUT_DIR/"
cp -R "$ROOT/tests" "$OUT_DIR/"
cp "$ROOT/scripts/com.antenna.fetch.plist" \
   "$ROOT/scripts/live_feed_matrix.sh" \
   "$ROOT/scripts/stage_public_tree.sh" \
   "$ROOT/scripts/smoke_test.sh" \
   "$ROOT/scripts/verify_public_install.sh" \
   "$OUT_DIR/scripts/"

find "$OUT_DIR" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "$OUT_DIR" -type f \( -name '*.pyc' -o -name '.DS_Store' \) -delete
rm -rf "$OUT_DIR/antenna.egg-info" "$OUT_DIR/build" "$OUT_DIR/dist"
ok "copied curated source tree to $OUT_DIR"

say "validate staged contents"
for required in \
    "$OUT_DIR/README.md" \
    "$OUT_DIR/LICENSE" \
    "$OUT_DIR/pyproject.toml" \
    "$OUT_DIR/antenna/cli.py" \
    "$OUT_DIR/scripts/smoke_test.sh" \
    "$OUT_DIR/tests/test_fetch_behavior.py"; do
    [ -e "$required" ] || bad "missing required file: $required"
done

for forbidden in \
    "$OUT_DIR/antenna.yaml" \
    "$OUT_DIR/antenna.db" \
    "$OUT_DIR/antenna.db-wal" \
    "$OUT_DIR/antenna.db-shm" \
    "$OUT_DIR/outbox" \
    "$OUT_DIR/logs" \
    "$OUT_DIR/tmp" \
    "$OUT_DIR/drafts" \
    "$OUT_DIR/docs" \
    "$OUT_DIR/infra" \
    "$OUT_DIR/site"; do
    [ ! -e "$forbidden" ] || bad "forbidden path leaked into staged tree: $forbidden"
done
ok "staged tree excludes local state and internal-only directories"

say "secret scan"
GREP_ARGS=(
    -rE
    --include='*.py'
    --include='*.yaml'
    --include='*.yml'
    --include='*.md'
    --include='*.sh'
    --include='*.txt'
)
for pattern in "${SECRET_PATTERNS[@]}"; do
    GREP_ARGS+=(-e "$pattern")
done

if grep "${GREP_ARGS[@]}" "$OUT_DIR"; then
    bad "possible secret in staged tree"
fi
ok "no obvious secrets found"

echo
echo "Staged public tree ready:"
echo "  $OUT_DIR"
