---
name: coverage-lcov
description: How to measure C++ line coverage with gcov/lcov, produce a tracefile, and read the coverage report to decide which tests to write next.
---

# Coverage measurement with gcov/lcov

## Preferred path: project coverage target

1. `make_target("clean")` — remove stale objects and old `.gcda`/`.gcno` files.
2. `run_coverage_build()` — runs `make coverage`. A good target compiles with
   `--coverage -O0 -g`, runs the tests, and produces `coverage.info`.
3. `coverage_report(tracefile="coverage.info", source_filter="src/")`.

## Fallback: no coverage target in the Makefile

1. `make_target("clean")`
2. Rebuild instrumented: `make_target("test", extra_args='CXXFLAGS+=--coverage LDFLAGS+=--coverage')`
   (or whatever variable the Makefile uses — read it). Compiling produces
   `.gcno`; running the tests produces `.gcda`.
3. Run the tests if the build didn't already.
4. `capture_lcov_tracefile()` — wraps `lcov --capture --directory .`.
5. `coverage_report(source_filter=...)`.

If `lcov` is not installed, run `gcov <source>.cpp` via the shell instead and
read the `.gcov` annotated files (lines prefixed `#####` are uncovered).

## Reading the report

- `total_line_coverage_pct` — compare against the goal. Always pass a
  `source_filter` so test files and system headers don't inflate/deflate it.
- `uncovered_functions` — functions never called: the highest-value targets;
  write at least one test per entry.
- `uncovered_lines` — map them to functions via `analyze_source_file` line ranges.
  Clusters of uncovered lines inside an otherwise-covered function are usually
  untaken branches: error paths, `else` arms, `switch` cases, exception paths.
  Write a test that forces that branch (invalid input, boundary value, etc.).

## Gotchas

- Zero coverage everywhere usually means the tests ran a NON-instrumented
  binary, or `.gcda` files were written elsewhere — clean and rebuild everything
  with `--coverage` in both compile and link steps.
- Coverage accumulates across runs (`.gcda` merges); clean between measurement
  rounds for accurate per-round numbers.
