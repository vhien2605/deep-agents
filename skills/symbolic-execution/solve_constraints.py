#!/usr/bin/env python3
"""Solve a symbolic-execution path constraint with Z3 and print concrete values.

Input is a JSON spec (file path argument, or stdin when the path is `-`):

    {
      "vars": {"x": "Int", "y": "Real", "flag": "Bool", "s": "String",
               "w": "BitVec32", "buf": "Array(Int,Int)"},
      "defs": {"total": "x + y"},                  // optional named sub-expressions
      "constraints": ["x > 0", "x + y == 10",
                      {"name": "branch_else", "expr": "Not(flag)"}],
      "num_solutions": 1,                          // >1 enumerates distinct models
      "optimize": {"goal": "minimize", "objective": "x"},
      "timeout_ms": 10000
    }

Alternatively pass raw SMT-LIB2 with `--smt2 file.smt2`.

Output is JSON on stdout:

    {"status": "sat", "model": {...}, "solutions": [{...}], "unsat_core": []}

Exit code is 0 for `sat`, 1 for `unsat`, 2 for `unknown`, 3 for a bad spec.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

try:
    import z3
except ImportError:  # pragma: no cover - environment problem, not logic
    sys.stderr.write(
        "z3 is not installed. Install it with one of:\n"
        "  uv add z3-solver\n"
        "  pip install z3-solver\n"
        "or re-run this script as: uv run --with z3-solver python <this script>\n"
    )
    raise SystemExit(3)


EXIT_SAT, EXIT_UNSAT, EXIT_UNKNOWN, EXIT_BAD_SPEC = 0, 1, 2, 3


class SpecError(Exception):
    """The input spec is malformed (bad type, unparsable expression, ...)."""


# --------------------------------------------------------------------------- #
# Sort / variable construction
# --------------------------------------------------------------------------- #

_BITVEC_RE = re.compile(r"^BitVec\s*[\(<]?\s*(\d+)\s*[\)>]?$", re.IGNORECASE)
_ARRAY_RE = re.compile(r"^Array\s*\(\s*(.+?)\s*,\s*(.+?)\s*\)$", re.IGNORECASE)
_SEQ_RE = re.compile(r"^(?:Seq|List|Array)\s*\(\s*(.+?)\s*\)$", re.IGNORECASE)


def make_sort(spec: str) -> z3.SortRef:
    """Turn a textual sort (`"Int"`, `"BitVec32"`, `"Array(Int,Real)"`) into a z3 sort."""
    text = spec.strip()
    lowered = text.lower()

    if lowered in ("int", "integer"):
        return z3.IntSort()
    if lowered in ("real", "float", "double"):
        return z3.RealSort()
    if lowered in ("bool", "boolean"):
        return z3.BoolSort()
    if lowered in ("string", "str"):
        return z3.StringSort()

    bv = _BITVEC_RE.match(text)
    if bv:
        return z3.BitVecSort(int(bv.group(1)))

    arr = _ARRAY_RE.match(text)
    if arr:
        return z3.ArraySort(make_sort(arr.group(1)), make_sort(arr.group(2)))

    seq = _SEQ_RE.match(text)
    if seq:
        return z3.SeqSort(make_sort(seq.group(1)))

    raise SpecError(
        f"unknown sort {spec!r}; use Int, Real, Bool, String, BitVec<N>, "
        f"Array(K,V) or Seq(T)"
    )


def declare_vars(var_spec: dict[str, str]) -> dict[str, z3.ExprRef]:
    """Declare one z3 constant per entry of `vars`."""
    if not isinstance(var_spec, dict):
        raise SpecError("'vars' must be an object mapping name -> sort")
    out: dict[str, z3.ExprRef] = {}
    for name, sort_text in var_spec.items():
        if not isinstance(sort_text, str):
            raise SpecError(f"sort of {name!r} must be a string, got {sort_text!r}")
        out[name] = z3.Const(name, make_sort(sort_text))
    return out


# --------------------------------------------------------------------------- #
# Expression parsing
# --------------------------------------------------------------------------- #

_SAFE_BUILTINS = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "enumerate": enumerate,
    "float": float,
    "int": int,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "pow": pow,
    "range": range,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
    "True": True,
    "False": False,
    "None": None,
}


def build_namespace(variables: dict[str, z3.ExprRef]) -> dict[str, Any]:
    """Evaluation namespace: every public z3 symbol plus the declared variables."""
    ns: dict[str, Any] = {n: getattr(z3, n) for n in dir(z3) if not n.startswith("_")}
    ns.update(_SAFE_BUILTINS)
    ns.update(variables)  # declared vars win over z3 names
    return ns


def parse_expr(text: str, namespace: dict[str, Any]) -> Any:
    """Evaluate one constraint written as a Python/z3 expression."""
    try:
        return eval(text, {"__builtins__": {}}, namespace)  # noqa: S307 - trusted local spec
    except SpecError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller as a spec error
        raise SpecError(f"cannot parse constraint {text!r}: {type(exc).__name__}: {exc}") from exc


def as_bool_expr(value: Any, source: str) -> z3.BoolRef:
    """Coerce a parsed constraint into a boolean z3 expression."""
    if isinstance(value, bool):
        return z3.BoolVal(value)
    if z3.is_expr(value) and z3.is_bool(value):
        return value
    raise SpecError(f"constraint {source!r} is not a boolean expression (got {value!r})")


def normalize_constraints(raw: Any) -> list[tuple[str, str]]:
    """Normalize `constraints` into `[(label, expression_text), ...]`."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise SpecError("'constraints' must be a string or a list")

    out: list[tuple[str, str]] = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            out.append((f"c{i}", item))
        elif isinstance(item, dict):
            expr = item.get("expr", item.get("constraint"))
            if not isinstance(expr, str):
                raise SpecError(f"constraint #{i} has no string 'expr' field")
            out.append((str(item.get("name", f"c{i}")), expr))
        else:
            raise SpecError(f"constraint #{i} must be a string or an object, got {item!r}")
    return out


