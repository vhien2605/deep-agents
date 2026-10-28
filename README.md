# deep-agents-unittest

*English · [Tiếng Việt](README.vi.md)*

A deep agent that generates unit tests for **C++, Python, and Java** projects, built on [`deepagents`](https://docs.langchain.com/labs/deep-agents/overview) (LangChain). The agent detects the project language on its own (Makefile → C++/make/lcov; pyproject.toml/uv.lock → Python/pytest/coverage.py; pom.xml → Java/Maven/JUnit 5/JaCoCo) and loads the matching skill set.

It runs a loop: **analyze the source (tree-sitter) → generate tests → build & self-repair (make) → measure coverage (gcov/lcov) → repeat** until the coverage threshold is met.

## Architecture

```
main.py (CLI)
└── create_cpp_unittest_agent(project_root)          # src/agent.py
    ├── Model: from .env — LLM_PROVIDER openai (any OpenAI-compatible endpoint) · openrouter · anthropic
    ├── Backend: LocalShellBackend(root=project)      # read/write/edit/glob/grep/execute on the real project
    ├── Skills (progressive disclosure, skills/*/SKILL.md)
    │   ├── C++:     cpp-analysis · make-build · coverage-lcov
    │   ├── Python:  python-analysis · pytest-testing · coverage-py
    │   ├── Java:    java-analysis · maven-junit · coverage-jacoco
    │   ├── Shared:  test-repair — self-repair loop, tells a wrong test from a real bug
    │   └── Shared:  symbolic-execution — solves path constraints with Z3
    ├── Tools (src/tools/)
    │   ├── analysis.py  — analyze_source_file, get_function_source, list_source_files
    │   │                  (tree-sitter cpp + python + java, multi-language)
    │   ├── build.py     — make_target (C++), run_command (pytest/coverage/mvn...), run_test_binary
    │   ├── coverage.py  — coverage_report (lcov) + coverage_py_report (coverage.py)
    │   │                  + jacoco_report (JaCoCo XML) — all on one JSON schema
    │   └── symbolic.py  — solve_branch_inputs, extract_path_conditions,
    │                      solve_path_constraints (engine in src/symbolic.py, Z3)
    └── Subagents (context isolation, one job each)
        ├── code-analyzer    — map the codebase, score by branch_count, read-only
        ├── path-solver      — solve path conditions → concrete inputs per branch
        ├── test-generator   — write test files following the project's conventions
        ├── test-fixer       — build → read errors → fix tests → repeat until green
        └── coverage-auditor — measure lcov, list uncovered functions/lines
```

The orchestrator plans with a todo list (LangChain's `TodoListMiddleware`, registered explicitly in `src/agent.py` — as of deepagents 0.7 it is no longer included by default), delegates to each subagent, and repeats steps 3–6 targeting the code that is still uncovered.

### Symbolic execution in the loop

For every function with `branch_count > 0`, the SOLVE step runs **before** test generation:
`solve_branch_inputs` parses the AST, builds each branch's path condition, translates it
to Z3 syntax, and solves it. Results come in three shapes — `sat` (concrete inputs that
enter the branch), `unsat` (the branch is **unreachable** → dead code, no test written),
and `skipped` (something could not be translated, with the reason). This spares the agent
from guessing inputs and burning a generate → build → measure cycle.

The translator handles: `&&`/`||`/`!`, `and`/`or`/`not`, Python chained comparisons
(`0 < b < 2`), C/Java integer promotion (`BitVec32` meeting `BitVec64` becomes
`SignExt`/`ZeroExt`), unsigned comparison (`ULT`/`UGT`), literal suffixes (`3L`, `10u`),
and `len(s)` / `s.length()` / `strlen(s)` → `Length`. Anything it cannot translate (calls
to other functions, field access, locals that are not parameters) is **reported** rather
than guessed.

## Requirements

- `g++`/`clang++`, `make`, `lcov` (macOS: `brew install lcov`)
- A `.env` file at the repo root (copy from `.env.example`). `LLM_PROVIDER` picks
  `openai` (any OpenAI-compatible endpoint), `openrouter`, or `anthropic`:
  ```
  LLM_PROVIDER=openai
  LLM_BASE_URL=http://host01:9207/v1
  LLM_API_KEY=sk-...
  LLM_MODEL=zai-org/GLM-5.2
  ```
- `uv sync` to install the Python dependencies

## Running

```bash
# Demo: the bundled calculator project (which has no tests yet)
uv run main.py --project examples/demo_calculator

# A real project, 90% coverage threshold, with extra instructions
uv run main.py --project /path/to/cpp/project --coverage 90 \
    --task "Only generate tests for src/parser.cpp, use doctest"
```

The target project needs a `Makefile` with a `test` target (build + run tests). A `coverage` target (build with `--coverage`, run tests, emit `coverage.info`) is optional — without it, the agent builds an instrumented binary itself and calls `lcov --capture` (see `skills/coverage-lcov/SKILL.md`).

### Step budget (`recursion_limit`)

LangGraph counts every node execution as one super-step, and one agent turn costs three of them (`model` → `TodoListMiddleware.after_model` → `tools`), so a limit of N allows only about **N/3 tool calls**. LangGraph's own default of 200 is ~66 tool calls, which the analyze → solve → generate → fix → measure loop exhausts on any non-trivial project, and the run dies with `GraphRecursionError`.

The default here is therefore **600** (`config.DEFAULT_RECURSION_LIMIT`), overridable per run with `--recursion-limit`, globally with `AGENT_RECURSION_LIMIT` in `.env`. Every subagent inherits the same budget for its own run — and because a `GraphRecursionError` raised inside a subagent propagates out of the `task` tool, one runaway subagent ends the whole session.

Raising the budget is not a substitute for terminating: the orchestrator, test-fixer, and coverage-auditor prompts carry explicit stop conditions (missing toolchain, unmeasurable coverage, two iterations with no coverage gain, iteration caps), and a missing `make`/`g++`/`lcov`/`mvn` is reported as `TOOLCHAIN MISSING` — a terminal error the agent is told never to retry.

## Benchmark (`bench/`)

`bench/` measures the agent the way the CITYWALK paper measures C++ unit-test generators: one
agent run per focal method, then compile / execute / cover the result and report per-method
CSR, EPR, line/branch coverage, and mutation score. It runs inside Docker so the toolchain
(g++, gcovr, GoogleTest) is pinned:

```bash
docker build -t deepagents-bench -f bench/Dockerfile .
bench/run-docker.sh run --project data/re2-<sha> --out runs/re2 --sample 30
```

See [`bench/README.md`](bench/README.md) for the `discover` / `gen` / `eval` / `report` commands
and the output columns.

## Tests

The test suite runs offline — no LLM, no API key:

```bash
uv run --with pytest pytest -q
```

## Safety rules baked into the prompts

- Never modify production source to make a test pass — fix the test instead.
- When a test exposes a real bug, keep the correct assertion, mark it `// BUG:`, and report it.
- No "fixing" by deleting assertions or loosening expectations until things pass.

Note: `LocalShellBackend` lets the agent run a real shell on your machine — use it only for local projects you trust, or wrap it in a sandbox/container when running unfamiliar code.
