"""Tests for the source-to-Z3 translation layer in src/symbolic.py.

Covers the parts a happy-path run never reaches: parameter shapes, literal
forms, operator promotion, the pure-call whitelist, loop guards, and the error
paths of the solver plumbing.
"""

import json

import pytest

from src import symbolic
from src.tools.symbolic import make_symbolic_tools


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return path


def _branch(analysis, branch_id):
    for branch in analysis["branches"]:
        if branch["id"] == branch_id:
            return branch
    raise AssertionError(f"no branch {branch_id} in {[b['id'] for b in analysis['branches']]}")


def _guard_expr(analysis, branch_id, label="true"):
    """The branch's own guard — always the last constraint in its spec."""
    return _branch(analysis, branch_id)["outcomes"][label]["spec"]["constraints"][-1]["expr"]


def _cpp(tmp_path, body, params="int k"):
    text = f"int f({params}) {{\n{body}\n    return 0;\n}}\n"
    return symbolic.analyze_file_function(_write(tmp_path, "f.cpp", text), "f")


def _java(tmp_path, body, params="int k"):
    text = f"class C {{\n    int f({params}) {{\n{body}\n        return 0;\n    }}\n}}\n"
    return symbolic.analyze_file_function(_write(tmp_path, "C.java", text), "f")


def _py(tmp_path, body, params="k: int"):
    text = f"def f({params}) -> int:\n{body}\n    return 0\n"
    return symbolic.analyze_file_function(_write(tmp_path, "f.py", text), "f")


# --------------------------------------------------------------------------- #
# Parameter extraction
# --------------------------------------------------------------------------- #


def test_cpp_reference_and_const_qualifiers_are_stripped(tmp_path):
    analysis = _cpp(tmp_path, "    if (s.size() > 1) { return 1; }", params="const std::string& s")
    assert analysis["parameters"][0]["sort"] == "String"


def test_cpp_pointer_parameter_is_reported_without_a_sort(tmp_path):
    analysis = _cpp(tmp_path, "    if (k == 0) { return 1; }", params="int* p, int k")
    pointer = next(p for p in analysis["parameters"] if p["name"] == "p")
    assert pointer["declared_type"] == "int*"
    assert pointer["sort"] is None


def test_cpp_char_pointer_is_treated_as_a_string(tmp_path):
    analysis = _cpp(tmp_path, "    if (k == 0) { return 1; }", params="const char* name, int k")
    assert next(p for p in analysis["parameters"] if p["name"] == "name")["sort"] == "String"


def test_function_without_parameters_yields_none(tmp_path):
    analysis = _cpp(tmp_path, "    if (1 > 0) { return 1; }", params="")
    assert analysis["parameters"] == []


def test_python_unannotated_parameter_has_a_note(tmp_path):
    analysis = _py(tmp_path, "    if k > 1:\n        return 1", params="k, j: int")
    untyped = next(p for p in analysis["parameters"] if p["name"] == "k")
    assert untyped["sort"] is None
    assert "no declared type" in untyped["note"]


def test_python_default_and_keyword_only_parameters_are_read(tmp_path):
    analysis = _py(tmp_path, "    if k > 1:\n        return 1", params='k: int = 3, *, r: float = 1.0')
    sorts = {p["name"]: p["sort"] for p in analysis["parameters"]}
    assert sorts == {"k": "Int", "r": "Real"}


def test_python_self_is_not_a_parameter(tmp_path):
    text = "class A:\n    def f(self, k: int) -> int:\n        if k > 1:\n            return 1\n        return 0\n"
    analysis = symbolic.analyze_file_function(_write(tmp_path, "a.py", text), "A.f")
    assert [p["name"] for p in analysis["parameters"]] == ["k"]


def test_python_optional_annotation_unwraps_to_the_inner_sort(tmp_path):
    analysis = _py(tmp_path, "    if k > 1:\n        return 1", params="k: int | None")
    assert analysis["parameters"][0]["sort"] == "Int"


def test_java_array_parameter_maps_to_an_array_sort(tmp_path):
    analysis = _java(tmp_path, "        if (k > 1) { return 1; }", params="int[] xs, int k")
    assert next(p for p in analysis["parameters"] if p["name"] == "xs")["sort"] == "Array(Int,BitVec32)"


