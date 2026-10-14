"""CLI entrypoint: run the C++ unit-test deep agent against a project.

Usage:
    uv run main.py --project examples/demo_calculator
    uv run main.py --project /path/to/cpp/project --coverage 90 \
        --task "Only generate tests for src/parser.cpp"

Model config comes from .env: LLM_PROVIDER, LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
(OpenAI-compatible endpoint). See .env.example.
"""

import argparse
import sys
from pathlib import Path

from src import create_cpp_unittest_agent, tracing
from src.config import DEFAULT_COVERAGE_TARGET, DEFAULT_RECURSION_LIMIT


def _print_update(update: dict) -> None:
    """Print a readable trace of each graph-node update."""
    for node, payload in update.items():
        if not isinstance(payload, dict):
            continue
        for message in payload.get("messages", []):
            kind = getattr(message, "type", "")
            content = getattr(message, "content", "")
            if kind == "ai":
                for call in getattr(message, "tool_calls", []) or []:
                    print(f"[{node}] -> tool {call['name']}({str(call['args'])[:200]})")
                text = content if isinstance(content, str) else " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict)
                )
                if text.strip():
                    print(f"[{node}] {text.strip()}\n")
            elif kind == "tool":
                text = content if isinstance(content, str) else str(content)
                print(f"[{node}] <- {getattr(message, 'name', 'tool')}: {text[:400]}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate C++ unit tests with a deep agent.")
    parser.add_argument("--project", required=True, help="Path to the C++ project root (must contain a Makefile).")
    parser.add_argument("--coverage", type=float, default=DEFAULT_COVERAGE_TARGET, help="Target line coverage percent.")
    parser.add_argument("--task", default=None, help="Optional extra instructions appended to the default task.")
    parser.add_argument(
        "--recursion-limit",
        type=int,
        default=DEFAULT_RECURSION_LIMIT,
        help=(
            "LangGraph super-step budget. One agent turn costs 3 steps, so this "
            f"allows roughly limit/3 tool calls (default {DEFAULT_RECURSION_LIMIT}, "
            "override with AGENT_RECURSION_LIMIT)."
        ),
    )
    args = parser.parse_args()

    agent = create_cpp_unittest_agent(args.project, coverage_target=args.coverage)

    task = (
        f"Generate unit tests for this C++ project until line coverage of the "
        f"production sources reaches at least {args.coverage}%. Follow your "
        f"workflow: analyze, generate, build & fix, measure, iterate."
    )
    if args.task:
        task += f"\n\nAdditional instructions: {args.task}"

    try:
        with tracing.trace_attributes(
            trace_name="cli-unittest-run",
            tags=["cli"],
            metadata={
                "project_root": str(Path(args.project).resolve())[:200],
                "coverage_target": str(args.coverage),
            },
        ):
            for update in agent.stream(
                {"messages": [{"role": "user", "content": task}]},
                stream_mode="updates",
                config={
                    "recursion_limit": args.recursion_limit,
                    "callbacks": tracing.langchain_callbacks(),
                    "run_name": "cli-unittest-run",
                },
            ):
                _print_update(update)
    finally:
        tracing.flush()

    print("=" * 72)
    print("Run finished. See trace above for the final coverage summary.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
