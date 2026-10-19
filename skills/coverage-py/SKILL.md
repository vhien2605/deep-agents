---
name: coverage-py
description: How to measure Python line coverage with coverage.py (or pytest-cov), produce a JSON report, and read it to decide which tests to write next.
---

# Coverage measurement with coverage.py

## Standard flow

1. Run the suite under coverage (pick the runner per the pytest-testing skill):
   - `run_command("python -m coverage run -m pytest -q")`
   - uv projects: `run_command("uv run coverage run -m pytest -q")`
   - if the project uses pytest-cov instead:
     `run_command("python -m pytest -q --cov=<pkg> --cov-report=json")`
2. Export JSON: `run_command("python -m coverage json")` → writes
   `coverage.json` (pytest-cov already wrote it in step 1).
3. Parse: `coverage_py_report(json_file="coverage.json", source_filter="src/")`
   — returns total %, per-file %, `uncovered_lines`, `uncovered_functions`.

Scope the measurement to production code: pass `source_filter` (e.g. the
package dir) so `tests/` and site-packages don't skew the number. If the
project has a `[tool.coverage.run] source = [...]` section, coverage already
scopes itself — still filter out test files in the report.

## Reading the report

- `uncovered_functions` — never-executed functions: highest-value targets,
  write at least one test each.
- Clusters of `uncovered_lines` inside a covered function = untaken branches:
  `else` arms, `except` handlers, early returns, default-argument paths.
  Use `analyze_source_file` line ranges to map lines back to functions, then
  `get_function_source` to see which branch needs forcing.
- Diminishing returns: `if TYPE_CHECKING:`, `raise NotImplementedError`
  stubs, `__main__` blocks — exclude via `# pragma: no cover` suggestions in
  the report rather than chasing 100%.

## Gotchas

- Coverage data accumulates in `.coverage`; run
  `run_command("python -m coverage erase")` between measurement rounds.
- Subprocesses spawned by tests aren't measured without extra config
  (`concurrency`/`parallel` settings) — note it instead of fighting it.
- If `coverage json` reports "No data to report", the tests ran without the
  coverage wrapper (e.g. bare pytest) — re-run step 1.
- `import`-time code only counts when imported during the run; a module with
  0% coverage usually just was never imported by any test.