def test_java_char_is_treated_as_unsigned(tmp_path):
    analysis = _java(tmp_path, "        if (c < 5) { return 1; }", params="char c")
    assert _guard_expr(analysis, "if_statement@3") == "ULT(c, 5)"


# --------------------------------------------------------------------------- #
# Literals
# --------------------------------------------------------------------------- #


def test_boolean_literals_translate(tmp_path):
    analysis = _cpp(tmp_path, "    if (true) { return 1; }\n    if (false) { return 2; }")
    assert _guard_expr(analysis, "if_statement@2") == "True"
    assert _guard_expr(analysis, "if_statement@3") == "False"


def test_string_literal_becomes_a_z3_string_value(tmp_path):
    analysis = _java(tmp_path, '        if (s.equals("hi")) { return 1; }', params="String s")
    assert _guard_expr(analysis, "if_statement@3") == '(s == StringVal("hi"))'


def test_null_comparison_is_untranslatable(tmp_path):
    analysis = _java(tmp_path, "        if (s == null) { return 1; }", params="String s")
    assert not _branch(analysis, "if_statement@3")["outcomes"]["true"]["solvable"]


# --------------------------------------------------------------------------- #
# Operators
# --------------------------------------------------------------------------- #


def test_unary_minus_and_bitwise_not(tmp_path):
    analysis = _cpp(tmp_path, "    if (-k > 3) { return 1; }\n    if (~k == 0) { return 2; }")
    assert _guard_expr(analysis, "if_statement@2") == "((-k) > 3)"
    assert _guard_expr(analysis, "if_statement@3") == "((~k) == 0)"


def test_unsigned_right_shift_uses_logical_shift(tmp_path):
    analysis = _cpp(tmp_path, "    if ((m >> 2) == 1) { return 1; }", params="unsigned m")
    assert _guard_expr(analysis, "if_statement@2") == "((LShR(m, 2)) == 1)"


def test_signed_right_shift_stays_arithmetic(tmp_path):
    analysis = _cpp(tmp_path, "    if ((k >> 2) == 1) { return 1; }")
    assert _guard_expr(analysis, "if_statement@2") == "(((k >> 2)) == 1)"


def test_narrower_left_operand_is_promoted(tmp_path):
    """`a * 3 == b` widens the 32-bit left side to meet the 64-bit right side."""
    analysis = _java(tmp_path, "        if (a * 3 == b) { return 1; }", params="int a, long b")
    assert "SignExt(32," in _guard_expr(analysis, "if_statement@3")


def test_unsigned_operand_is_zero_extended(tmp_path):
    analysis = _cpp(tmp_path, "    if (m == big) { return 1; }", params="unsigned m, unsigned long big")
    assert "ZeroExt(32," in _guard_expr(analysis, "if_statement@2")


def test_bitvec_meeting_a_length_is_converted_to_int(tmp_path):
    analysis = _java(tmp_path, "        if (a < s.length()) { return 1; }", params="int a, String s")
    assert _guard_expr(analysis, "if_statement@3") == "(BV2Int(a, is_signed=True) < Length(s))"


def test_bitvec_meeting_a_real_is_converted(tmp_path):
    analysis = _cpp(tmp_path, "    if (k < r) { return 1; }", params="int k, double r")
    assert _guard_expr(analysis, "if_statement@2") == "(ToReal(BV2Int(k, is_signed=True)) < r)"


def test_length_on_the_left_converts_the_bitvec_on_the_right(tmp_path):
    """The mirror of the previous case — conversion must work from either side."""
    analysis = _java(tmp_path, "        if (s.length() > a) { return 1; }", params="int a, String s")
    assert _guard_expr(analysis, "if_statement@3") == "(Length(s) > BV2Int(a, is_signed=True))"


def test_int_meeting_a_real_is_converted(tmp_path):
    analysis = _py(tmp_path, "    if k < r:\n        return 1", params="k: int, r: float")
    assert _guard_expr(analysis, "if_statement@2") == "(ToReal(k) < r)"


