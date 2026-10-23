#!/usr/bin/env bash
# Run the whole CITYWALK benchmark suite: every project under data/, one after
# another, under the paper's configuration.
#
#   bench/run-suite.sh runs/glm-suite
#
# Projects are swept sequentially rather than in parallel: each one builds an
# instrumented copy of itself and the workers already saturate the model
# endpoint, so overlapping projects would only make both slower. A project that
# fails to build is reported and skipped — one bad build must not take the
# suite down with it.
set -uo pipefail

OUT_ROOT="${1:-runs/suite}"
WORKERS="${BENCH_WORKERS:-8}"
JOBS="${BENCH_JOBS:-4}"

mkdir -p "$OUT_ROOT"
SUMMARY="$OUT_ROOT/suite.log"
: > "$SUMMARY"

for project in data/*/; do
  project="${project%/}"
  [ -d "$project" ] || continue
  name="$(basename "$project")"
  short="${name%-*}"
  out="$OUT_ROOT/$short"

  if [ -f "$out/results.csv" ]; then
    echo "== $short: already done, skipping" | tee -a "$SUMMARY"
    continue
  fi

  # Check the project builds before generating anything for it. Generation is
  # the expensive half, and a project whose library will not compile can never
  # be scored, so tokens spent on it are wasted. The build is kept and reused
  # by the eval phase rather than repeated.
  echo "== $short: checking build $(date +%H:%M:%S)" | tee -a "$SUMMARY"
  mkdir -p "$out"
  if ! python - "$project" "$out/precheck" <<'PRECHECK' 2>&1 | tee -a "$SUMMARY"
import sys
from pathlib import Path
from bench.harness import build_instrumented
project, build_dir = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
built, last = build_instrumented(project, build_dir, jobs=4)
if built is None:
    print("     build failed:", (last.stderr or last.stdout)[-300:].replace("\n", " "))
    sys.exit(1)
print(f"     build ok: {[p.name for p in built.libraries][:3]}")
PRECHECK
  then
    echo "== $short: SKIPPED — instrumented build failed" | tee -a "$SUMMARY"
    continue
  fi

  echo "== $short: generating $(date +%H:%M:%S)" | tee -a "$SUMMARY"
  python -m bench.cli run \
      --project "$project" \
      --out "$out" \
      --workers "$WORKERS" \
      --jobs "$JOBS" \
      --citywalk-focal \
      --paper-sample \
      --no-agent-fix \
      --repair-rounds 1 \
      --build-dir "$out/precheck" \
      > "$out/run.log" 2>&1
  status=$?

  if [ $status -eq 0 ]; then
    rows=$(( $(wc -l < "$out/rows.jsonl" 2>/dev/null || echo 1) ))
    echo "== $short: done, $rows rows $(date +%H:%M:%S)" | tee -a "$SUMMARY"
  else
    echo "== $short: FAILED (exit $status) — see $out/run.log" | tee -a "$SUMMARY"
    tail -5 "$out/run.log" | sed 's/^/     /' | tee -a "$SUMMARY"
  fi
done

echo "== suite finished $(date +%H:%M:%S)" | tee -a "$SUMMARY"
