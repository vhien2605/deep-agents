---
name: symbolic-execution
description: Solve path constraints with the Z3 SMT solver to obtain concrete input values. Use when you need test inputs that force a specific branch/path, when you must prove a branch is unreachable (dead code), or when a coverage report shows lines you cannot hit by guessing inputs.
compatibility: Requires the z3-solver Python package (uv add z3-solver).
---

# Symbolic execution — solving path constraints with Z3

Input: the constraints on a path through a function. Output: concrete values for
each input variable that make that path execute — i.e. a ready-made test case.

## Start with the automatic path

`solve_branch_inputs(file_path, function_name)` does steps 1–4 below for you:
it parses the function, derives every branch's path condition, translates it to
Z3, and solves it. Use it first. Everything after it in this document is for the
branches it reports as `skipped`/`error`, and for constraints you derive
yourself.

- `extract_path_conditions(file_path, function_name)` — same analysis without
  solving; returns a ready-to-edit spec per branch outcome.
- `solve_path_constraints(spec_json)` — solve a spec you wrote or repaired.

The same solver is available as a script for use outside the agent:

```
<skill_dir>/solve_constraints.py
```

## Workflow

1. **Get the real source.** `get_function_source(file_path, function_name)` —
   never derive constraints from a signature or from memory. Check the automatic
   translation against it before trusting the result.
2. **Pick the target path.** Usually a line/branch the coverage report says is
   uncovered (see the coverage-py / coverage-lcov / coverage-jacoco skills).
3. **Collect the path condition.** Walk from the function entry to the target
   line and conjoin, for every `if`/`while`/`&&`/`||`/ternary on the way, the
   branch condition as taken (negate it for the `else` arm). Substitute
   assignments made along the way so every constraint is expressed **only in
   terms of the function's inputs** (parameters, fields, globals) — that is the
   whole point: the solver can only give you values for things you can actually
   pass in.
4. **Write the JSON spec** (schema below) and run the solver.
5. **Read the result:**
   - `sat` → `model` holds one concrete input per variable. Turn it into a test.
   - `unsat` → the path is **infeasible**; the branch is dead code. Do not write
     a test for it — report it and move on. `unsat_core` names the minimal set
     of conflicting constraints, which tells you *why*.
   - `unknown` → the theory was too hard or the timeout hit (`reason` says
     which). Simplify: drop non-linear terms, shrink bit-widths, raise
     `timeout_ms`, or fall back to a hand-picked input.
6. **Write the test with the concrete values** and run it to confirm the target
   line is now covered. Solving is only a means to that end.

## Input spec

```json
{
  "vars": {
    "n":     "Int",
    "ratio": "Real",
    "flag":  "Bool",
    "name":  "String",
    "mask":  "BitVec32",
    "buf":   "Array(Int,Int)"
  },
  "defs":        { "total": "n * 2 + 1" },
  "constraints": [
    "n > 0",
    "n < 100",
    "total % 3 == 0",
    { "name": "else_branch", "expr": "Not(flag)" }
  ],
  "num_solutions": 1,
  "timeout_ms": 10000,
  "optimize": { "goal": "minimize", "objective": "n" }
}
```

- `vars` — one entry per **function input**. Sorts: `Int`, `Real`, `Bool`,
  `String`, `BitVec<N>` (e.g. `BitVec32`), `Array(K,V)`, `Seq(T)`.
- `defs` *(optional)* — named sub-expressions reusable in `constraints`; use
  them for intermediate variables assigned along the path.
- `constraints` — the path condition, one conjunct per entry. Each is a Python
  expression over the declared vars using z3 operators. Give the important ones
  a `{"name": ..., "expr": ...}` label so they show up readably in `unsat_core`.
- `num_solutions` *(default 1)* — ask for N **distinct** models when you want
  several test cases covering the same branch with different data.
- `optimize` *(optional)* — `minimize`/`maximize` an objective instead of taking
  any model; use it to get the smallest input that still hits the branch (nicer
  test data). Note: `unsat_core` is not available in optimize mode.

Run it:

```bash
python <skill_dir>/solve_constraints.py spec.json          # file
cat spec.json | python <skill_dir>/solve_constraints.py -   # stdin
python <skill_dir>/solve_constraints.py spec.json -n 5      # 5 distinct models
python <skill_dir>/solve_constraints.py --smt2 path.smt2    # raw SMT-LIB2
```

If z3 is not importable in the project's interpreter, run
`uv run --with z3-solver python <skill_dir>/solve_constraints.py ...`.

