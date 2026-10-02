---
name: cpp-analysis
description: How to analyze C++ source code with the tree-sitter tools to find testable units and prioritize test generation. Use before generating any tests.
---

# C++ source analysis with tree-sitter

## Tools

- `list_source_files(subdir, language="cpp")` — enumerate .cpp/.h files, excluding build dirs.
- `analyze_source_file(file_path)` — JSON structure of one file: includes, classes,
  functions with `signature`, `start_line`/`end_line`, `branch_count`.
- `get_function_source(file_path, function_name)` — exact body of a function.

## Workflow

1. `list_source_files(".")` to find production sources vs test files
   (test files typically live under `tests/` or match `*_test.cpp`, `test_*.cpp`).
2. `analyze_source_file` on every production `.cpp` (and header-only `.hpp` with
   inline definitions). Headers with declarations only tell you the public API;
   the `.cpp` tells you branch complexity.
3. Build a priority list of functions to test:
   - Functions with **high `branch_count`** first — each branch is a coverage
     line that plain happy-path tests will miss.
   - Functions never referenced by existing test files.
   - Skip: `main()`, trivial getters/setters (branch_count 0, 1–2 lines),
     private helpers only reachable through public API (test via the public API).
4. Always fetch the real body with `get_function_source` before writing a test —
   never guess parameter types, const-ness, or exception behavior.

## Gotchas

- `has_parse_errors: true` in the JSON means tree-sitter hit invalid or
  macro-heavy code; the function list may be incomplete. Fall back to reading
  the file directly.
- Template functions appear as `function_definition` inside
  `template_declaration`; test them by instantiating with concrete types.
- Qualified names like `Calculator::add` come from out-of-class definitions;
  match either the full name or the last segment.
