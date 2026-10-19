---
name: pytest-testing
description: How to write and run pytest tests for a Python project - environment detection, test conventions, running the suite, and interpreting common failures.
---

# Writing and running Python tests with pytest

## Detecting how to run the project

Read the project files first, then pick ONE runner and stick to it:

| Signal | Runner prefix |
|---|---|
| `uv.lock` / `[tool.uv]` | `uv run pytest ...` (also `uv run coverage ...`) |
| `poetry.lock` | `poetry run pytest ...` |
| plain `requirements.txt` / nothing | `python -m pytest ...` |

Run via `run_command`, e.g. `run_command("python -m pytest -x -q")`.
If pytest is missing (exit code says "No module named pytest"), report it —
do not install packages without being asked; plain `assert`-based test scripts
run with `python tests/test_x.py` are the fallback.

## Test conventions

- Files: `tests/test_<module>.py` (mirror the package structure for big repos).
- Follow existing tests' import style. src-layout usually needs the package
  installed (`pip install -e .` already done) or `pythonpath = ["src"]` in
  pyproject's `[tool.pytest.ini_options]` — check before inventing sys.path hacks.
- Style: plain `assert`, `pytest.raises(ValueError, match="...")` for error
  paths, `@pytest.mark.parametrize` for input tables, fixtures for shared
  setup, `tmp_path` for filesystem, `monkeypatch` for env/attrs.
- Float comparisons: `pytest.approx`. Random: seed it. Time: freeze or inject.
- Mock at the boundary only (`monkeypatch` / `unittest.mock.patch` on network,
  filesystem, subprocess). Never mock the unit under test itself.

## Running and reading failures

- Iterate fast: `run_command("python -m pytest -x -q tests/test_foo.py")`.
- `ModuleNotFoundError`/`ImportError` at collection → wrong import path or
  missing `__init__.py`; fix the test's import, not the package.
- `fixture 'x' not found` → typo or missing conftest; define the fixture in the
  test file unless one already exists in `conftest.py`.
- Assertion failure → triage per the test-repair skill (wrong expectation vs
  real bug; keep the correct-behavior assertion and mark `# BUG:` when the
  source is at fault).
- Whole-suite run before finishing: `run_command("python -m pytest -q")` —
  your new tests must not break existing ones.