Exit codes: `0` sat, `1` unsat, `2` unknown, `3` malformed spec. Output is JSON
on stdout: `status`, `model`, `solutions`, `unsat_core`, and `reason` when
unknown.

## Writing constraints (z3 expression cheatsheet)

| Want | Write |
|---|---|
| and / or / not | `And(a, b)`, `Or(a, b)`, `Not(a)` |
| implication, iff | `Implies(a, b)`, `a == b` |
| if-then-else value | `If(cond, then_val, else_val)` |
| chained compare | `And(0 <= x, x < 10)` — **not** `0 <= x < 10` |
| absolute value | `If(x >= 0, x, -x)` |
| divisibility | `x % 3 == 0` |
| int ↔ real | `ToReal(n)`, `ToInt(r)` |
| string length / contains | `Length(s) == 3`, `Contains(s, StringVal("ab"))` |
| array read / write | `Select(a, i)`, `Store(a, i, v)` |
| bitwise | `x & y`, `x \| y`, `x ^ y`, `x << 2`, `LShR(x, 2)` (logical shift right) |
| unsigned BV compare | `ULT(x, y)`, `UGT`, `ULE`, `UGE` (plain `<` is **signed**) |
| all different | `Distinct(x, y, z)` |
| quantified | `ForAll([i], Implies(And(0 <= i, i < n), Select(a, i) > 0))` |

## Modelling the source language

- **Python** — `int` is unbounded → `Int`. `float` → `Real`. `str` → `String`.
  `list`/`dict` → `Array(Int,V)` plus a separate `Int` for the length, and
  constrain indices with `And(0 <= i, i < length)`.
- **C/C++/Java** — fixed-width integers are `BitVec32` / `BitVec64`, *not* `Int`,
  whenever overflow, wraparound, or bitwise ops matter for the branch. Signed
  comparison is the default in z3's BV ops; use `ULT`/`UGT`/... for `unsigned`.
  `float`/`double` → `Real` is an approximation: fine for ordinary arithmetic
  branches, wrong for NaN/infinity/rounding branches — say so rather than
  producing a bogus test.
- **Division** — z3 integer division truncates toward zero like C/Java but
  unlike Python's floor division; for negative operands model Python's `//`
  explicitly with `ToInt(ToReal(a) / ToReal(b))` adjustments, or just constrain
  the operands positive.
- **Pointers / references / objects** — do not model them. Use an `Int` or
  `Bool` proxy for the property the branch actually tests (`is_null`,
  `list_size`) and pass a matching real object in the test.

## What the automatic extraction handles

Translated faithfully: `if` / `else if` / `else` / `while` / `for` guards,
`&&` `||` `!` and Python `and`/`or`/`not`, chained Python comparisons
(`0 < b < 2`), C/Java integer promotion (a `BitVec32` meeting a `BitVec64` is
sign- or zero-extended), unsigned comparison (`ULT`/`UGT` when either operand is
unsigned), literal suffixes (`3L`, `10u`), and the string calls `len(s)`,
`s.length()`, `s.isEmpty()`, `s.equals(t)`, `strlen(s)`, `abs(x)`.

Reported as `skipped` rather than guessed: any other call, field access,
subscript, ternary, `switch`/`case`, and any symbol that is a local rather than a
parameter. That last one is the important guardrail — a spec is only solved when
every constraint is expressed purely over the function's inputs.

Solved inputs are searched in a small readable range first (|v| ≤ 1000,
strings ≤ 8 chars) and only widened when no solution exists there, so the values
are usable as test data. An `unsat` verdict always comes from the unrestricted
problem, so it is a real proof of infeasibility.

## Guardrails

- Only declare variables the test can actually control. A constraint over a
  local that is never derived from an input yields a model you cannot use.
- Convert a model to test inputs **verbatim**. Reals come back as
  `{"value": 9.5, "exact": "19/2"}` — use `exact` when the float is not
  representable. BitVecs come back as `{"unsigned": ..., "signed": ..., "hex":
  ...}` — pick the one matching the parameter's signedness.
- Arrays come back as `{"entries": {...}, "default": v}`: build the concrete
  container from `entries` and pad the remaining indices with `default`.
- z3 loves degenerate answers (`0`, `""`, empty array). If the model looks
  trivial, add the constraints the code implicitly assumes (non-empty input,
  positive size) and re-solve — a test with realistic data is worth more.
- Loops: unroll a fixed small number of iterations (2–3) and say how many you
  unrolled. Do not claim full path coverage of an unbounded loop.
- Always run the generated test. A `sat` model proves the *path condition* is
  satisfiable under **your** encoding; if the encoding drifted from the source,
  the test still misses the branch.
