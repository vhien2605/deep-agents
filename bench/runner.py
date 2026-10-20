"""Drive the deep agent once per focal method and collect what it cost.

Generation is deliberately separate from measurement: this module only produces
test files and cost metrics, and `bench.harness` scores them afterwards. That
split means a scoring bug can be fixed and every row recomputed without paying
for generation again.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path

from bench.discover import FocalMethod

# Where generated tests land inside the project under test. Kept out of the
# project's own test directory so discovery never mistakes them for production
# code and the tree can be wiped between sweeps.
GENERATED_TESTS_DIRNAME = "bench_generated_tests"

TASK_TEMPLATE = """\
Write GoogleTest unit tests for exactly ONE function of this C++ project.

Focal function
  file:      {file}
  signature: {signature}
  lines:     {start_line}-{end_line} (as of the current file contents)
  name:      {name}

Requirements — the benchmark measures these, so follow them exactly:
1. Write the tests to `{output_path}` and to no other file. Do not modify any
   production source, build file, or existing test.
2. Use GoogleTest: `#include <gtest/gtest.h>` and `TEST(SuiteName, CaseName)`
   blocks. Do not write a `main()`; the harness links gtest_main.
3. Every case must exercise `{name}` — directly, or through the smallest public
   entry point that reaches it if the function is not publicly reachable.
4. Cover the branches of the focal function. It has cyclomatic complexity
   {complexity} ({branch_count} branch points), so aim for at least that many
   meaningfully distinct cases where the inputs allow it.
5. Include whatever project headers the code needs, using paths as the project
   itself includes them.
6. Assertions must encode the behaviour you determined from reading the source.
   Never weaken an assertion just to make it pass.

Start by reading the focal function and the headers that declare its types.
When the test file is written, stop — the harness compiles and runs it.
{build_policy}"""

# Appended when the benchmark enforces the repair budget itself. The agent
# keeps a shell through its backend, so the prohibition is stated rather than
# merely implied by withholding the build tools; runs are audited afterwards
# for build artefacts to confirm it held.
NO_BUILD_POLICY = """
Do NOT compile, build, link, or run anything — no g++, no make, no cmake, no
test binary, not through any tool and not through the shell. You get exactly
one attempt at the file: write it correctly from reading the source. Repair,
if any, happens outside this run.
"""


@dataclass
class GenerationResult:
    """What one agent run produced and what it cost."""

    focal: FocalMethod
    output_path: Path
    wrote_file: bool = False
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0
    fix_phase: int = 0
    model: str = ""
    error: str = ""
    tool_calls: dict = field(default_factory=dict)


def output_path_for(project_root: Path, focal: FocalMethod, index: int) -> Path:
    """A collision-free test path derived from the focal method's identity.

    Method names alone collide (overloads, same name in different files), so the
    file stem carries the source stem, a sanitised method name, and the index.
    """
    safe_name = "".join(c if c.isalnum() else "_" for c in focal.name).strip("_")
    stem = Path(focal.file).stem
    return project_root / GENERATED_TESTS_DIRNAME / f"test_{stem}_{safe_name}_{index}.cpp"


def _usage_from(message) -> tuple[int, int, int]:
    """(input, output, total) tokens for one AI message, zeros when absent."""
    usage = getattr(message, "usage_metadata", None) or {}
    if not usage:
        metadata = getattr(message, "response_metadata", None) or {}
        usage = metadata.get("token_usage") or metadata.get("usage") or {}
        return (
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
            int(usage.get("total_tokens", 0)),
        )
    input_tokens = int(usage.get("input_tokens", 0))
    output_tokens = int(usage.get("output_tokens", 0))
    total = int(usage.get("total_tokens", 0)) or input_tokens + output_tokens
    return input_tokens, output_tokens, total


def _model_from(message) -> str:
    metadata = getattr(message, "response_metadata", None) or {}
    return str(metadata.get("model_name") or metadata.get("model") or "")


# Tool names that mean the agent went back to the compiler — the signal for
# how many build/fix rounds a run needed.
BUILD_TOOL_NAMES = {"make_target", "run_command", "run_test_binary"}


def generate_for_method(
    agent,
    project_root: Path,
    focal: FocalMethod,
    index: int,
    *,
    recursion_limit: int,
    stream_callback=None,
    allow_build: bool = True,
) -> GenerationResult:
    """Run the agent for one focal method and return the run's cost metrics."""
    output_path = output_path_for(project_root, focal, index)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)

    relative_output = output_path.relative_to(project_root).as_posix()
    task = TASK_TEMPLATE.format(
        file=focal.file,
        signature=focal.signature,
        start_line=focal.start_line,
        end_line=focal.end_line,
        name=focal.name,
        complexity=focal.complexity,
        branch_count=focal.branch_count,
        output_path=relative_output,
        build_policy="" if allow_build else NO_BUILD_POLICY,
    )

    result = GenerationResult(focal=focal, output_path=output_path)
    started = time.monotonic()
    build_calls = 0
    try:
        for update in agent.stream(
            {"messages": [{"role": "user", "content": task}]},
            stream_mode="updates",
            config={"recursion_limit": recursion_limit, "run_name": "bench-gen"},
        ):
            for _node, payload in update.items():
                if not isinstance(payload, dict):
                    continue
                for message in payload.get("messages", []) or []:
                    if getattr(message, "type", "") != "ai":
                        continue
                    input_tokens, output_tokens, total = _usage_from(message)
                    result.input_tokens += input_tokens
                    result.output_tokens += output_tokens
                    result.total_tokens += total
                    if not result.model:
                        result.model = _model_from(message)
                    for call in getattr(message, "tool_calls", []) or []:
                        name = call.get("name", "")
                        result.tool_calls[name] = result.tool_calls.get(name, 0) + 1
                        if name in BUILD_TOOL_NAMES:
                            build_calls += 1
            if stream_callback:
                stream_callback(update)
    except Exception as exc:  # noqa: BLE001 — one bad run must not kill the sweep
        result.error = f"{type(exc).__name__}: {exc}"[:500]

    result.duration_s = round(time.monotonic() - started, 2)
    # The first build is generation, not repair; every later one is a fix round.
    result.fix_phase = max(0, build_calls - 1)
    result.wrote_file = output_path.is_file() and output_path.stat().st_size > 0
    return result
