"""Tests for src/tools/coverage.py — lcov/coverage.py/JaCoCo report parsing."""

import json
from pathlib import Path

import pytest

from src.tools.coverage import (
    _demangle,
    make_coverage_tools,
    parse_lcov_tracefile,
)


# ---------------------------------------------------------------------------
# _demangle
# ---------------------------------------------------------------------------


def test_demangle_no_mangled_names():
    """Plain names (no _Z prefix) are returned unchanged."""
    names = ["foo", "bar", "baz"]
    assert _demangle(names) == names


def test_demangle_empty_list():
    assert _demangle([]) == []


def test_demangle_mixed_names():
    """Only mangled names (starting with _Z) are demangled; others pass through."""
    names = ["plain", "_Z3foov"]
    result = _demangle(names)
    assert len(result) == 2
    assert result[0] == "plain"
    # _Z3foov demangles to "foo()" if c++filt is available, else stays as-is
    assert result[1] in ("foo()", "_Z3foov")


def test_demangle_cxxfilt_not_available(monkeypatch):
    """When c++filt is not on PATH, names are returned as-is."""
    import src.tools.coverage as cov_module

    monkeypatch.setattr(cov_module.shutil, "which", lambda cmd: None)
    names = ["_Z3foov", "plain"]
    result = _demangle(names)
    assert result == names


def test_demangle_subprocess_fails(monkeypatch):
    """When c++filt raises a SubprocessError, names are returned as-is."""
    import src.tools.coverage as cov_module

    monkeypatch.setattr(cov_module.shutil, "which", lambda cmd: "/usr/bin/c++filt")

    def fake_run(*args, **kwargs):
        raise cov_module.subprocess.SubprocessError("fail")

    monkeypatch.setattr(cov_module.subprocess, "run", fake_run)
    names = ["_Z3foov"]
    assert _demangle(names) == names


def test_demangle_oserror(monkeypatch):
    """When c++filt raises OSError, names are returned as-is."""
    import src.tools.coverage as cov_module

    monkeypatch.setattr(cov_module.shutil, "which", lambda cmd: "/usr/bin/c++filt")

    def fake_run(*args, **kwargs):
        raise OSError("fail")

    monkeypatch.setattr(cov_module.subprocess, "run", fake_run)
    assert _demangle(["_Z3foov"]) == ["_Z3foov"]


# ---------------------------------------------------------------------------
# parse_lcov_tracefile
# ---------------------------------------------------------------------------


SIMPLE_TRACE = """\
SF:src/main.cpp
DA:1,1
DA:2,1
DA:3,0
FNDA:1,add
FNDA:0,unused_func
end_of_record
SF:src/util.cpp
DA:1,0
DA:2,1
end_of_record
"""


def test_parse_lcov_basic():
    result = parse_lcov_tracefile(SIMPLE_TRACE)
    # File1: 2/3 hit, File2: 1/2 hit → 3/5 total
    assert result["total_line_coverage_pct"] == round(100.0 * 3 / 5, 2)
    assert len(result["files"]) == 2


def test_parse_lcov_file_structure():
    result = parse_lcov_tracefile(SIMPLE_TRACE)
    f = result["files"][0]
    assert f["file"] == "src/main.cpp"
    assert f["lines_found"] == 3
    assert f["lines_hit"] == 2
    assert f["line_coverage_pct"] == round(100.0 * 2 / 3, 2)
    assert 3 in f["uncovered_lines"]


def test_parse_lcov_uncovered_functions():
    result = parse_lcov_tracefile(SIMPLE_TRACE)
    f = result["files"][0]
    assert "unused_func" in f["uncovered_functions"]
    assert "add" not in f["uncovered_functions"]


def test_parse_lcov_fna_format():
    """lcov 2.x FNA:<index>,<hits>,<name> format."""
    trace = """\
SF:src/test.cpp
FNA:0,1,used_func
FNA:1,0,never_called
DA:1,1
end_of_record
"""
    result = parse_lcov_tracefile(trace)
    f = result["files"][0]
    assert "never_called" in f["uncovered_functions"]
    assert "used_func" not in f["uncovered_functions"]


def test_parse_lcov_empty_text():
    result = parse_lcov_tracefile("")
    assert result["total_line_coverage_pct"] == 0.0
    assert result["files"] == []


def test_parse_lcov_no_lines_in_file():
    """A record with no DA lines has 0.0% coverage."""
    trace = """\
SF:src/empty.cpp
end_of_record
"""
    result = parse_lcov_tracefile(trace)
    f = result["files"][0]
    assert f["lines_found"] == 0
    assert f["lines_hit"] == 0
    assert f["line_coverage_pct"] == 0.0


