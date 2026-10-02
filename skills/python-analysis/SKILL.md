---
name: python-analysis
description: How to analyze Python source code with the tree-sitter tools to find testable units and prioritize test generation. Use before generating any Python tests.
---

# Python source analysis with tree-sitter

## Tools

- `list_source_files(subdir, language="python")` — enumerate .py files,
  excluding venv/build dirs.
- `analyze_source_file(file_path)` — JSON structure of one file: imports,
  classes, functions/methods (`Class.method`) with `signature`,
  `start_line`/`end_line`, `branch_count`.
- `get_function_source(file_path, function_name)` — exact body of a function
  (matches `MyClass.method` or just `method`).

## Workflow

1. `list_source_files(".", language="python")` — separate production modules
   from test files (`tests/`, `test_*.py`, `*_test.py`, `conftest.py`).
2. Read `pyproject.toml` / `setup.py` to learn the package name and layout
   (src-layout `src/pkg/` vs flat `pkg/`) — this decides the import lines in
   the generated tests.
3. `analyze_source_file` on every production module. Prioritize:
   - Functions/methods with **high `branch_count`**.
   - Public API (no leading underscore) not referenced by any existing test.
   - Pure logic first; I/O-heavy code later (needs mocking — see
     pytest-testing skill).
4. Always fetch the real body with `get_function_source` before writing a test —
   check for default arguments, keyword-only args, raised exceptions, and
   return types.

## What to skip

- `if __name__ == "__main__":` blocks and CLI `main()` wiring.
- Trivial `@property` getters, `__repr__`/`__str__` boilerplate.
- Private helpers (`_foo`) reachable through a public function — test via the
  public API instead.
- Generated code and migrations.

## Gotchas

- Decorated functions still appear as `function_definition`; note decorators
  like `@staticmethod`/`@classmethod` (changes how you call them) and
  `@lru_cache` (call `.cache_clear()` between tests if state matters).
- Async functions (`async def`) need `pytest.mark.asyncio` or an anyio runner —
  check whether the project already depends on such a plugin before using it.
- `has_parse_errors: true` means the file may use very new syntax; fall back to
  reading the file directly.