# --------------------------------------------------------------------------- #
# Model extraction
# --------------------------------------------------------------------------- #


def value_to_python(value: Any) -> Any:
    """Convert a z3 model value into a JSON-serializable Python value."""
    if value is None:
        return None
    if z3.is_int_value(value):
        return value.as_long()
    if z3.is_rational_value(value):
        num, den = value.numerator_as_long(), value.denominator_as_long()
        exact = f"{num}/{den}" if den != 1 else str(num)
        return {"value": num / den, "exact": exact}
    if z3.is_algebraic_value(value):
        approx = value.approx(10)
        return {
            "value": approx.numerator_as_long() / approx.denominator_as_long(),
            "exact": str(value),
            "irrational": True,
        }
    if z3.is_true(value):
        return True
    if z3.is_false(value):
        return False
    if z3.is_string_value(value):
        return value.as_string()
    if z3.is_bv_value(value):
        width = value.size()
        unsigned = value.as_long()
        signed = unsigned - (1 << width) if unsigned >= (1 << (width - 1)) else unsigned
        return {"unsigned": unsigned, "signed": signed, "hex": f"0x{unsigned:0{(width + 3) // 4}x}", "bits": width}
    if z3.is_expr(value) and isinstance(value.sort(), z3.ArraySortRef):
        return array_to_python(value)
    try:  # sequence values expose as_list()
        return [value_to_python(entry) for entry in value.as_list()]
    except Exception:  # noqa: BLE001 - fall through to the textual form
        return str(value)


def array_to_python(value: z3.ExprRef) -> Any:
    """Flatten an array model value (a `Store(...)` chain over `K(...)`) into a dict.

    Returns `{"entries": {index: value, ...}, "default": v}`. Indices not listed
    take the default, so a caller building a test fixture can fill a concrete
    buffer from `entries` and pad the rest with `default`.
    """
    entries: dict[str, Any] = {}
    node = value
    while z3.is_app_of(node, z3.Z3_OP_STORE):
        index, stored = node.arg(1), node.arg(2)
        key = str(value_to_python(index))
        entries.setdefault(key, value_to_python(stored))  # outermost store wins
        node = node.arg(0)
    if z3.is_app_of(node, z3.Z3_OP_CONST_ARRAY):
        return {"entries": entries, "default": value_to_python(node.arg(0))}
    return {"entries": entries, "default": None, "rest": str(node)}


def extract_model(model: z3.ModelRef, variables: dict[str, z3.ExprRef]) -> dict[str, Any]:
    """Read every declared variable out of a model, completing don't-care values."""
    out: dict[str, Any] = {}
    for name, var in variables.items():
        out[name] = value_to_python(model.eval(var, model_completion=True))
    return out


def block_model(model: z3.ModelRef, variables: dict[str, z3.ExprRef]) -> z3.BoolRef | None:
    """Build a clause forbidding this exact assignment, so the next solve differs."""
    literals = []
    for var in variables.values():
        if isinstance(var.sort(), z3.ArraySortRef):
            continue  # arrays are infinite objects; blocking them is unsound here
        literals.append(var != model.eval(var, model_completion=True))
    return z3.Or(*literals) if literals else None


# --------------------------------------------------------------------------- #
# Solving
# --------------------------------------------------------------------------- #