def test_real_on_the_left_converts_the_int_on_the_right(tmp_path):
    analysis = _py(tmp_path, "    if r < k:\n        return 1", params="k: int, r: float")
    assert _guard_expr(analysis, "if_statement@2") == "(r < ToReal(k))"


def test_real_on_the_left_converts_the_bitvec_on_the_right(tmp_path):
    analysis = _cpp(tmp_path, "    if (r < k) { return 1; }", params="int k, double r")
    assert _guard_expr(analysis, "if_statement@2") == "(r < ToReal(BV2Int(k, is_signed=True)))"


def test_unsigned_bitvec_converts_to_an_unsigned_int(tmp_path):
    analysis = _java(tmp_path, "        if (c < s.length()) { return 1; }", params="char c, String s")
    assert "is_signed=False" in _guard_expr(analysis, "if_statement@3")


def test_python_arithmetic_operators_translate(tmp_path):
    analysis = _py(tmp_path, "    if k % 7 == 3:\n        return 1")
    assert _guard_expr(analysis, "if_statement@2") == "((k % 7) == 3)"


def test_python_floor_division_is_flagged_as_an_approximation(tmp_path):
    analysis = _py(tmp_path, "    if k // 2 > 3:\n        return 1")
    outcome = _branch(analysis, "if_statement@2")["outcomes"]["true"]
    assert outcome["spec"]["constraints"][-1]["expr"] == "((k / 2) > 3)"
    assert any("floor division" in w for w in outcome["warnings"])


def test_python_membership_test_is_untranslatable(tmp_path):
    analysis = _py(tmp_path, "    if k in (1, 2):\n        return 1")
    assert not _branch(analysis, "if_statement@2")["outcomes"]["true"]["solvable"]


def test_ternary_is_untranslatable_and_noted(tmp_path):
    analysis = _py(tmp_path, "    if (k if j else 0) > 1:\n        return 1", params="k: int, j: bool")
    assert not _branch(analysis, "if_statement@2")["outcomes"]["true"]["solvable"]
    assert any("ternary" in n for n in analysis["notes"])


def test_switch_statement_is_noted_as_undecomposed(tmp_path):
    analysis = _cpp(tmp_path, "    switch (k) { case 1: return 1; }")
    assert any("switch" in n for n in analysis["notes"])


# --------------------------------------------------------------------------- #
# Pure-call whitelist
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("s.size() > 2", "(Length(s) > 2)"),
        ("s.length() > 2", "(Length(s) > 2)"),
        ("strlen(s) > 2", "(Length(s) > 2)"),
    ],
)
def test_cpp_string_length_calls(tmp_path, expr, expected):
    analysis = _cpp(tmp_path, f"    if ({expr}) {{ return 1; }}", params="std::string s")
    assert _guard_expr(analysis, "if_statement@2") == expected


def test_cpp_abs_expands_to_a_conditional(tmp_path):
    analysis = _cpp(tmp_path, "    if (abs(k) > 3) { return 1; }")
    assert _guard_expr(analysis, "if_statement@2") == "(If(k >= 0, k, -k) > 3)"


def test_java_is_empty_expands_to_a_length_check(tmp_path):
    analysis = _java(tmp_path, "        if (s.isEmpty()) { return 1; }", params="String s")
    assert _guard_expr(analysis, "if_statement@3") == "(Length(s) == 0)"


def test_length_on_a_non_string_is_rejected(tmp_path):
    """`v.size()` on a vector must not silently become Length(v)."""
    analysis = _cpp(tmp_path, "    if (v.size() > 2) { return 1; }", params="std::vector<int> v")
    assert not _branch(analysis, "if_statement@2")["outcomes"]["true"]["solvable"]


def test_call_outside_the_whitelist_is_rejected(tmp_path):
    analysis = _py(tmp_path, '    if s.startswith("q"):\n        return 1', params="s: str")
    outcome = _branch(analysis, "if_statement@2")["outcomes"]["true"]
    assert any("startswith" in b for b in outcome["blockers"])


def test_qualified_call_with_a_receiver_is_rejected_when_arity_mismatches(tmp_path):
    """`Math.abs(a)` reaches the whitelist by name but has receiver + arg."""
    analysis = _java(tmp_path, "        if (Math.abs(a) > 3) { return 1; }", params="int a")
    assert any("Math.abs" in b for b in _branch(analysis, "if_statement@3")["outcomes"]["true"]["blockers"])


