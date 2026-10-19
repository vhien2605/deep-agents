---
name: make-build
description: How to build the C++ project and run its test suite with make, and how to interpret common compiler/linker failures when tests do not build.
---

# Building and running tests with make

## Discovering the build interface

1. Read the `Makefile` first (`read` tool). Identify targets: typical names are
   `all`, `test`, `check`, `coverage`, `clean`; note `CXX`, `CXXFLAGS`, include
   paths (`-I...`), and how test sources are globbed (e.g. `tests/*.cpp`).
2. Build: `make_target("")` (default target) or `make_target("test")`.
3. Run tests either via the `test` target or directly with
   `run_test_binary("path/to/test_runner")`.

## Interpreting failures (exit_code != 0)

| Symptom in output | Likely cause | Fix in the TEST file |
|---|---|---|
| `fatal error: X.h: No such file` | wrong include path | match `-I` flags in Makefile (`#include "calculator.h"` vs `"src/calculator.h"`) |
| `no matching function for call` | guessed signature wrong | re-read real signature with `get_function_source` |
| `undefined reference to X` | symbol not linked | ensure the Makefile compiles the source file defining X; don't redeclare — include the header |
| `multiple definition of main` | more than one test file defines `main()` | keep exactly one `main()` (or use the project's shared runner) |
| assertion output / non-zero test exit | test expectation wrong OR real bug | see test-repair skill |

## Rules

- Always `make clean` (if the target exists) before a coverage build so stale
  `.gcda`/`.gcno` files don't skew results.
- Never edit the Makefile or production sources to make a test compile; adapt
  the test. Only touch the Makefile if the user explicitly asks you to add a
  missing `test`/`coverage` target, and say so in your report.