def solve_spec(spec: dict[str, Any], *, show_smt2: bool = False) -> tuple[dict[str, Any], int]:
    """Solve a parsed spec. Returns `(result_dict, exit_code)`."""
    variables = declare_vars(spec.get("vars", {}))
    namespace = build_namespace(variables)

    for name, expr_text in (spec.get("defs") or {}).items():
        namespace[name] = parse_expr(expr_text, namespace)

    constraints = normalize_constraints(spec.get("constraints"))
    timeout_ms = int(spec.get("timeout_ms", 10000))
    num_solutions = max(1, int(spec.get("num_solutions", 1)))
    optimize = spec.get("optimize")

    result: dict[str, Any] = {
        "status": "unknown",
        "vars": {n: str(v.sort()) for n, v in variables.items()},
        "constraints": [{"name": n, "expr": e} for n, e in constraints],
        "model": None,
        "solutions": [],
        "unsat_core": [],
    }

    if optimize:
        solver = z3.Optimize()
        solver.set("timeout", timeout_ms)
        for _name, expr_text in constraints:
            solver.add(as_bool_expr(parse_expr(expr_text, namespace), expr_text))
        objective_text = optimize.get("objective")
        if not isinstance(objective_text, str):
            raise SpecError("'optimize' needs an 'objective' expression string")
        objective = parse_expr(objective_text, namespace)
        goal = str(optimize.get("goal", "minimize")).lower()
        if goal.startswith("min"):
            handle = solver.minimize(objective)
        elif goal.startswith("max"):
            handle = solver.maximize(objective)
        else:
            raise SpecError(f"'optimize.goal' must be minimize or maximize, got {goal!r}")
        result["optimize"] = {"goal": goal, "objective": objective_text}
        if show_smt2:
            result["smt2"] = solver.sexpr()
        check = solver.check()
        result["status"] = str(check)
        if check == z3.sat:
            model = solver.model()
            result["model"] = extract_model(model, variables)
            result["solutions"] = [result["model"]]
            result["optimize"]["value"] = value_to_python(solver.lower(handle) if goal.startswith("min") else solver.upper(handle))
            return result, EXIT_SAT
        return result, EXIT_UNSAT if check == z3.unsat else EXIT_UNKNOWN

    solver = z3.Solver()
    solver.set("timeout", timeout_ms)
    solver.set(unsat_core=True)
    seen_labels: dict[str, int] = {}
    for label, expr_text in constraints:
        unique = label if label not in seen_labels else f"{label}#{seen_labels[label]}"
        seen_labels[label] = seen_labels.get(label, 0) + 1
        solver.assert_and_track(as_bool_expr(parse_expr(expr_text, namespace), expr_text), unique)
    if show_smt2:
        result["smt2"] = solver.sexpr()

    check = solver.check()
    result["status"] = str(check)

    if check == z3.unsat:
        result["unsat_core"] = [str(c) for c in solver.unsat_core()]
        return result, EXIT_UNSAT
    if check == z3.unknown:
        result["reason"] = solver.reason_unknown()
        return result, EXIT_UNKNOWN

    solutions: list[dict[str, Any]] = []
    while len(solutions) < num_solutions:
        model = solver.model()
        solutions.append(extract_model(model, variables))
        if len(solutions) == num_solutions:
            break
        clause = block_model(model, variables)
        if clause is None:
            break
        solver.add(clause)
        if solver.check() != z3.sat:
            break

    result["model"] = solutions[0]
    result["solutions"] = solutions
    return result, EXIT_SAT


def solve_smt2(path: str, *, timeout_ms: int = 10000) -> tuple[dict[str, Any], int]:
    """Solve a raw SMT-LIB2 file."""
    text = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    solver = z3.Solver()
    solver.set("timeout", timeout_ms)
    solver.add(z3.parse_smt2_string(text))
    check = solver.check()
    result: dict[str, Any] = {"status": str(check), "model": None, "solutions": [], "unsat_core": []}
    if check == z3.sat:
        model = solver.model()
        assignment = {str(d.name()): value_to_python(model[d]) for d in model.decls()}
        result["model"] = assignment
        result["solutions"] = [assignment]
        return result, EXIT_SAT
    if check == z3.unsat:
        return result, EXIT_UNSAT
    result["reason"] = solver.reason_unknown()
    return result, EXIT_UNKNOWN


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Solve path constraints with Z3.")
    parser.add_argument("spec", nargs="?", default="-", help="JSON spec file, or - for stdin")
    parser.add_argument("--smt2", metavar="FILE", help="solve a raw SMT-LIB2 file instead")
    parser.add_argument("-n", "--num-solutions", type=int, help="override num_solutions")
    parser.add_argument("--timeout-ms", type=int, help="override timeout_ms")
    parser.add_argument("--show-smt2", action="store_true", help="include the SMT-LIB2 encoding in the output")
    args = parser.parse_args(argv)

    try:
        if args.smt2:
            result, code = solve_smt2(args.smt2, timeout_ms=args.timeout_ms or 10000)
        else:
            raw = sys.stdin.read() if args.spec == "-" else open(args.spec, encoding="utf-8").read()
            spec = json.loads(raw)
            if not isinstance(spec, dict):
                raise SpecError("spec must be a JSON object")
            if args.num_solutions:
                spec["num_solutions"] = args.num_solutions
            if args.timeout_ms:
                spec["timeout_ms"] = args.timeout_ms
            result, code = solve_spec(spec, show_smt2=args.show_smt2)
    except json.JSONDecodeError as exc:
        print(json.dumps({"status": "error", "error": f"invalid JSON: {exc}"}, indent=2))
        return EXIT_BAD_SPEC
    except (SpecError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, indent=2))
        return EXIT_BAD_SPEC

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
