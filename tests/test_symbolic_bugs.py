"""Regression tests for known defects in src/symbolic.py.

Each test here asserts the CORRECT behaviour and is marked
`xfail(strict=True)`, so the suite stays green while the bug is open and turns
red the moment a fix lands — at which point the marker should be deleted.

The defects all share one consequence: the solver reports inputs that do not
actually drive execution to the branch, which is the single guarantee the whole
symbolic-execution flow rests on. Where possible a test proves that by running
the function with the reported inputs.
"""

import pytest

from src import symbolic

# Functions are written so the return value uniquely identifies the branch that
# executed — no tracing needed to tell which path a set of inputs really took.

EARLY_RETURN_SRC = """\
def f(a: int) -> int:
    if a > 5:
        return 1
    if a > 100:
        return 2
    return 0
"""

LOOP_SRC = """\
def h(items: list[int], k: int) -> int:
    for x in items:
        if k > 3:
            return 1
    return 0
"""

TRUTHY_SRC = """\
def g(s: str) -> int:
    if not s:
        return 1
    if s:
        return 2
    return 0
"""

HEX_SRC = """\
int f(int k) {
    if (k == -0x1F) { return 1; }
    return 0;
}
"""


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return path


def _branch(analysis, branch_id):
    for branch in analysis["branches"]:
        if branch["id"] == branch_id:
            return branch
    raise AssertionError(f"no branch {branch_id} in {[b['id'] for b in analysis['branches']]}")


def _call(source, fn_name, **kwargs):
    """Execute the sample function and return which branch it actually took."""
    namespace: dict = {}
    exec(compile(source, "<sample>", "exec"), namespace)  # noqa: S102 - fixed test source
    return namespace[fn_name](**kwargs)


# --------------------------------------------------------------------------- #
# Sequential guards: path_condition only walks ancestors, never preceding
# siblings that already returned.
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(strict=True, reason="path_condition ignores preceding guard clauses")
def test_preceding_guard_clause_is_part_of_the_path_condition(tmp_path):
    """`if a > 100` sits after `if a > 5: return`, so it can only see a <= 5."""
    src = _write(tmp_path, "early.py", EARLY_RETURN_SRC)
    analysis = symbolic.analyze_file_function(src, "f")
    guards = _branch(analysis, "if_statement@4")["path_condition"]
    assert [g["z3"] for g in guards] == ["Not((a > 5))"]


@pytest.mark.xfail(strict=True, reason="path_condition ignores preceding guard clauses")
def test_unreachable_branch_after_a_guard_clause_is_proved_infeasible(tmp_path):
    """a <= 5 and a > 100 cannot both hold: the branch is dead code."""
    src = _write(tmp_path, "early.py", EARLY_RETURN_SRC)
    result = symbolic.solve_all_branches(src, "f")
    assert _branch(result, "if_statement@4")["outcomes"]["true"]["status"] == "unsat"


@pytest.mark.xfail(strict=True, reason="path_condition ignores preceding guard clauses")
def test_reported_inputs_reach_the_branch_they_claim(tmp_path):
    """The core guarantee: running with the solved inputs must take that path."""
    src = _write(tmp_path, "early.py", EARLY_RETURN_SRC)
    result = symbolic.solve_all_branches(src, "f")
    outcome = _branch(result, "if_statement@4")["outcomes"]["true"]
    if outcome["status"] != "sat":
        pytest.skip("covered by the infeasibility test")
    assert _call(EARLY_RETURN_SRC, "f", a=outcome["inputs"]["a"]) == 2


# --------------------------------------------------------------------------- #
# Python for-each contributes no guard, so a branch in the loop body is solved
# as if the loop always ran. Java's enhanced-for already blocks correctly.
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(strict=True, reason="Python for_statement yields no guard for its body")
def test_branch_inside_a_python_loop_reports_the_iterable_requirement(tmp_path):
    src = _write(tmp_path, "loop.py", LOOP_SRC)
    analysis = symbolic.analyze_file_function(src, "h")
    outcome = _branch(analysis, "if_statement@3")["outcomes"]["true"]
    assert not outcome["solvable"]
    assert any("loop" in b or "empty" in b for b in outcome["blockers"])


@pytest.mark.xfail(strict=True, reason="Python for_statement yields no guard for its body")
def test_python_loop_inputs_do_not_claim_an_empty_iterable(tmp_path):
    """An empty list never enters the body, so those inputs miss the branch."""
    src = _write(tmp_path, "loop.py", LOOP_SRC)
    result = symbolic.solve_all_branches(src, "h")
    outcome = _branch(result, "if_statement@3")["outcomes"]["true"]
    if outcome["status"] != "sat":
        pytest.skip("blocked instead, which is the other acceptable fix")
    items = outcome["inputs"]["items"]
    assert items.get("entries"), "solver claimed an empty list reaches the loop body"


