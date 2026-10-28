"""Tests for bench/metrics.py — turning tool output into table columns."""

import json

import pytest

from bench import metrics

TEST_FILE = """\
// A generated test file.
#include <gtest/gtest.h>
#include "lib.h"

/* block
   comment */
TEST(SuiteA, First) {
  int x = compute(1);
  if (x > 0 && x < 10) {
    EXPECT_EQ(x, 5);
  }
}

TEST(SuiteA, Second) {
  EXPECT_TRUE(compute(2));
}

TEST_F(FixtureB, Third) {
  EXPECT_FALSE(false);
}
"""


# ---------------------------------------------------------------------------
# Test-file statistics


def test_count_gtest_cases_finds_every_macro_form():
    assert metrics.count_gtest_cases(TEST_FILE) == [
        "SuiteA.First", "SuiteA.Second", "FixtureB.Third",
    ]


def test_count_gtest_cases_is_empty_for_a_file_without_cases():
    assert metrics.count_gtest_cases("int main() { return 0; }") == []


def test_test_loc_skips_blank_and_comment_lines():
    # 2 includes + 12 lines of case bodies; the // and /* */ comments do not count.
    assert metrics.test_loc(TEST_FILE) == 14


def test_test_loc_handles_inline_block_comments():
    assert metrics.test_loc("/* only a comment */\nint x = 1;\n") == 1


def test_test_complexity_counts_decisions_in_case_bodies():
    # First: 1 + if + && = 3; Second: 1; Third: 1
    assert metrics.test_complexity(TEST_FILE) == 5


@pytest.mark.parametrize(
    "text, expected",
    [
        ('#include <gmock/gmock.h>', True),
        ("MOCK_METHOD(int, Foo, ());", True),
        ("::testing::NiceMock<Bar> bar;", True),
        ("#include <gtest/gtest.h>", False),
    ],
)
def test_uses_mock(text: str, expected: bool):
    assert metrics.uses_mock(text) is expected


# ---------------------------------------------------------------------------
# Case extraction and pruning


def test_extract_case_source_balances_braces():
    block = metrics.extract_case_source(TEST_FILE, "SuiteA.First")
    assert block.startswith("TEST(SuiteA, First)")
    assert block.rstrip().endswith("}")
    assert block.count("{") == block.count("}")


def test_extract_case_source_returns_none_for_unknown_case():
    assert metrics.extract_case_source(TEST_FILE, "Nope.Missing") is None


def test_strip_cases_removes_only_the_named_cases():
    pruned = metrics.strip_cases(TEST_FILE, {"SuiteA.First"})
    assert metrics.count_gtest_cases(pruned) == ["SuiteA.Second", "FixtureB.Third"]
    assert "#include <gtest/gtest.h>" in pruned  # includes survive


def test_strip_cases_is_a_no_op_without_targets():
    assert metrics.strip_cases(TEST_FILE, set()) == TEST_FILE


def test_cases_covering_lines_maps_a_diagnostic_back_to_its_case():
    # Line 9 sits inside SuiteA.First (the `if` line).
    assert metrics.cases_covering_lines(TEST_FILE, {9}) == {"SuiteA.First"}


def test_cases_covering_lines_is_empty_without_line_numbers():
    assert metrics.cases_covering_lines(TEST_FILE, set()) == set()


# ---------------------------------------------------------------------------
# Error classification


@pytest.mark.parametrize(
    "output, expected",
    [
        ("t.cpp:3:10: fatal error: lib.h: No such file or directory", "missing_include"),
        ("t.cpp:5:3: error: 'foo' was not declared in this scope", "undeclared_identifier"),
        ("t.cpp:5:3: error: 'class Bar' has no member named 'baz'", "no_member"),
        ("t.cpp:5:3: error: 'Bar::x' is private within this context", "private_access"),
        ("t.cpp:5:3: error: no matching function for call to 'f()'", "no_matching_function"),
        ("t.cpp:5:3: error: cannot convert 'int' to 'char*'", "type_mismatch"),
        ("/usr/bin/ld: undefined reference to `foo()'", "linker_error"),
        ("t.cpp:5:3: error: expected ';' before '}' token", "syntax_error"),
    ],
)
def test_classify_error_recognises_compiler_diagnostics(output: str, expected: str):
    category, _ = metrics.classify_error(output, stage="compile")
    assert category == expected


