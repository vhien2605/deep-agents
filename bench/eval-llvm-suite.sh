#!/usr/bin/env bash
# Re-score every project with clang instrumentation and llvm-cov, the
# toolchain the source study used. The g++/gcov numbers cannot be compared
# against theirs: gcov counts the implicit exception arcs C++ lowering
# inserts around every allocating call, which inflates the branch
# denominator on exactly the allocation-heavy code these projects contain.
#
# Sequential on purpose: this VM has 8 GB and parallel builds OOM-killed
# dockerd three times on 2026-09-03. Cheapest projects run first so a
# sample of scored methods is available early.
#
# Each project's stale build tree is removed immediately before its rebuild
# rather than all at once: the disk holds one instrumented build, not ten.
set -uo pipefail
ROOT="${1:-runs/glm53}"
ORDER="${ORDER:-hjson-cpp papy json.cpp tinyxml2 re2 yaml-cpp ninja leveldb glomap mlx}"

# The mlx shards are gcc output from a run that no longer informs anything,
# and they alone hold more space than a clang build of any project needs.
rm -rf "$ROOT"/mlx/work-shard* "$ROOT"/mlx/precheck-s*

for short in $ORDER; do
  out="$ROOT/$short"
  [ -f "$out/manifest.json" ] || { echo "== $short: no manifest, skip"; continue; }
  project=$(python -c "import json;print(json.load(open('$out/manifest.json',encoding='utf-8'))['project'])" 2>/dev/null)
  [ -d "$project" ] || project=$(ls -d data/"$short"-* 2>/dev/null | head -1)
  [ -d "$project" ] || { echo "== $short: project dir missing, skip"; continue; }

  # mlx and glomap link the largest translation units in the suite; four
  # concurrent clang invocations on them is what the 8 GB ceiling refuses.
  jobs=4
  case "$short" in mlx|glomap) jobs=2 ;; esac

  echo "== $short: clearing stale gcc build $(date +%H:%M:%S)"
  rm -rf "$out/precheck" "$out/work" "$out/build-llvm"

  echo "== $short: eval start $(date +%H:%M:%S)"
  python -m bench.cli eval --out "$out" --project "$project" \
      --build-dir "$out/build-llvm" --jobs "$jobs" --repair-rounds 1 \
      > "$out/eval-llvm.log" 2>&1
  rc=$?
  tail -8 "$out/eval-llvm.log" | sed 's/^/   /'
  echo "== $short: done rc=$rc $(date +%H:%M:%S)  rows=$(wc -l < "$out/rows.jsonl" 2>/dev/null)"
done
echo "== llvm eval suite finished $(date +%H:%M:%S)"
