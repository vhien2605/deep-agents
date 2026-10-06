"""Tests for bench/report.py — aggregating per-method rows into summaries."""

import pytest

from bench.report import (
    Aggregate,
    accumulate,
    by_complexity,
    by_file,
    error_breakdown,
    render_markdown,
)
from bench.schema import Row


def make_row(**kwargs) -> Row:
    """A row with sensible defaults; override only what a test cares about."""
    defaults = dict(
        project="demo", file="core/engine.cc", method="classify", line=10,
        model="test-model", n_cases=4, n_cases_compiled=4, n_cases_passed=4,
        csr=1.0, epr=1.0, line_coverage=100.0, branch_coverage=80.0,
        touched_focal=True, total_tokens=1000, duration_s=10.0, focal_cc=3,
        test_loc=40, test_cc=5,
    )
    defaults.update(kwargs)
    row = Row(**defaults)
    row.detail.setdefault("coverage", {"branches_found": 4, "branches_hit": 3})
    return row


# ---------------------------------------------------------------------------
# Rate arithmetic


def test_rates_weight_by_case_count_not_by_method():
    """A method with 20 cases must outweigh one with 2.

    Averaging per-method percentages would treat them equally and report a
    rate that no set of cases actually achieved.
    """
    rows = [
        make_row(n_cases=20, n_cases_compiled=10, n_cases_passed=10),
        make_row(n_cases=2, n_cases_compiled=2, n_cases_passed=2),
    ]
    agg = accumulate(rows)
    assert agg.cases == 22
    assert agg.csr == pytest.approx(100.0 * 12 / 22)
    # Naive mean of per-method CSR would be (50 + 100) / 2 = 75%.
    assert agg.csr != pytest.approx(75.0)


def test_csr_and_epr_are_zero_without_cases():
    agg = accumulate([make_row(n_cases=0, n_cases_compiled=0, n_cases_passed=0)])
    assert agg.csr == 0.0
    assert agg.epr == 0.0


def test_epr_is_relative_to_compiled_not_written():
    agg = accumulate([make_row(n_cases=10, n_cases_compiled=5, n_cases_passed=4)])
    assert agg.csr == 50.0
    assert agg.epr == 80.0


def test_empty_aggregate_has_no_division_errors():
    agg = Aggregate()
    assert (agg.csr, agg.epr, agg.line_coverage, agg.touched_pct) == (0.0, 0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# Coverage averaging


def test_coverage_excludes_methods_whose_tests_never_ran():
    """A method with no passing case has no coverage to average in."""
    rows = [make_row(line_coverage=100.0), make_row(n_cases_passed=0, line_coverage=0.0)]
    agg = accumulate(rows)
    assert agg.line_cov_n == 1
    assert agg.line_coverage == 100.0


def test_branch_coverage_excludes_branchless_methods():
    """A branchless method scores 0% by arithmetic, not by any test failure."""
    branchless = make_row(branch_coverage=0.0)
    branchless.detail["coverage"] = {"branches_found": 0, "branches_hit": 0}
    agg = accumulate([make_row(branch_coverage=90.0), branchless])
    assert agg.branch_cov_n == 1
    assert agg.branch_coverage == 90.0


def test_branch_coverage_reports_not_available_when_nothing_has_branches():
    branchless = make_row(branch_coverage=0.0)
    branchless.detail["coverage"] = {"branches_found": 0}
    agg = accumulate([branchless])
    assert agg.branch_cov_n == 0
    assert "n/a" in render_markdown([branchless])


def test_touched_focal_is_a_share_of_all_methods():
    agg = accumulate([make_row(touched_focal=True), make_row(touched_focal=False)])
    assert agg.touched_pct == 50.0


# ---------------------------------------------------------------------------
# Grouping


def test_by_complexity_buckets_and_skips_empty_bands():
    rows = [make_row(focal_cc=1), make_row(focal_cc=3), make_row(focal_cc=20)]
    labels = [a.label for a in by_complexity(rows)]
    assert labels == ["1 (straight-line)", "2-3", "16+"]


def test_by_complexity_counts_each_method_once():
    rows = [make_row(focal_cc=cc) for cc in (1, 2, 5, 9, 30)]
    assert sum(a.methods for a in by_complexity(rows)) == len(rows)


def test_by_file_orders_by_method_count():
    rows = [make_row(file="a.cc"), make_row(file="b.cc"), make_row(file="b.cc")]
    assert [a.label for a in by_file(rows)] == ["b.cc", "a.cc"]


def test_by_file_honours_the_top_limit():
    rows = [make_row(file=f"f{i}.cc") for i in range(20)]
    assert len(by_file(rows, top=5)) == 5


# ---------------------------------------------------------------------------
# Error breakdown and rendering


def test_error_breakdown_labels_successes_as_none():
    rows = [make_row(error_category=""), make_row(error_category="no_member")]
    breakdown = dict((name, count) for name, count, _ in error_breakdown(rows))
    assert breakdown == {"none": 1, "no_member": 1}


def test_error_breakdown_is_ordered_by_frequency():
    rows = [make_row(error_category="a")] * 1 + [make_row(error_category="b")] * 3
    assert error_breakdown(rows)[0][0] == "b"


def test_render_markdown_contains_every_section():
    text = render_markdown([make_row(error_category="no_member")], {"temperature": 0.0})
    for heading in ("# Benchmark summary", "## Overall", "## By focal complexity",
                    "## By source file", "## First failure by category"):
        assert heading in text


def test_render_markdown_warns_when_the_endpoint_served_several_models():
    """A sweep spanning two models cannot be reported as one experiment."""
    rows = [make_row(model="model-a"), make_row(model="model-b")]
    text = render_markdown(rows, {"model_requested": "model-a"})
    assert "more than one model" in text


def test_render_markdown_stays_quiet_for_a_single_model():
    text = render_markdown([make_row(model="only-one")], {"model_requested": "only-one"})
    assert "more than one model" not in text


def test_render_markdown_without_config_omits_the_configuration_section():
    assert "## Configuration" not in render_markdown([make_row()])
