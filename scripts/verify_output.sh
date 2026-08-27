#!/usr/bin/env bash
# Regression gate for the readability/modularity pass.
#   scripts/verify_output.sh capture  -> write the golden set
#   scripts/verify_output.sh check    -> re-run the full chain and diff against it
#
# Needs a populated data/ and a running Postgres. The golden set goes to .golden/
# (gitignored) by default; override with GOLDEN_DIR.
#
# Runs profile -> merge -> index so changes to the ingest write paths are covered,
# then the read commands that re-derive their output from Postgres.
set -uo pipefail
cd "$(dirname "$0")/.."
BASE="${GOLDEN_DIR:-$PWD/.golden}"
G="$BASE/golden"
OUT="$BASE/current"
MODE="${1:-check}"
export COLUMNS=100 TERM=dumb
[ "$MODE" = capture ] && OUT="$G"
mkdir -p "$OUT"

run () { local name="$1"; shift; "$@" > "$OUT/$name" 2>&1; }

for s in skout remi; do
  run "$s.profile.stdout" uv run poc profile --store "$s"
  cp "data/$s/profile.json" "$OUT/$s.profile.json"
  run "$s.merge.txt" uv run poc merge --store "$s"
  run "$s.index.txt" uv run poc index --store "$s"
done
for s in skout remi; do
  run "$s.report.txt"      uv run poc report --store "$s"
  run "$s.labels.txt"      uv run poc labels --store "$s"
  run "$s.labels.yaml.txt" uv run poc labels --store "$s" --yaml
done
run stores.txt       uv run poc stores
run show-query.txt   uv run poc show-query
run help.txt         uv run poc --help
run skout.facts.txt  uv run poc facts peanut-butter-protein-bar --store skout --no-live
run remi.facts.txt   uv run poc facts water-flosser --store remi --no-live
run skout.search.txt uv run poc search "cookies without peanuts" --store skout --exclude peanut --no-live --no-rerank
run remi.search.txt  uv run poc search "how long does the battery last" --store remi --no-live --no-rerank
run skout.eval.txt   uv run poc eval --store skout --no-rerank
run remi.eval.txt    uv run poc eval --store remi --no-rerank
run skout.evalcmp.txt uv run poc eval --store skout --compare-rerank

if [ "$MODE" = capture ]; then
  echo "captured $(ls "$G" | wc -l | tr -d ' ') golden files"
  exit 0
fi

# canon.py normalises two known pre-existing nondeterminisms; see its docstring.
canon () { python3 "$(dirname "$0")/canon_output.py" "$1"; }

fail=0
for f in "$G"/*; do
  b=$(basename "$f")
  if ! diff -q <(canon "$f") <(canon "$OUT/$b") >/dev/null 2>&1; then
    echo "DIFF: $b"
    diff <(canon "$f") <(canon "$OUT/$b") | head -25
    fail=1
  fi
done
[ $fail -eq 0 ] && echo "OK: all $(ls "$G" | wc -l | tr -d ' ') golden outputs identical"
exit $fail