def test_java_enhanced_for_blocks_the_nested_branch(tmp_path):
    """The behaviour Python's for-each is missing — locked in so it cannot regress."""
    src = _write(tmp_path, "E.java", """\
class E {
    int f(boolean b) {
        for (String x : new String[]{}) { if (b) { return 1; } }
        return 0;
    }
}
""")
    analysis = symbolic.analyze_file_function(src, "f")
    outcome = _branch(analysis, "if_statement@3")["outcomes"]["true"]
    assert not outcome["solvable"]
    assert any("for-each" in b for b in outcome["blockers"])


# --------------------------------------------------------------------------- #
# Python truthiness on a non-boolean reaches Z3 and raises instead of being
# reported as an untranslatable guard.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("branch_id", ["if_statement@2", "if_statement@4"])
@pytest.mark.xfail(strict=True, reason="truthiness is emitted as Not(s)/s and blows up inside Z3")
def test_truthiness_of_a_string_is_reported_not_raised(tmp_path, branch_id):
    src = _write(tmp_path, "truthy.py", TRUTHY_SRC)
    result = symbolic.solve_all_branches(src, "g")
    outcome = _branch(result, branch_id)["outcomes"]["true"]
    assert outcome["status"] == "skipped", f"got {outcome['status']}: {outcome.get('error')}"


# --------------------------------------------------------------------------- #
# tree-sitter-cpp folds the sign into the literal, so the suffix stripper eats
# trailing hex digits. Java and Python split the sign off and are unaffected.
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(strict=True, reason="_clean_literal strips the F from -0x1F")
def test_negative_hex_literal_keeps_its_value(tmp_path):
    src = _write(tmp_path, "hex.cpp", HEX_SRC)
    result = symbolic.solve_all_branches(src, "f")
    outcome = _branch(result, "if_statement@2")["outcomes"]["true"]
    assert outcome["status"] == "sat"
    assert outcome["inputs"]["k"]["signed"] == -31


@pytest.mark.xfail(strict=True, reason="_clean_literal turns -0xFF into the invalid literal -0x")
def test_negative_hex_literal_of_all_f_digits_still_parses(tmp_path):
    src = _write(tmp_path, "hex2.cpp", "int f(int k) {\n    if (k == -0xFF) { return 1; }\n    return 0;\n}\n")
    result = symbolic.solve_all_branches(src, "f")
    outcome = _branch(result, "if_statement@2")["outcomes"]["true"]
    assert outcome["status"] == "sat"
    assert outcome["inputs"]["k"]["signed"] == -255


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("0x1F", "0x1F"),
        ("0xFF", "0xFF"),
        ("31u", "31"),
        ("3L", "3"),
        ("1.5f", "1.5"),
        ("0xFFul", "0xFF"),
        ("0b1011", "0b1011"),
        ("12", "12"),
    ],
)
def test_positive_literal_suffixes_are_stripped_correctly(literal, expected):
    """Unsigned literals already work; locked in so a fix cannot break them."""
    assert symbolic._clean_literal(literal) == expected


@pytest.mark.parametrize("lang_file", ["neg.java", "neg.py"])
def test_java_and_python_negative_hex_are_unaffected(tmp_path, lang_file):
    """Both grammars split the sign into a unary node, so the literal stays whole."""
    if lang_file.endswith(".java"):
        text = "class N {\n    int f(int k) {\n        if (k == -0x1F) { return 1; }\n        return 0;\n    }\n}\n"
        branch_id, key = "if_statement@3", "signed"
    else:
        text = "def f(k: int) -> int:\n    if k == -0x1F:\n        return 1\n    return 0\n"
        branch_id, key = "if_statement@2", None
    result = symbolic.solve_all_branches(_write(tmp_path, lang_file, text), "f")
    inputs = _branch(result, branch_id)["outcomes"]["true"]["inputs"]
    value = inputs["k"][key] if key else inputs["k"]
    assert value == -31


# --------------------------------------------------------------------------- #
# Branch entries without outcomes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("noout.py", "def f(xs: list[int]) -> int:\n    for x in xs:\n        pass\n    return 0\n"),
        ("noout.cpp", "int f(int k) {\n    for (;;) { break; }\n    return 0;\n}\n"),
    ],
)
@pytest.mark.xfail(strict=True, reason="loops without a condition are emitted as empty branches")
def test_every_reported_branch_carries_a_condition(tmp_path, name, text):
    """A branch with no outcomes is noise, and consumers KeyError on `condition`."""
    analysis = symbolic.analyze_file_function(_write(tmp_path, name, text), "f")
    for branch in analysis["branches"]:
        assert branch["outcomes"], f"{branch['id']} has no outcomes"
        assert "condition" in branch
