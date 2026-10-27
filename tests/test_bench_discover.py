"""Tests for bench/discover.py — which files and functions get benchmarked."""

from pathlib import Path

import pytest

from bench.discover import (
    _collect_cmake_variables,
    _expand,
    _is_excluded_path,
    discover,
    focal_methods,
    production_sources,
    sources_by_heuristic,
    sources_from_cmake,
)


@pytest.fixture
def cpp_project(tmp_path: Path) -> Path:
    """A project shaped like re2: library sources, its own tests, build junk."""
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "engine.cc").write_text(
        """
int classify(int n) {
  if (n < 0) return -1;
  if (n == 0 && n != 1) return 0;
  return 1;
}

void noop() {}
""".lstrip()
    )
    (tmp_path / "core" / "helper.cc").write_text("int helper() { return 7; }\n")
    (tmp_path / "core" / "testing").mkdir()
    (tmp_path / "core" / "testing" / "harness.cc").write_text("void harness() {}\n")
    (tmp_path / "core" / "engine_test.cc").write_text("void engine_test() {}\n")
    (tmp_path / "build_eval").mkdir()
    (tmp_path / "build_eval" / "stale.cc").write_text("void stale() {}\n")
    (tmp_path / "CMakeLists.txt").write_text(
        """
cmake_minimum_required(VERSION 3.10)
project(demo CXX)
set(CORE_SOURCES
    core/engine.cc
    core/helper.cc
    )
add_library(demo STATIC ${CORE_SOURCES})
add_library(testing STATIC core/testing/harness.cc)
""".lstrip()
    )
    return tmp_path


# ---------------------------------------------------------------------------
# CMake parsing


def test_collect_cmake_variables_drops_set_modifiers():
    text = 'set(SRCS a.cc b.cc)\nset(MODE "Debug" CACHE STRING FORCE)'
    variables = _collect_cmake_variables(text)
    assert variables["SRCS"] == ["a.cc", "b.cc"]
    assert "CACHE" not in variables["MODE"] and "FORCE" not in variables["MODE"]


def test_expand_substitutes_variable_references():
    variables = {"INNER": ["x.cc"], "OUTER": ["${INNER}", "y.cc"]}
    assert _expand(["${OUTER}"], variables) == ["x.cc", "y.cc"]


def test_expand_leaves_unknown_variables_alone():
    assert _expand(["${MISSING}"], {}) == ["${MISSING}"]


def test_sources_from_cmake_uses_the_library_source_list(cpp_project: Path):
    names = [p.name for p in sources_from_cmake(cpp_project)]
    assert names == ["engine.cc", "helper.cc"]


def test_sources_from_cmake_skips_test_targets(cpp_project: Path):
    """`add_library(testing ...)` is the project's own test scaffolding."""
    assert "harness.cc" not in [p.name for p in sources_from_cmake(cpp_project)]


def test_production_sources_prefers_cmake(cpp_project: Path):
    sources, strategy = production_sources(cpp_project)
    assert strategy == "cmake"
    assert [p.name for p in sources] == ["engine.cc", "helper.cc"]


def test_production_sources_falls_back_to_heuristics(tmp_path: Path):
    (tmp_path / "lib.cc").write_text("int f() { return 1; }\n")
    sources, strategy = production_sources(tmp_path)
    assert strategy == "heuristic"
    assert [p.name for p in sources] == ["lib.cc"]


# ---------------------------------------------------------------------------
# Path exclusion


@pytest.mark.parametrize(
    "relative",
    ["test", "tests", "testing", "third_party", "build", "cmake-build-debug", ".git"],
)
def test_is_excluded_path_rejects_non_production_directories(tmp_path: Path, relative: str):
    assert _is_excluded_path(tmp_path / relative, tmp_path) is True


def test_is_excluded_path_accepts_ordinary_source_directories(tmp_path: Path):
    assert _is_excluded_path(tmp_path / "core" / "engine", tmp_path) is False


def test_heuristic_skips_test_named_files(cpp_project: Path):
    names = [p.name for p in sources_by_heuristic(cpp_project)]
    assert "engine_test.cc" not in names
    assert "stale.cc" not in names  # build_eval/ is build output


# ---------------------------------------------------------------------------
# Focal methods and complexity


def test_focal_methods_reports_position_and_complexity(cpp_project: Path):
    methods = focal_methods(cpp_project, cpp_project / "core" / "engine.cc")
    by_name = {m.name: m for m in methods}
    assert set(by_name) == {"classify", "noop"}
    # two ifs plus one && => three decision points, plus one
    assert by_name["classify"].complexity == 4
    assert by_name["classify"].branch_count == 2
    assert by_name["noop"].complexity == 1
    assert by_name["classify"].file == "core/engine.cc"
    assert by_name["classify"].start_line == 1


def test_focal_methods_honours_min_lines(cpp_project: Path):
    methods = focal_methods(cpp_project, cpp_project / "core" / "engine.cc", min_lines=3)
    assert [m.name for m in methods] == ["classify"]


def test_focal_method_key_is_unique_per_definition(cpp_project: Path):
    methods = focal_methods(cpp_project, cpp_project / "core" / "engine.cc")
    assert len({m.key for m in methods}) == len(methods)


def test_discover_walks_every_production_source(cpp_project: Path):
    methods, strategy = discover(cpp_project)
    assert strategy == "cmake"
    assert {m.name for m in methods} == {"classify", "noop", "helper"}


def test_discover_ignores_declarations_without_a_body(tmp_path: Path):
    (tmp_path / "decl.cc").write_text("int declared_only(int x);\nint defined() { return 1; }\n")
    methods, _ = discover(tmp_path)
    assert [m.name for m in methods] == ["defined"]
