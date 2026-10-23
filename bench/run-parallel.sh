#!/usr/bin/env bash
# Sweep several projects at once, each in its own process.
#
#   bench/run-parallel.sh runs/suite
#
# Running projects one after another wastes the worker pool: hjson-cpp has 25
# focal methods, so a 64-worker sweep leaves 39 workers idle for its whole
# duration. Projects are independent — separate builds, separate output trees,
# separate test files — so they are swept concurrently instead, with the worker
# count per project scaled to how many methods it actually has.
#
# The endpoint, not this machine, is the limit: it sustained 192 concurrent
# requests without an error in testing, so the per-project worker counts below
# are sized to stay near that in aggregate.
set -uo pipefail

OUT_ROOT="${1:-runs/suite}"
MAX_PARALLEL="${BENCH_MAX_PARALLEL:-4}"

mkdir -p "$OUT_ROOT"
SUMMARY="$OUT_ROOT/suite.log"

# Workers per project, by focal-method count: a small project cannot use more
# workers than it has methods, and handing it 64 only starves the others.
# Sized from a measured failure rate, not from endpoint probing. A probe with
# 30-token prompts sustained 192 concurrent requests without error, and 160
# concurrent agents was extrapolated from it — that run lost 39-66% of methods
# to timeouts, because a real agent sends prompts of tens of thousands of
# tokens and saturates the endpoint far earlier. The 8-worker configuration
# that lost only 7.8% is the reference point these are scaled from.
workers_for() {
  case "$1" in
    leveldb|ninja|mlx|yaml-cpp) echo 16 ;;
    re2|tinyxml2)               echo 12 ;;
    *)                          echo 8 ;;
  esac
}

# Largest first: the long poles start earliest and the short ones fill in
# around them, instead of a 4-hour project starting last.
ORDER="ninja leveldb mlx yaml-cpp tinyxml2 re2 json.cpp papy hjson-cpp glomap"

sweep_one() {
  local short="$1" project="$2" out="$3" workers="$4"
  {
    echo "== $short: build check $(date +%H:%M:%S)"
    mkdir -p "$out"
    if ! python - "$project" "$out/precheck" <<'PRECHECK'
import sys
from pathlib import Path
from bench.harness import build_instrumented
project, build_dir = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
built, last = build_instrumented(project, build_dir, jobs=4)
if built is None:
    print("   build failed:", (last.stderr or last.stdout)[-250:].replace("\n", " "))
    sys.exit(1)
print(f"   build ok: {[p.name for p in built.libraries][:3]}")
PRECHECK
    then
      echo "== $short: SKIPPED — build failed"
      return 1
    fi
    echo "== $short: sweeping with $workers workers $(date +%H:%M:%S)"
    python -m bench.cli run \
        --project "$project" --out "$out" \
        --workers "$workers" --jobs 4 \
        --citywalk-focal --paper-sample --no-agent-fix --repair-rounds 1 \
        --build-dir "$out/precheck" > "$out/run.log" 2>&1
    # A failed method is systematically a complex one, so leaving it out
    # inflates every rate. One retry pass recovers most of them; the timeouts
    # and step-budget exhaustions that caused them are not deterministic.
    if [ $? -eq 0 ] && [ "${BENCH_RETRY:-1}" = "1" ]; then
      failed=$(python -c "
import json,sys
try:
    m=json.load(open('$out/manifest.json',encoding='utf-8'))
    print(sum(1 for e in m['entries'] if not e['wrote_file']))
except Exception: print(0)
")
      if [ "$failed" -gt 0 ]; then
        echo "== $short: retrying $failed failed methods $(date +%H:%M:%S)"
        python -m bench.cli gen --project "$project" --out "$out"             --retry-failed --workers "$workers"             --no-agent-fix >> "$out/run.log" 2>&1
        python -m bench.cli eval --out "$out" --repair-rounds 1             --build-dir "$out/precheck" >> "$out/run.log" 2>&1
      fi
    fi
    if [ $? -eq 0 ]; then
      echo "== $short: done, $(wc -l < "$out/rows.jsonl" 2>/dev/null || echo 0) rows $(date +%H:%M:%S)"
    else
      echo "== $short: FAILED — see $out/run.log"
    fi
  } >> "$out/status.log" 2>&1
}

PIDS=()
for short in $ORDER; do
  project=$(ls -d data/"$short"-* 2>/dev/null | head -1)
  [ -n "$project" ] || continue
  out="$OUT_ROOT/$short"
  if [ -f "$out/results.csv" ]; then
    echo "== $short: already done, skipping" | tee -a "$SUMMARY"
    continue
  fi

  # Block until a slot frees up, so at most MAX_PARALLEL builds and sweeps
  # compete for the machine at once.
  # `jobs -rp` reports nothing in a non-interactive shell (job control is off),
  # so the limit never engaged and every project launched at once — the very
  # overload this gate exists to prevent. Track PIDs directly. The length guard
  # matters under `set -u`, where expanding an empty array aborts the script.
  while [ "${#PIDS[@]}" -gt 0 ]; do
    alive=0
    for pid in "${PIDS[@]}"; do
      kill -0 "$pid" 2>/dev/null && alive=$((alive + 1))
    done
    [ "$alive" -lt "$MAX_PARALLEL" ] && break
    sleep 10
  done

  workers=$(workers_for "$short")
  # Create the directory before launching: the block redirect in sweep_one is
  # opened by the shell before the mkdir inside it can run.
  mkdir -p "$out"
  echo "-> launching $short ($workers workers)" | tee -a "$SUMMARY"
  sweep_one "$short" "$project" "$out" "$workers" &
  PIDS+=($!)
  sleep 5   # stagger the builds so they do not all hit cmake at once
done

wait
for short in $ORDER; do
  [ -f "$OUT_ROOT/$short/status.log" ] && cat "$OUT_ROOT/$short/status.log" >> "$SUMMARY"
done
echo "== all projects finished $(date +%H:%M:%S)" | tee -a "$SUMMARY"
