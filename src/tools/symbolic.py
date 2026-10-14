"""Symbolic-execution tools: derive branch inputs by solving path constraints."""

import json
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from src import symbolic
from src.config import MAX_OUTPUT_CHARS


def _dump(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return (
        text[:MAX_OUTPUT_CHARS]
        + "\n...[truncated] — re-run on a single branch with solve_path_constraints"
    )


def _resolve(project_root: Path, file_path: str) -> Path:
    path = Path(file_path)
    return path if path.is_absolute() else project_root / path


def make_symbolic_tools(project_root: Path) -> list:
    """Build Z3-backed path-constraint tools scoped to `project_root`."""

    @tool
    def solve_branch_inputs(file_path: str, function_name: str, num_solutions: int = 1) -> str:
        """Compute concrete argument values that drive execution down each branch of a
        function. This is the primary way to cover a branch: it parses the function
        (C/C++, Python, Java), derives every branch's path condition, translates it to
        Z3, and solves it.

        Returns JSON with, for each branch and each outcome (`true`/`false`):
          - `status`: "sat" (use `inputs` as the test arguments), "unsat" (the path is
            INFEASIBLE — dead code, do not write a test for it; `unsat_core` says why),
            "skipped" (`blockers` explains what could not be translated — fall back to
            reading the code), or "unknown"/"error".
          - `inputs`: one concrete value per parameter. Ints come back plain; Reals as
            {"value", "exact"}; fixed-width ints as {"signed", "unsigned", "hex"} —
            pick the field matching the parameter's signedness.

        `num_solutions` > 1 returns extra distinct assignments per branch, useful for
        parameterized tests. Call this before writing tests for any function whose
        branch guards are not trivially satisfiable by inspection.
        """
        path = _resolve(project_root, file_path)
        if not path.is_file():
            return f"ERROR: file not found: {path}"
        try:
            result = symbolic.solve_all_branches(
                path, function_name, num_solutions=max(1, num_solutions)
            )
        except (ValueError, FileNotFoundError) as exc:
            return f"ERROR: {exc}"
        # Specs are redundant once solved; drop them to keep the payload small.
        for branch in result["branches"]:
            for outcome in branch["outcomes"].values():
                if outcome.get("status") != "skipped":
                    outcome.pop("spec", None)
                outcome.pop("unsigned_vars", None)
        return _dump(result)

    @tool
    def extract_path_conditions(file_path: str, function_name: str) -> str:
        """Show the path condition of every branch in a function WITHOUT solving it:
        the parameters and their Z3 sorts, each branch's source condition, its
        translation to Z3, the enclosing guards, and a ready-to-solve spec per outcome.

        Use this when `solve_branch_inputs` reports `skipped` or `error` for a branch:
        read the emitted spec, fix it by hand (declare the missing variable, rewrite the
        untranslatable term such as a function call or field access in terms of the
        inputs), then pass it to `solve_path_constraints`.
        """
        path = _resolve(project_root, file_path)
        if not path.is_file():
            return f"ERROR: file not found: {path}"
        try:
            return _dump(symbolic.analyze_file_function(path, function_name))
        except (ValueError, FileNotFoundError) as exc:
            return f"ERROR: {exc}"

    @tool
    def solve_path_constraints(spec_json: str, num_solutions: int = 1) -> str:
        """Solve a hand-written constraint system with Z3 and return concrete values.

        `spec_json` is a JSON object:
          {"vars": {"x": "Int", "w": "BitVec32", "s": "String"},
           "constraints": ["x > 0", {"name": "branch", "expr": "And(x < 10, Length(s) == 3)"}],
           "optimize": {"goal": "minimize", "objective": "x"}}

        Sorts: Int, Real, Bool, String, BitVec<N>, Array(K,V), Seq(T). Constraints are
        Z3 Python expressions (And/Or/Not/Implies/If, Length, Select, ULT/UGT for
        unsigned comparison). Returns {"status", "model", "solutions", "unsat_core"}.

        Use this for constraints you derived yourself — a branch the automatic
        extraction could not translate, or a guard combined with extra assumptions the
        test needs. See the symbolic-execution skill for the full spec format.
        """
        try:
            spec = json.loads(spec_json)
        except json.JSONDecodeError as exc:
            return f"ERROR: invalid JSON spec: {exc}"
        if not isinstance(spec, dict):
            return "ERROR: spec must be a JSON object"
        spec.setdefault("num_solutions", max(1, num_solutions))
        try:
            return _dump(symbolic.solve(spec))
        except Exception as exc:  # noqa: BLE001 - surfaced to the model, never fatal
            return f"ERROR: {type(exc).__name__}: {exc}"

    return [solve_branch_inputs, extract_path_conditions, solve_path_constraints]