def test_parse_lcov_all_lines_covered():
    trace = """\
SF:src/perfect.cpp
DA:1,5
DA:2,3
end_of_record
"""
    result = parse_lcov_tracefile(trace)
    assert result["total_line_coverage_pct"] == 100.0
    assert result["files"][0]["uncovered_lines"] == []


def test_parse_lcov_all_lines_uncovered():
    trace = """\
SF:src/bad.cpp
DA:1,0
DA:2,0
end_of_record
"""
    result = parse_lcov_tracefile(trace)
    assert result["total_line_coverage_pct"] == 0.0
    assert result["files"][0]["uncovered_lines"] == [1, 2]


def test_parse_lcov_max_uncovered_cap():
    """When uncovered lines exceed max_uncovered_per_file, the list is capped."""
    lines = [f"DA:{i},0" for i in range(1, 60)]
    trace = "SF:src/big.cpp\n" + "\n".join(lines) + "\nend_of_record\n"
    result = parse_lcov_tracefile(trace, max_uncovered_per_file=10)
    assert len(result["files"][0]["uncovered_lines"]) == 10


def test_parse_lcov_line_before_sf_is_ignored():
    """Lines before the first SF: are skipped."""
    trace = "DA:1,1\nrandom garbage\nSF:src/a.cpp\nDA:1,1\nend_of_record\n"
    result = parse_lcov_tracefile(trace)
    assert len(result["files"]) == 1
    assert result["files"][0]["lines_found"] == 1


def test_parse_lcov_strips_whitespace():
    trace = "  SF:src/a.cpp  \n  DA:1,1  \n  end_of_record  \n"
    result = parse_lcov_tracefile(trace)
    assert len(result["files"]) == 1
    assert result["files"][0]["file"] == "src/a.cpp"


def test_parse_lcov_da_with_multiple_fields():
    """DA lines may have more than 2 fields (checksum); only first 2 are used."""
    trace = """\
SF:src/a.cpp
DA:1,1,checksum
DA:2,0,checksum
end_of_record
"""
    result = parse_lcov_tracefile(trace)
    assert result["files"][0]["lines_found"] == 2
    assert result["files"][0]["lines_hit"] == 1


def test_parse_lcov_total_aggregation():
    trace = """\
SF:src/a.cpp
DA:1,1
DA:2,0
end_of_record
SF:src/b.cpp
DA:1,1
end_of_record
"""
    result = parse_lcov_tracefile(trace)
    assert result["total_line_coverage_pct"] == round(100.0 * 2 / 3, 2)


# ---------------------------------------------------------------------------
# make_coverage_tools — coverage_report
# ---------------------------------------------------------------------------


@pytest.fixture
def coverage_tools(tmp_path):
    return make_coverage_tools(tmp_path), tmp_path


def test_coverage_report_basic(coverage_tools):
    tools, root = coverage_tools
    report_fn = tools[2]  # coverage_report
    (root / "coverage.info").write_text(SIMPLE_TRACE)
    result = report_fn.invoke({"tracefile": "coverage.info", "source_filter": ""})
    data = json.loads(result)
    assert data["total_line_coverage_pct"] == round(100.0 * 3 / 5, 2)
    assert len(data["files"]) == 2


def test_coverage_report_with_source_filter(coverage_tools):
    tools, root = coverage_tools
    report_fn = tools[2]
    (root / "coverage.info").write_text(SIMPLE_TRACE)
    result = report_fn.invoke({"tracefile": "coverage.info", "source_filter": "main"})
    data = json.loads(result)
    assert len(data["files"]) == 1
    assert "main.cpp" in data["files"][0]["file"]
    # Recomputed percentage for filtered files
    assert data["total_line_coverage_pct"] == round(100.0 * 2 / 3, 2)


def test_coverage_report_not_found(coverage_tools):
    tools, root = coverage_tools
    report_fn = tools[2]
    result = report_fn.invoke({"tracefile": "missing.info", "source_filter": ""})
    assert "ERROR" in result
    assert "tracefile not found" in result


def test_coverage_report_absolute_path(coverage_tools):
    tools, root = coverage_tools
    report_fn = tools[2]
    (root / "coverage.info").write_text(SIMPLE_TRACE)
    result = report_fn.invoke({
        "tracefile": str(root / "coverage.info"),
        "source_filter": "",
    })
    data = json.loads(result)
    assert len(data["files"]) == 2


def test_coverage_report_filter_no_match(coverage_tools):
    tools, root = coverage_tools
    report_fn = tools[2]
    (root / "coverage.info").write_text(SIMPLE_TRACE)
    result = report_fn.invoke({"tracefile": "coverage.info", "source_filter": "nomatch"})
    data = json.loads(result)
    assert data["files"] == []
    assert data["total_line_coverage_pct"] == 0.0


