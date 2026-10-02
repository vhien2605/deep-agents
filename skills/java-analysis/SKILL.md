---
name: java-analysis
description: How to analyze Java source code with the tree-sitter tools to find testable units and prioritize test generation. Use before generating any Java tests.
---

# Java source analysis with tree-sitter

## Tools

- `list_source_files(subdir, language="java")` — enumerate .java files,
  excluding `target/` and build dirs.
- `analyze_source_file(file_path)` — JSON structure of one file: imports,
  classes/interfaces/enums/records, methods and constructors (`Class.method`)
  with `signature`, `start_line`/`end_line`, `branch_count`.
- `get_function_source(file_path, function_name)` — exact body of a method
  (matches `Calculator.divide` or just `divide`).

## Workflow

1. `list_source_files(".", language="java")` — production code lives in
   `src/main/java/`, tests in `src/test/java/` (Maven standard layout).
2. Read `pom.xml`: note the JUnit version (junit-jupiter = JUnit 5 vs junit =
   JUnit 4 — dictates test style), Java version, and existing plugins
   (surefire, jacoco).
3. `analyze_source_file` on every production class. Prioritize:
   - Methods with **high `branch_count`**.
   - Public methods not referenced by any existing test class.
   - Pure logic first; classes needing heavy dependency wiring later
     (constructor injection makes them testable — instantiate with fakes).
4. Always fetch the real body with `get_function_source` before writing a test —
   check parameter types, checked exceptions (`throws`), overloads (same name,
   different signatures — the tool returns all matches), and visibility.

## What to skip

- `main(String[] args)`, generated code (lombok-heavy classes, protobuf).
- Trivial getters/setters/`toString`/`equals`/`hashCode` boilerplate
  (branch_count 0) — unless hand-written with logic.
- Private methods reachable through a public method — test via the public API;
  never use reflection to reach them.
- Interfaces and abstract methods (no body) — test the concrete implementations.

## Gotchas

- Overloads: `get_function_source` returns every overload matching the name;
  pick by signature.
- Nested/inner classes are reported with the inner class name
  (`Outer.Inner.method` shows as `Inner.method`) — open the file to confirm
  the enclosing type when writing imports.
- Records get an implicit constructor and accessors that don't appear as
  `method_declaration` — only their explicit methods show up.
