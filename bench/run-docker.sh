#!/usr/bin/env bash
# Run the benchmark CLI inside the container, mounting this repo at /app.
#
#   bench/run-docker.sh discover --project data/re2-... --limit 20
#   bench/run-docker.sh run --project data/re2-... --out runs/re2 --sample 30
#
# Set BENCH_REBUILD=1 to force an image rebuild.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${BENCH_IMAGE:-deepagents-bench}"

if [[ "${BENCH_REBUILD:-0}" == "1" ]] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker build -t "$IMAGE" -f "$REPO_ROOT/bench/Dockerfile" "$REPO_ROOT"
fi

# No --env-file: docker keeps the quotes in a value like LLM_API_KEY="sk-...",
# and load_dotenv() inside the container will not override an already-set
# variable, so a quoted key would poison every run. The repo (including .env)
# is mounted at /app, where python-dotenv parses it correctly. Host variables
# are still forwarded so CI can override without touching the file.
ENV_ARGS=()
for name in LLM_BASE_URL LLM_API_KEY LLM_MODEL AGENT_RECURSION_LIMIT \
            LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY LANGFUSE_BASE_URL; do
  [[ -n "${!name:-}" ]] && ENV_ARGS+=(-e "$name=${!name}")
done

exec docker run --rm -it \
  -v "$REPO_ROOT:/app" \
  -w /app \
  "${ENV_ARGS[@]}" \
  "$IMAGE" "$@"
