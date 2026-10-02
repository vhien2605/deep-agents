"""Aggregate a sweep's per-method rows into the summary tables of a report.

`results.csv` is the method-level table; this module answers the questions a
write-up asks of it: what are the overall rates, how do they move with focal
complexity, and what actually went wrong when a method failed.

Rates are computed over case totals, not as a mean of per-method rates: a
method contributing 20 cases should weigh more than one contributing 2, and
averaging percentages would silently equalise them.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from bench.schema import Row

# Complexity bands. Chosen so each holds a meaningful share of a real project
# rather than being evenly spaced: most methods are simple, and the interesting
# behaviour is in the tail.
CC_BUCKETS = [(1, 1, "1 (straight-line)"), (2, 3, "2-3"), (4, 7, "4-7"),
              (8, 15, "8-15"), (16, 10**6, "16+")]


@dataclass
class Aggregate:
    """Totals over a set of rows."""

    label: str = ""
    methods: int = 0
    methods_with_output: int = 0
    cases: int = 0
    cases_compiled: int = 0
    cases_passed: int = 0
    touched: int = 0
    line_cov_sum: float = 0.0
    line_cov_n: int = 0
    branch_cov_sum: float = 0.0
    branch_cov_n: int = 0
    tokens: int = 0
    duration_s: float = 0.0
    fix_rounds: int = 0
    mock_files: int = 0
    test_loc: int = 0

    @property
    def csr(self) -> float:
        return 100.0 * self.cases_compiled / self.cases if self.cases else 0.0

    @property
    def epr(self) -> float:
        return 100.0 * self.cases_passed / self.cases_compiled if self.cases_compiled else 0.0

    @property
    def line_coverage(self) -> float:
        return self.line_cov_sum / self.line_cov_n if self.line_cov_n else 0.0

    @property
    def branch_coverage(self) -> float:
        return self.branch_cov_sum / self.branch_cov_n if self.branch_cov_n else 0.0

    @property
    def touched_pct(self) -> float:
        return 100.0 * self.touched / self.methods if self.methods else 0.0


def effective_compiled(row: Row) -> int:
    """Cases the binary actually held, when the run recorded them.

    Companion to `effective_category`: a preprocessor-excluded case must
    leave the EPR denominator as well as the failure column.
    """
    cases = row.detail.get("cases") or {}
    if cases and 0 < len(cases) < row.n_cases_compiled:
        return len(cases)
    return row.n_cases_compiled

def effective_category(row: Row) -> str:
    """The row's outcome after one correction the harness used to miss.

    Rows scored before the harness learned to trust the binary's case count
    carry `unknown` for a run in which every case passed but one TEST macro
    was wrapped in a preprocessor guard the build did not satisfy. The
    per-case results and exit code are stored, so the correction is applied
    here rather than by re-running fifteen methods across six projects.
    """
    category = row.error_category or "pass"
    if category != "unknown":
        return category
    cases = row.detail.get("cases") or {}
    if (cases and row.detail.get("run_exit_code") == 0
            and not any(v == "failed" for v in cases.values())):
        return "pass"
    return category

def _has_branches(row: Row) -> bool:
    """Whether the focal method has any branch to cover.

    A branchless method scores 0 % branch coverage by arithmetic, not by any
    failure of the tests, so it must not enter the branch average.
    """
    return (row.detail.get("coverage") or {}).get("branches_found", 0) > 0


def accumulate(rows: list[Row], label: str = "all") -> Aggregate:
    agg = Aggregate(label=label)
    for row in rows:
        agg.methods += 1
        agg.tokens += row.total_tokens
        agg.duration_s += row.duration_s
        agg.fix_rounds += row.fix_phase
        agg.test_loc += row.test_loc
        if row.n_cases:
            agg.methods_with_output += 1
        agg.cases += row.n_cases
        agg.cases_compiled += row.n_cases_compiled
        agg.cases_passed += row.n_cases_passed
        if row.touched_focal:
            agg.touched += 1
        if row.uses_mock:
            agg.mock_files += 1
        # Coverage is only meaningful for a method whose tests ran at all.
        if row.n_cases_passed > 0:
            agg.line_cov_sum += row.line_coverage
            agg.line_cov_n += 1
            if _has_branches(row):
                agg.branch_cov_sum += row.branch_coverage
                agg.branch_cov_n += 1
    return agg


SOURCE_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx", ".C"})


def compiled_sources(compile_db: Path, project_dir_name: str) -> set[str]:
    """Project-relative paths of the sources the project's own build compiles.

    Read from the `compile_commands.json` CMake writes for the instrumented
    build, so this is what the build system actually did on this platform,
    not what a directory listing suggests it might do.
    """
    if not compile_db.is_file():
        return set()
    marker = project_dir_name + "/"
    compiled: set[str] = set()
    for entry in json.loads(compile_db.read_text(encoding="utf-8")):
        path = Path(entry.get("file", "")).as_posix()
        index = path.find(marker)
        if index >= 0:
            compiled.add(path[index + len(marker):])
    return compiled


def is_platform_excluded(row: Row, compiled: set[str]) -> bool:
    """Whether a row failed because its source cannot be built here at all.

    Two conditions together, because neither alone is sound. Structurally,
    the focal source must be one the project's build never compiles -- mlx's
    Metal and CUDA back ends, leveldb's `env_windows.cc`. That test on its
    own over-reaches: leveldb's `util/testutil.cc` and yaml-cpp's command
    line tools are absent from the library build yet compile perfectly, and
    three of them produced passing tests. So the row must also have failed
    with a header the toolchain could not find, which is what a missing
    platform SDK looks like from inside the compiler.

    A method excluded this way is not evidence about the generator. It is
    also not evidence the source study would have shared: Metal sources
    build on macOS, where that study ran, so 39 of mlx's methods were
    testable there and are not here. Rows are therefore labelled, never
    silently dropped.
    """
    if Path(row.file).suffix not in SOURCE_SUFFIXES:
        return False
    if compiled and row.file in compiled:
        return False
    if row.n_cases_compiled:
        return False
    for attempt in (row.detail.get("compile_attempts") or []):
        if "file not found" in (attempt.get("stderr_head") or ""):
            return True
    return False


def citywalk_aggregate(rows: list[Row]) -> dict:
    """The four metrics under the conventions of the source study's evaluator.

    Its `utils.py` fixes three choices that ours does not share, and the
    comparison is only honest on its footing:

    * the unit is the *file* holding the focal method, read from the per-file
      rows of `llvm-cov report`, not the method's own line range;
    * a file whose branch column is empty contributes 0 to the branch sum and
      still counts in the denominator (`... if ... is not None else 0`,
      divided by `count`), so branchless code is scored, not skipped;
    * the average is over entries, giving a 3-line file the weight of a
      3000-line one.

    Reported next to the method-level figures rather than instead of them:
    the study's own prose describes coverage "within the focal methods",
    which is what the method-level column measures, while these numbers are
    what its released code computes.
    """
    line_sum = branch_sum = 0.0
    count = 0
    cases = compiled = passed = 0
    for row in rows:
        # A case the preprocessor removed is neither written nor compiled
        # as far as the binary is concerned; it leaves both totals.
        excluded = row.n_cases_compiled - effective_compiled(row)
        cases += row.n_cases - excluded
        compiled += effective_compiled(row)
        passed += row.n_cases_passed
        if not row.n_cases_passed:
            continue
        whole = row.detail.get("coverage_whole_file") or {}
        if not whole.get("lines_found"):
            continue
        line_sum += float(whole.get("line_coverage") or 0.0)
        branch_sum += float(whole.get("branch_coverage") or 0.0)
        count += 1
    return {
        "methods": len(rows),
        "scored": count,
        "csr": 100.0 * compiled / cases if cases else 0.0,
        "epr": 100.0 * passed / compiled if compiled else 0.0,
        "line_coverage": line_sum / count if count else 0.0,
        "branch_coverage": branch_sum / count if count else 0.0,
    }


def branch_coverage_variants(rows: list[Row]) -> dict:
    """Branch coverage of the focal methods under the three defensible rules.

    They disagree by tens of points on projects where most methods hold no
    branch, so the paper reports which one it used rather than a bare figure.
    """
    scored = [r for r in rows if r.n_cases_passed]
    with_branches = [r for r in scored if _has_branches(r)]
    found = sum((r.detail.get("coverage") or {}).get("branches_found", 0)
                for r in scored)
    hit = sum((r.detail.get("coverage") or {}).get("branches_hit", 0)
              for r in scored)
    mean = lambda rs: (sum(r.branch_coverage for r in rs) / len(rs)) if rs else 0.0
    return {
        "keeping_zeros": mean(scored),
        "excluding_branchless": mean(with_branches),
        "pooled": 100.0 * hit / found if found else 0.0,
        "n_scored": len(scored),
        "n_branchless": len(scored) - len(with_branches),
    }


def by_complexity(rows: list[Row]) -> list[Aggregate]:
    out = []
    for low, high, label in CC_BUCKETS:
        bucket = [r for r in rows if low <= r.focal_cc <= high]
        if bucket:
            out.append(accumulate(bucket, label))
    return out


def by_file(rows: list[Row], top: int = 15) -> list[Aggregate]:
    groups: dict[str, list[Row]] = {}
    for row in rows:
        groups.setdefault(row.file, []).append(row)
    aggregates = [accumulate(group, name) for name, group in groups.items()]
    aggregates.sort(key=lambda a: a.methods, reverse=True)
    return aggregates[:top]


def error_breakdown(rows: list[Row]) -> list[tuple[str, int, float]]:
    """(category, methods affected, share of all methods), most common first."""
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.error_category or "none"] = counts.get(row.error_category or "none", 0) + 1
    total = len(rows) or 1
    ordered = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [(name, n, 100.0 * n / total) for name, n in ordered]


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(cells) + " |" for cells in rows]
    return "\n".join(lines)


def _agg_row(agg: Aggregate) -> list[str]:
    return [
        agg.label, str(agg.methods), str(agg.cases),
        f"{agg.csr:.1f}%", f"{agg.epr:.1f}%",
        f"{agg.line_coverage:.1f}%",
        f"{agg.branch_coverage:.1f}%" if agg.branch_cov_n else "n/a",
        f"{agg.touched_pct:.1f}%",
    ]


AGG_HEADERS = ["Group", "Methods", "Cases", "CSR", "EPR",
               "Line cov", "Branch cov", "Touched focal"]


def render_markdown(rows: list[Row], config: dict | None = None) -> str:
    """The full summary, ready to paste into a write-up."""
    overall = accumulate(rows)
    parts: list[str] = ["# Benchmark summary", ""]

    if config:
        parts += ["## Configuration", ""]
        selection = config.get("selection") or {}
        settings = [
            ("Model requested", config.get("model_requested", "")),
            ("Temperature", config.get("temperature", "")),
            ("Recursion limit", config.get("recursion_limit", "")),
            ("Selection", ", ".join(f"{k}={v}" for k, v in selection.items() if v) or "all"),
        ]
        parts += [_markdown_table(["Setting", "Value"],
                                  [[k, str(v)] for k, v in settings]), ""]
        models = sorted({r.model for r in rows if r.model})
        if len(models) > 1:
            parts += [
                f"> **Warning:** the endpoint served more than one model during this "
                f"sweep ({', '.join(models)}). The rows are not from a single model "
                f"and should not be reported as such.", "",
            ]

    parts += ["## Overall", ""]
    failed = overall.methods - overall.methods_with_output
    overview = [
        ("Focal methods", str(overall.methods)),
        ("Methods with a generated test file", f"{overall.methods_with_output} "
         f"({100.0 * overall.methods_with_output / overall.methods:.1f}%)"
         if overall.methods else "0"),
        ("Methods with no output", str(failed)),
        ("Test cases written", str(overall.cases)),
        ("Cases compiled", f"{overall.cases_compiled} (CSR {overall.csr:.1f}%)"),
        ("Cases passed", f"{overall.cases_passed} (EPR {overall.epr:.1f}%)"),
        ("Avg line coverage of focal method", f"{overall.line_coverage:.1f}% "
         f"(over {overall.line_cov_n} methods with running tests)"),
        ("Avg branch coverage of focal method", f"{overall.branch_coverage:.1f}% "
         f"(over {overall.branch_cov_n} branchy methods)"),
        ("Focal method actually executed", f"{overall.touched} / {overall.methods} "
         f"({overall.touched_pct:.1f}%)"),
        ("Total tokens", f"{overall.tokens:,}"),
        ("Total generation time", f"{overall.duration_s / 3600:.2f} h"),
        ("Mean tokens per method", f"{overall.tokens // overall.methods:,}"
         if overall.methods else "0"),
        ("Mean generation time per method", f"{overall.duration_s / overall.methods:.1f} s"
         if overall.methods else "0"),
        ("Build/fix rounds (total)", str(overall.fix_rounds)),
        ("Test files using mocks", str(overall.mock_files)),
        ("Generated test LOC", f"{overall.test_loc:,}"),
    ]
    parts += [_markdown_table(["Metric", "Value"], [[k, v] for k, v in overview]), ""]

    parts += ["## By focal complexity", "",
              _markdown_table(AGG_HEADERS, [_agg_row(a) for a in by_complexity(rows)]), ""]

    parts += ["## By source file (top 15 by method count)", "",
              _markdown_table(AGG_HEADERS, [_agg_row(a) for a in by_file(rows)]), ""]

    parts += ["## First failure by category", "",
              _markdown_table(
                  ["Category", "Methods", "Share"],
                  [[name, str(n), f"{pct:.1f}%"] for name, n, pct in error_breakdown(rows)],
              ), ""]

    worst = sorted(
        [r for r in rows if r.error_category],
        key=lambda r: (r.n_cases_passed, -r.n_cases),
    )[:15]
    if worst:
        parts += ["## Hardest methods (first 15 with a failure)", "",
                  _markdown_table(
                      ["File", "Method", "CC", "Cases", "Compiled", "Passed", "Error"],
                      [[r.file, r.method, str(r.focal_cc), str(r.n_cases),
                        str(r.n_cases_compiled), str(r.n_cases_passed),
                        r.error_category] for r in worst],
                  ), ""]
    return "\n".join(parts)
