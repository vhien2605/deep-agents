#!/usr/bin/env bash
# The source study's mutation protocol on our tests.
#
# Two passes. First the two projects the source study itself reports, mutated
# exhaustively so the comparison rests on a census rather than an estimate.
# Then every project on a uniform sample of source lines, handed to the tool's
# own --lines option, because a full pass over all ten would take about four
# days: universalmutator compiles every candidate mutant itself, one at a
# time. Generated mutants are cached, so re-scoring costs only the scoring.
set -uo pipefail
ROOT="${1:-runs/glm53}"
LINES="${LINES:-1500}"
JOBS="${JOBS:-5}"
GEN="${GEN_JOBS:-6}"

score() {
  short="$1"; log="$2"; shift 2
  out="$ROOT/$short"
  [ -f "$out/rows.jsonl" ] || { echo "== $short: no rows, skip"; return; }
  [ -d "$out/build-llvm" ] || { echo "== $short: no build-llvm, skip"; return; }
  gen=$GEN; case "$short" in mlx|glomap) gen=2 ;; esac
  echo "== $short ($log): start $(date +%H:%M:%S)"
  python -m bench.cli mutate-citywalk --out "$out" --build-dir "$out/build-llvm" \
      --jobs "$JOBS" --gen-jobs "$gen" --resume "$@" > "$out/mutation-citywalk-$log.log" 2>&1
  rc=$?
  grep -E '"mutation_score"|"valid_mutants"|"survived_uncovered"' "$out/mutation-citywalk-$log.log" | sed 's/^/   /'
  echo "== $short ($log): done rc=$rc $(date +%H:%M:%S)"
}

# Pass 1 -- the two projects with a published figure, mutated exhaustively.
score tinyxml2 exhaustive
score json.cpp exhaustive --files json.cpp

# Pass 2 -- every project, sampled.
for short in papy hjson-cpp glomap json.cpp tinyxml2 yaml-cpp ninja re2 leveldb mlx; do
  score "$short" sampled --sample-lines "$LINES"
done
echo "== citywalk mutation suite finished $(date +%H:%M:%S)"
