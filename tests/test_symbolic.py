"""Tests for symbolic path-condition extraction and Z3 solving."""

import json

import pytest

from src import symbolic
from src.tools.symbolic import make_symbolic_tools

CPP_SRC = """\
int classify(int a, unsigned b, double r, bool f) {
    if (a > 0 && !f) {
        if (b == a * 3 + 7) { return 1; }
    } else if (a < -100) {
        if (a > 0) { return 99; }
    }
    return 0;
}
"""

PY_SRC = """\
def classify(a: int, b: float, s: str) -> int:
    if a > 0 and len(s) > 2:
        if 0 < b < 1.5:
            return 1
    elif a == -5:
        return 2
    return 0
"""

JAVA_SRC = """\
class Classify {
    int classify(int a, long b, boolean f) {
        if (a > 0 && !f) {
            if (b == a * 3L + 7) { return 1; }
        }
        return 0;
    }
}
"""


@pytest.fixture
def sources(tmp_path):
    """Write the three sample sources and return their paths by language."""
    paths = {
        "cpp": tmp_path / "classify.cpp",
        "python": tmp_path / "classify.py",
        "java": tmp_path / "Classify.java",
    }
    paths["cpp"].write_text(CPP_SRC)
    paths["python"].write_text(PY_SRC)
    paths["java"].write_text(JAVA_SRC)
    return paths


def _branch(analysis, branch_id):
    for branch in analysis["branches"]:
        if branch["id"] == branch_id:
            return branch
    raise AssertionError(f"no branch {branch_id} in {[b['id'] for b in analysis['branches']]}")


def _constraint(outcome):
    """The last constraint of a spec is the branch's own guard."""
    return outcome["spec"]["constraints"][-1]["expr"]


# --------------------------------------------------------------------------- #
# Parameter sorts
# --------------------------------------------------------------------------- #


def test_cpp_parameters_map_to_bitvecs_and_reals(sources):
    analysis = symbolic.analyze_file_function(sources["cpp"], "classify")
    sorts = {p["name"]: p["sort"] for p in analysis["parameters"]}
    assert sorts == {"a": "BitVec32", "b": "BitVec32", "r": "Real", "f": "Bool"}


def test_python_int_is_unbounded_not_a_bitvec(sources):
    analysis = symbolic.analyze_file_function(sources["python"], "classify")
    sorts = {p["name"]: p["sort"] for p in analysis["parameters"]}
    assert sorts == {"a": "Int", "b": "Real", "s": "String"}


def test_java_long_is_wider_than_int(sources):
    analysis = symbolic.analyze_file_function(sources["java"], "classify")
    sorts = {p["name"]: p["sort"] for p in analysis["parameters"]}
    assert sorts == {"a": "BitVec32", "b": "BitVec64", "f": "Bool"}


def test_unmapped_type_reports_a_note_and_no_sort(tmp_path):
    src = tmp_path / "widget.cpp"
    src.write_text("int f(Widget w) { if (w > 0) { return 1; } return 0; }\n")
    analysis = symbolic.analyze_file_function(src, "f")
    param = analysis["parameters"][0]
    assert param["sort"] is None
    assert "no direct Z3 sort" in param["note"]


# --------------------------------------------------------------------------- #
# Condition translation
# --------------------------------------------------------------------------- #


def test_logical_operators_become_z3_calls(sources):
    analysis = symbolic.analyze_file_function(sources["cpp"], "classify")
    expr = _constraint(_branch(analysis, "if_statement@2")["outcomes"]["true"])
    assert expr == "And((a > 0), Not(f))"


def test_false_outcome_negates_the_guard(sources):
    analysis = symbolic.analyze_file_function(sources["cpp"], "classify")
    expr = _constraint(_branch(analysis, "if_statement@2")["outcomes"]["false"])
    assert expr == "Not(And((a > 0), Not(f)))"


def test_java_promotes_the_narrower_integer(sources):
    """`b == a * 3L + 7` mixes a 32-bit int with a 64-bit long."""
    analysis = symbolic.analyze_file_function(sources["java"], "classify")
    expr = _constraint(_branch(analysis, "if_statement@4")["outcomes"]["true"])
    assert "SignExt(32," in expr


