---
name: test-repair
description: How to automatically repair generated C++ tests that fail to compile or fail at runtime, and how to tell a wrong test expectation from a real source bug.
---

# Repairing failing generated tests

## Loop

Repeat until `make test` exits 0 (cap at ~6 iterations, then report what's left):

1. Build & run: `make_target("test")`.
2. Classify the first failure in the output (fix one class of error per
   iteration — compile errors first, they mask everything else).
3. Apply the smallest fix to the TEST file, rebuild.

## Classifying runtime assertion failures

When a test builds but an assertion fails, decide who is wrong:

1. Re-read the function body (`get_function_source`) and hand-trace it with the
   failing input.
2. If the traced result matches the FUNCTION's actual output → the test's
   expected value was wrong (typical for generated tests). Fix the expectation.
3. If the traced result matches the TEST's expectation but not the function's
   output → likely a real bug. Keep the test asserting the CORRECT behavior,
   add `// BUG: <one-line explanation>` above it, and list it in your report.
   The suite may stay red on that one test — that is acceptable and must be
   reported, never papered over.

## Forbidden "fixes"

- Deleting or commenting out a failing assertion without justification.
- Widening an expectation to always pass (e.g. `EXPECT_TRUE(true)`,
  asserting a range that spans every possible value).
- Editing production source files, ever.
- Catching and swallowing an exception the test was supposed to assert.

## Crash triage

- Segfault in a test: usually the test passed a null/dangling pointer or
  violated a documented precondition — fix the test setup. If a documented
  valid input crashes the function, treat it as a real bug (rule 3 above).
- Timeout/hang: likely an infinite loop on an untested branch — real bug
  candidate; isolate the input and report it.
