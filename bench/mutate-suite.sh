#!/usr/bin/env bash
# Mutation analysis over every project of a finished sweep, sequentially,
# cheapest first so early results are available while the heavy projects
# compile. Runs inside the benchmark container:
#
#   docker run -d --name mutate --entrypoint bash -v "$PWD:/app" -w /app \
#       deepagents-bench bench/mutate-suite.sh runs/glm53
#
# Each project reuses the instrumented build its rows were scored against
# (build-llvm) and the test sources the scoring pass finally compiled.
set -uo pipefail
ROOT="${1:-runs/glm53}"
ORDER="${ORDER:-hjson-cpp papy tinyxml2 json.cpp leveldb ninja re2 yaml-cpp glomap mlx}"
MAX="${MAX_MUTANTS:-10}"

for short in $ORDER; do
  out="$ROOT/$short"
  [ -f "$out/rows.jsonl" ] || { echo "== $short: no rows, skip"; continue; }
  [ -d "$out/build-llvm" ] || { echo "== $short: no build-llvm, skip"; continue; }
  echo "== $short: mutate start $(date +%H:%M:%S)"
  python -m bench.cli mutate --out "$out" --build-dir "$out/build-llvm" \
      --max-mutants "$MAX" --resume > "$out/mutation.log" 2>&1
  rc=$?
  tail -3 "$out/mutation.log" | sed 's/^/   /'
  echo "== $short: done rc=$rc $(date +%H:%M:%S)  methods=$(wc -l < "$out/mutation.jsonl" 2>/dev/null)"
done
echo "== mutation suite finished $(date +%H:%M:%S)"