@pytest.mark.parametrize(
    ("call", "params"),
    [
        ("strlen(s, 2) > 1", "std::string s"),   # Length with two arguments
        ("abs(k, 2) > 1", "int k"),              # abs with two arguments
    ],
)
def test_whitelisted_call_with_the_wrong_arity_is_rejected(tmp_path, call, params):
    analysis = _cpp(tmp_path, f"    if ({call}) {{ return 1; }}", params=params)
    assert not _branch(analysis, "if_statement@2")["outcomes"]["true"]["solvable"]


@pytest.mark.parametrize(
    "call",
    ["s.isEmpty(1)", "s.equals()"],
)
def test_java_whitelisted_call_with_the_wrong_arity_is_rejected(tmp_path, call):
    analysis = _java(tmp_path, f"        if ({call}) {{ return 1; }}", params="String s")
    assert not _branch(analysis, "if_statement@3")["outcomes"]["true"]["solvable"]


# --------------------------------------------------------------------------- #
# Loops
# --------------------------------------------------------------------------- #


def test_while_condition_guards_the_body(tmp_path):
    analysis = _cpp(tmp_path, "    while (k > 0) { if (k < 5) { return 1; } }")
    assert [g["z3"] for g in _branch(analysis, "if_statement@2")["path_condition"]] == ["(k > 0)"]


def test_cpp_for_condition_guards_the_body(tmp_path):
    analysis = _cpp(tmp_path, "    for (int i = 0; i < k; i++) { if (k > 3) { return 1; } }")
    assert [g["z3"] for g in _branch(analysis, "if_statement@2")["path_condition"]] == ["(i < k)"]


def test_do_while_body_needs_no_guard(tmp_path):
    """A do/while body always executes once, so it adds nothing to the path."""
    analysis = _cpp(tmp_path, "    do { if (k < 9) { return 1; } } while (k > 0);")
    assert _branch(analysis, "if_statement@2")["path_condition"] == []


# --------------------------------------------------------------------------- #
# Spec assembly
# --------------------------------------------------------------------------- #


def test_parameter_without_a_sort_used_in_a_guard_blocks_the_spec(tmp_path):
    analysis = _py(tmp_path, "    if j > 1:\n        return 1", params="j")
    blockers = _branch(analysis, "if_statement@2")["outcomes"]["true"]["blockers"]
    assert any("no Z3 sort" in b for b in blockers)


def test_unsigned_vars_are_recorded_on_the_outcome(tmp_path):
    analysis = _cpp(tmp_path, "    if (m > 1) { return 1; }", params="unsigned m, int k")
    assert _branch(analysis, "if_statement@2")["outcomes"]["true"]["unsigned_vars"] == ["m"]


def test_constraint_names_encode_line_and_polarity(tmp_path):
    analysis = _cpp(tmp_path, "    if (k > 0) {\n        if (k < 9) { return 1; }\n    }")
    names = [c["name"] for c in _branch(analysis, "if_statement@3")["outcomes"]["false"]["spec"]["constraints"]]
    assert names == ["line2_true", "line3_false"]


def test_unsupported_file_type_raises(tmp_path):
    with pytest.raises(ValueError, match="unsupported file type"):
        symbolic.analyze_file_function(_write(tmp_path, "notes.txt", "hello"), "f")


# --------------------------------------------------------------------------- #
# Solver plumbing
# --------------------------------------------------------------------------- #


def test_solver_module_is_loaded_from_the_skill(tmp_path):
    assert hasattr(symbolic.load_solver(), "solve_spec")


def test_missing_solver_script_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(symbolic, "_solver_module", None)
    monkeypatch.setattr(symbolic, "_SOLVER_PATH", tmp_path / "gone.py")
    with pytest.raises(FileNotFoundError, match="solver script missing"):
        symbolic.load_solver()


def test_prefer_small_can_be_disabled(tmp_path):
    spec = {"vars": {"x": "Int"}, "constraints": [{"name": "c", "expr": "x > 100000"}]}
    result = symbolic.solve_outcome(spec, prefer_small=False)
    assert result["status"] == "sat"
    assert result["bounded"] is False