def test_unsigned_comparison_uses_the_unsigned_builtin(tmp_path):
    src = tmp_path / "u.cpp"
    src.write_text("int f(unsigned n) { if (n < 5) { return 1; } return 0; }\n")
    analysis = symbolic.analyze_file_function(src, "f")
    assert _constraint(_branch(analysis, "if_statement@1")["outcomes"]["true"]) == "ULT(n, 5)"


def test_signed_comparison_stays_infix(tmp_path):
    src = tmp_path / "s.cpp"
    src.write_text("int f(int n) { if (n < 5) { return 1; } return 0; }\n")
    analysis = symbolic.analyze_file_function(src, "f")
    assert _constraint(_branch(analysis, "if_statement@1")["outcomes"]["true"]) == "(n < 5)"


def test_python_chained_comparison_expands_to_and(sources):
    analysis = symbolic.analyze_file_function(sources["python"], "classify")
    expr = _constraint(_branch(analysis, "if_statement@3")["outcomes"]["true"])
    assert expr == "And((0 < b), (b < 1.5))"


def test_len_translates_to_z3_length(sources):
    analysis = symbolic.analyze_file_function(sources["python"], "classify")
    expr = _constraint(_branch(analysis, "if_statement@2")["outcomes"]["true"])
    assert "Length(s)" in expr


def test_java_literal_suffix_is_stripped(sources):
    analysis = symbolic.analyze_file_function(sources["java"], "classify")
    expr = _constraint(_branch(analysis, "if_statement@4")["outcomes"]["true"])
    assert "3L" not in expr


def test_untranslatable_call_blocks_the_spec(tmp_path):
    src = tmp_path / "call.py"
    src.write_text("def f(a: int) -> int:\n    if helper(a) > 2:\n        return 1\n    return 0\n")
    analysis = symbolic.analyze_file_function(src, "f")
    outcome = _branch(analysis, "if_statement@2")["outcomes"]["true"]
    assert not outcome["solvable"]
    assert any("helper(a)" in b for b in outcome["blockers"])


def test_local_variable_is_not_mistaken_for_an_input(tmp_path):
    src = tmp_path / "local.py"
    src.write_text("def f(a: int) -> int:\n    t = a * 2\n    if t > 10:\n        return 1\n    return 0\n")
    analysis = symbolic.analyze_file_function(src, "f")
    outcome = _branch(analysis, "if_statement@3")["outcomes"]["true"]
    assert not outcome["solvable"]
    assert any("not bound to a parameter" in b and "t" in b for b in outcome["blockers"])


# --------------------------------------------------------------------------- #
# Path conditions
# --------------------------------------------------------------------------- #


def test_nested_branch_inherits_the_enclosing_guard(sources):
    analysis = symbolic.analyze_file_function(sources["cpp"], "classify")
    inner = _branch(analysis, "if_statement@3")
    assert [g["z3"] for g in inner["path_condition"]] == ["And((a > 0), Not(f))"]


def test_else_arm_inherits_the_negated_guard(sources):
    analysis = symbolic.analyze_file_function(sources["cpp"], "classify")
    inner = _branch(analysis, "if_statement@5")
    assert [g["z3"] for g in inner["path_condition"]] == ["Not(And((a > 0), Not(f)))", "(a < -100)"]


def test_python_elif_is_reported_as_a_branch(sources):
    analysis = symbolic.analyze_file_function(sources["python"], "classify")
    assert _branch(analysis, "elif_clause@5")["condition"] == "a == -5"


def test_missing_function_raises(sources):
    with pytest.raises(ValueError, match="no function matching"):
        symbolic.analyze_file_function(sources["cpp"], "nope")


# --------------------------------------------------------------------------- #
# Solving
# --------------------------------------------------------------------------- #


def test_solved_inputs_actually_satisfy_the_guard(sources):
    result = symbolic.solve_all_branches(sources["python"], "classify")
    outcome = _branch(result, "if_statement@3")["outcomes"]["true"]
    assert outcome["status"] == "sat"
    inputs = outcome["inputs"]
    assert inputs["a"] > 0 and len(inputs["s"]) > 2
    assert 0 < inputs["b"]["value"] < 1.5