def test_classify_error_returns_the_first_diagnostic_line():
    output = "some noise\nt.cpp:12:5: error: no matching function for call\nmore noise"
    _, first = metrics.classify_error(output, stage="compile")
    assert first == "t.cpp:12:5: error: no matching function for call"


def test_classify_error_handles_runtime_failures():
    category, _ = metrics.classify_error(
        "t.cpp:9: Failure\nExpected equality of these values:", stage="runtime"
    )
    assert category == "assertion_failure"


def test_classify_error_on_empty_output():
    assert metrics.classify_error("", stage="compile") == ("unknown", "")


def test_failing_case_lines_only_counts_the_test_file():
    output = (
        "test_x.cpp:12:5: error: boom\n"
        "/usr/include/other.h:99:1: error: unrelated\n"
        "test_x.cpp:20:1: error: also boom\n"
    )
    assert metrics.failing_case_lines(output, "test_x.cpp") == {12, 20}


# ---------------------------------------------------------------------------
# GoogleTest JSON and coverage


def test_parse_gtest_json_counts_per_case_results():
    report = json.dumps({
        "testsuites": [{
            "name": "SuiteA",
            "testsuite": [
                {"name": "First", "result": "COMPLETED"},
                {"name": "Second", "result": "COMPLETED", "failures": [{"failure": "x"}]},
                {"name": "Third", "result": "skipped"},
            ],
        }]
    })
    parsed = metrics.parse_gtest_json(report)
    assert parsed["passed"] == 1
    assert parsed["failed"] == 1
    assert parsed["cases"]["SuiteA.Third"] == "skipped"


def test_parse_gtest_json_survives_garbage():
    assert metrics.parse_gtest_json("not json")["total"] == 0


COVERAGE_REPORT = {
    "files": [
        {
            "file": "core/engine.cc",
            "lines": [
                {"line_number": 9, "count": 0, "branches": []},
                {"line_number": 10, "count": 3,
                 "branches": [{"count": 1}, {"count": 0}]},
                {"line_number": 11, "count": 3, "branches": []},
                {"line_number": 40, "count": 0, "branches": []},
            ],
        },
        {"file": "core/other.cc", "lines": [{"line_number": 10, "count": 99}]},
    ]
}


def test_focal_coverage_restricts_to_the_method_line_range():
    result = metrics.focal_coverage(COVERAGE_REPORT, "core/engine.cc", 10, 11)
    assert result["lines_found"] == 2
    assert result["lines_hit"] == 2
    assert result["line_coverage"] == 100.0
    assert result["branch_coverage"] == 50.0
    assert result["touched_focal"] is True


def test_focal_coverage_reports_an_untouched_method():
    result = metrics.focal_coverage(COVERAGE_REPORT, "core/engine.cc", 40, 40)
    assert result["line_coverage"] == 0.0
    assert result["touched_focal"] is False


def test_focal_coverage_matches_on_a_path_suffix():
    """gcovr may report absolute paths; the focal file is relative."""
    report = {"files": [{"file": "/build/src/core/engine.cc",
                         "lines": [{"line_number": 10, "count": 1}]}]}
    assert metrics.focal_coverage(report, "core/engine.cc", 10, 10)["line_coverage"] == 100.0


def test_focal_coverage_of_an_unknown_file_is_zero_not_an_error():
    result = metrics.focal_coverage(COVERAGE_REPORT, "core/missing.cc", 1, 10)
    assert result["lines_found"] == 0
    assert result["line_coverage"] == 0.0