def test_solver_failure_is_reported_per_branch(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("solver exploded")

    monkeypatch.setattr(symbolic, "solve_outcome", boom)
    result = symbolic.solve_all_branches(_write(tmp_path, "f.py", "def f(k: int) -> int:\n    if k > 1:\n        return 1\n    return 0\n"), "f")
    outcome = _branch(result, "if_statement@2")["outcomes"]["true"]
    assert outcome["status"] == "error"
    assert "solver exploded" in outcome["error"]
    assert result["summary"]["error"] == 2


def test_unknown_status_carries_the_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(
        symbolic, "solve_outcome",
        lambda *_a, **_k: {"status": "unknown", "reason": "timeout", "bounded": False},
    )
    result = symbolic.solve_all_branches(_write(tmp_path, "f.py", "def f(k: int) -> int:\n    if k > 1:\n        return 1\n    return 0\n"), "f")
    assert _branch(result, "if_statement@2")["outcomes"]["true"]["reason"] == "timeout"


# --------------------------------------------------------------------------- #
# Tool wrappers
# --------------------------------------------------------------------------- #


@pytest.fixture
def tools(tmp_path):
    _write(tmp_path, "f.py", "def f(k: int) -> int:\n    if k > 1:\n        return 1\n    return 0\n")
    return {t.name: t for t in make_symbolic_tools(tmp_path)}


def test_oversized_output_is_truncated_with_a_hint(tools, monkeypatch):
    monkeypatch.setattr("src.tools.symbolic.MAX_OUTPUT_CHARS", 120)
    out = tools["solve_branch_inputs"].invoke({"file_path": "f.py", "function_name": "f"})
    assert out.endswith("re-run on a single branch with solve_path_constraints")
    assert len(out) < 300


def test_solve_branch_inputs_drops_the_spec_once_solved(tools):
    out = json.loads(tools["solve_branch_inputs"].invoke({"file_path": "f.py", "function_name": "f"}))
    outcome = out["branches"][0]["outcomes"]["true"]
    assert "spec" not in outcome
    assert "unsigned_vars" not in outcome


def test_solve_branch_inputs_keeps_the_spec_when_skipped(tmp_path):
    _write(tmp_path, "s.py", "def f(k: int) -> int:\n    if helper(k):\n        return 1\n    return 0\n")
    tools = {t.name: t for t in make_symbolic_tools(tmp_path)}
    out = json.loads(tools["solve_branch_inputs"].invoke({"file_path": "s.py", "function_name": "f"}))
    outcome = out["branches"][0]["outcomes"]["true"]
    assert outcome["status"] == "skipped"
    assert "spec" in outcome


def test_num_solutions_is_clamped_to_at_least_one(tools):
    out = json.loads(tools["solve_branch_inputs"].invoke(
        {"file_path": "f.py", "function_name": "f", "num_solutions": 0}
    ))
    assert out["summary"]["sat"] > 0


def test_solve_path_constraints_rejects_a_non_object_spec(tools):
    assert tools["solve_path_constraints"].invoke({"spec_json": "[1, 2]"}) == "ERROR: spec must be a JSON object"


def test_solve_path_constraints_reports_a_bad_sort(tools):
    out = tools["solve_path_constraints"].invoke(
        {"spec_json": json.dumps({"vars": {"x": "Widget"}, "constraints": ["x > 0"]})}
    )
    assert "unknown sort" in out


def test_solve_path_constraints_supports_optimization(tools):
    out = json.loads(tools["solve_path_constraints"].invoke({
        "spec_json": json.dumps({
            "vars": {"x": "Int"},
            "constraints": ["x > 3"],
            "optimize": {"goal": "minimize", "objective": "x"},
        })
    }))
    assert out["model"]["x"] == 4


def test_extract_path_conditions_rejects_a_missing_file(tools):
    assert tools["extract_path_conditions"].invoke(
        {"file_path": "nope.py", "function_name": "f"}
    ).startswith("ERROR: file not found")


def test_solve_branch_inputs_reports_an_unknown_function(tools):
    out = tools["solve_branch_inputs"].invoke({"file_path": "f.py", "function_name": "missing"})
    assert out.startswith("ERROR: no function matching")
