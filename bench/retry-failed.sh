#!/usr/bin/env bash
# Re-generate the methods a sweep failed to produce a file for.
#
#   bench/retry-failed.sh runs/glm53              # every project in the tree
#   bench/retry-failed.sh runs/glm53 ninja leveldb # only these
#
# Failures are not random: the methods that time out or exhaust the step budget
# carry several times the cyclomatic complexity of those that succeed, so
# leaving them out removes the hardest methods from the sample and inflates
# every rate. Neither failure mode is deterministic, so a second attempt
# recovers a good share of them.
#
# Safe to re-run: methods that already produced a file are never touched, and
# each retry reuses the original index so its row is replaced, not duplicated.
set -uo pipefail

OUT_ROOT="${1:-runs/suite}"
shift || true
ONLY=("$@")
WORKERS="${BENCH_RETRY_WORKERS:-12}"
# Restrict the retry to one failure mode. A timed-out request says nothing about
# the method and should be re-run; an exhausted step budget is the bounded agent
# behaving as designed, and re-running it would relax the budget by the back door.
CAUSE="${BENCH_RETRY_CAUSE:-}"

should_run() {
  [ "${#ONLY[@]}" -eq 0 ] && return 0
  for name in "${ONLY[@]}"; do [ "$name" = "$1" ] && return 0; done
  return 1
}

for manifest in "$OUT_ROOT"/*/manifest.json; do
  [ -f "$manifest" ] || continue
  out="$(dirname "$manifest")"
  short="$(basename "$out")"
  should_run "$short" || continue

  failed=$(CAUSE="$CAUSE" python -c "
import json, os, re
m = json.load(open('$manifest', encoding='utf-8'))
c = os.environ.get('CAUSE') or ''
p = re.compile(c, re.IGNORECASE) if c else None
print(sum(1 for e in m['entries']
          if not e['wrote_file'] and (not p or p.search(e.get('error') or ''))))
" 2>/dev/null || echo 0)

  if [ "$failed" -eq 0 ]; then
    echo "== $short: nothing to retry"
    continue
  fi

  project=$(python -c "
import json
print(json.load(open('$manifest', encoding='utf-8'))['project'])
" 2>/dev/null)
  [ -d "$project" ] || project=$(ls -d data/"$short"-* 2>/dev/null | head -1)
  if [ ! -d "$project" ]; then
    echo "== $short: project directory not found, skipping"
    continue
  fi

  echo "== $short: retrying $failed failed methods ($WORKERS workers) $(date +%H:%M:%S)"
  python -m bench.cli gen \
      --project "$project" --out "$out" \
      --retry-failed --retry-cause "$CAUSE" --workers "$WORKERS" --no-agent-fix \
      >> "$out/retry.log" 2>&1

  recovered=$(CAUSE="$CAUSE" python -c "
import json, os, re
m = json.load(open('$manifest', encoding='utf-8'))
c = os.environ.get('CAUSE') or ''
p = re.compile(c, re.IGNORECASE) if c else None
print(sum(1 for e in m['entries']
          if not e['wrote_file'] and (not p or p.search(e.get('error') or ''))))
" 2>/dev/null || echo "$failed")
  echo "== $short: $((failed - recovered))/$failed recovered, $recovered still failing"

  # Re-score so the recovered methods appear in results.csv.
  python -m bench.cli eval --out "$out" --repair-rounds 1 \
      --build-dir "$out/precheck" >> "$out/retry.log" 2>&1
  echo "== $short: re-scored $(date +%H:%M:%S)"
done

echo "== retry pass finished $(date +%H:%M:%S)"