def test_contradictory_path_is_reported_as_dead_code(sources):
    """`if (a > 0)` nested inside `else if (a < -100)` can never be taken."""
    result = symbolic.solve_all_branches(sources["cpp"], "classify")
    outcome = _branch(result, "if_statement@5")["outcomes"]["true"]
    assert outcome["status"] == "unsat"
    assert "infeasible" in outcome["verdict"]
    assert len(outcome["unsat_core"]) >= 2


def test_inputs_stay_in_a_readable_range_when_possible(sources):
    result = symbolic.solve_all_branches(sources["java"], "classify")
    inputs = _branch(result, "if_statement@4")["outcomes"]["true"]["inputs"]
    assert abs(inputs["a"]["signed"]) <= 1000
    assert abs(inputs["b"]["signed"]) <= 1000


def test_readability_bounds_never_turn_sat_into_unsat(tmp_path):
    """A branch only reachable with huge values must still be solved."""
    src = tmp_path / "big.py"
    src.write_text("def f(a: int) -> int:\n    if a > 100000:\n        return 1\n    return 0\n")
    result = symbolic.solve_all_branches(src, "f")
    outcome = _branch(result, "if_statement@2")["outcomes"]["true"]
    assert outcome["status"] == "sat"
    assert outcome["inputs"]["a"] > 100000
    assert any("readable range" in w for w in outcome["warnings"])


def test_num_solutions_returns_distinct_assignments(sources):
    result = symbolic.solve_all_branches(sources["python"], "classify", num_solutions=3)
    outcome = _branch(result, "if_statement@2")["outcomes"]["true"]
    models = [outcome["inputs"], *outcome["alternative_inputs"]]
    assert len(models) == 3
    assert len({json.dumps(m, sort_keys=True) for m in models}) == 3


def test_summary_counts_every_outcome(sources):
    result = symbolic.solve_all_branches(sources["cpp"], "classify")
    total = sum(len(b["outcomes"]) for b in result["branches"])
    assert sum(result["summary"].values()) == total


# --------------------------------------------------------------------------- #
# Tool wrappers
# --------------------------------------------------------------------------- #


@pytest.fixture
def tools(tmp_path, sources):
    return {t.name: t for t in make_symbolic_tools(tmp_path)}


def test_solve_branch_inputs_tool_returns_json(tools):
    out = json.loads(tools["solve_branch_inputs"].invoke(
        {"file_path": "classify.py", "function_name": "classify"}
    ))
    assert out["language"] == "python"
    assert out["summary"]["sat"] > 0


def test_extract_path_conditions_tool_keeps_the_spec(tools):
    out = json.loads(tools["extract_path_conditions"].invoke(
        {"file_path": "classify.cpp", "function_name": "classify"}
    ))
    assert out["branches"][0]["outcomes"]["true"]["spec"]["vars"]["a"] == "BitVec32"


def test_solve_path_constraints_tool_solves_a_hand_written_spec(tools):
    out = json.loads(tools["solve_path_constraints"].invoke({
        "spec_json": json.dumps({
            "vars": {"x": "Int", "y": "Int"},
            "constraints": ["x > 0", "y == x * 3 + 7", "y < 20"],
        })
    }))
    assert out["status"] == "sat"
    assert out["model"]["y"] == out["model"]["x"] * 3 + 7


def test_solve_path_constraints_tool_reports_unsat_core(tools):
    out = json.loads(tools["solve_path_constraints"].invoke({
        "spec_json": json.dumps({
            "vars": {"x": "Int"},
            "constraints": [
                {"name": "pos", "expr": "x > 5"},
                {"name": "neg", "expr": "x < 0"},
            ],
        })
    }))
    assert out["status"] == "unsat"
    assert set(out["unsat_core"]) == {"pos", "neg"}


def test_tools_report_bad_input_instead_of_raising(tools):
    assert tools["solve_path_constraints"].invoke({"spec_json": "{not json"}).startswith("ERROR")
    assert tools["solve_branch_inputs"].invoke(
        {"file_path": "missing.py", "function_name": "f"}
    ).startswith("ERROR: file not found")
    assert tools["extract_path_conditions"].invoke(
        {"file_path": "classify.py", "function_name": "nope"}
    ).startswith("ERROR")
