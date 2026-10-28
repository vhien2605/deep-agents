"""Turn raw compiler/runner/coverage output into the numbers the table needs.

Everything here is pure text/JSON processing with no side effects, so each
metric can be unit-tested against a captured tool output.
"""

import json
import re
from pathlib import Path

from tree_sitter import Node

from src.tools.analysis import LANGUAGES, _node_text, _parse, _walk
from bench.discover import CPP_SHORT_CIRCUIT

# GoogleTest case declarations. TEST_P/TYPED_TEST also register cases, so they
# count too; the suite name is the first argument, the case name the second.
GTEST_CASE_RE = re.compile(
    r"\b(TEST|TEST_F|TEST_P|TYPED_TEST|TYPED_TEST_P)\s*\(\s*([A-Za-z_]\w*)\s*,\s*([A-Za-z_]\w*)\s*\)"
)
MOCK_RE = re.compile(r"#\s*include\s*[<\"]gmock/|MOCK_METHOD|::testing::(NiceMock|StrictMock|Mock)")

# Compiler diagnostics, most specific first — the first match wins, so an
# ordering change here changes the reported category.
COMPILE_ERROR_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("missing_include", re.compile(r"fatal error:.*?(?:file not found|No such file or directory)", re.I)),
    ("undeclared_identifier", re.compile(r"(?:was not declared in this scope|use of undeclared identifier|undeclared \(first use)", re.I)),
    ("no_member", re.compile(r"(?:has no member named|no member named|is not a member of)", re.I)),
    ("private_access", re.compile(r"(?:is private within this context|is a private member|private member of)", re.I)),
    ("no_matching_function", re.compile(r"(?:no matching function for call|no matching constructor|no match for call)", re.I)),
    ("wrong_argument_count", re.compile(r"(?:too few arguments|too many arguments)", re.I)),
    ("deleted_function", re.compile(r"use of deleted function|call to deleted", re.I)),
    ("incomplete_type", re.compile(r"invalid use of incomplete type|incomplete type .* used", re.I)),
    ("ambiguous_call", re.compile(r"is ambiguous", re.I)),
    ("type_mismatch", re.compile(r"(?:cannot convert|no viable conversion|invalid conversion|incompatible types|invalid operands)", re.I)),
    ("linker_error", re.compile(r"(?:undefined reference to|undefined symbols? for|ld returned|multiple definition of)", re.I)),
    ("syntax_error", re.compile(r"(?:expected .*before|expected ['\"]|parse error|error: expected)", re.I)),
]

# Runtime failures, checked against the test binary's output and exit status.
RUNTIME_ERROR_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("assertion_failure", re.compile(r"(?:Failure\b|Expected equality|Assertion .* failed|EXPECT_|ASSERT_)")),
    ("uncaught_exception", re.compile(r"(?:terminate called|C\+\+ exception with description|throws an exception)", re.I)),
    ("segfault", re.compile(r"(?:Segmentation fault|SIGSEGV|SEGV_)", re.I)),
    ("abort", re.compile(r"(?:SIGABRT|Aborted)", re.I)),
    ("sanitizer", re.compile(r"(?:AddressSanitizer|UndefinedBehaviorSanitizer|LeakSanitizer)", re.I)),
]

DIAGNOSTIC_LINE_RE = re.compile(r"^(?P<file>[^\s:]+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?:fatal )?error:", re.M)


def count_gtest_cases(text: str) -> list[str]:
    """Case ids (`Suite.Name`) declared in a test file, in source order."""
    return [f"{m.group(2)}.{m.group(3)}" for m in GTEST_CASE_RE.finditer(text)]


def uses_mock(text: str) -> bool:
    return bool(MOCK_RE.search(text))


def test_loc(text: str) -> int:
    """Non-blank, non-comment lines — the LOC convention used in the paper."""
    count = 0
    in_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if in_block:
            if "*/" in line:
                in_block = False
                line = line.split("*/", 1)[1].strip()
            else:
                continue
        while line.startswith("/*"):
            if "*/" in line:
                line = line.split("*/", 1)[1].strip()
            else:
                in_block = True
                line = ""
        if not line or line.startswith("//"):
            continue
        count += 1
    return count


def _complexity_of(node: Node) -> int:
    branch_types = LANGUAGES["cpp"]["branch_types"]
    points = 0
    for child in _walk(node):
        if child.type in branch_types:
            points += 1
        elif child.type == "binary_expression":
            operator = child.child_by_field_name("operator")
            if operator is not None and operator.type in CPP_SHORT_CIRCUIT:
                points += 1
    return points + 1


def test_complexity(text: str) -> int:
    """Cyclomatic complexity summed over the file's test-case bodies.

    A GoogleTest case is a macro invocation, which tree-sitter parses as a
    function-like definition; when the grammar cannot resolve it (macros often
    confuse the parser) the whole translation unit is scored instead, which is
    the same number for a file that contains only test cases.
    """
    tree = _parse(text.encode(), "cpp")
    bodies = [
        node for node in _walk(tree.root_node)
        if node.type == "function_definition" and node.child_by_field_name("body") is not None
    ]
    if bodies:
        # Sum of per-case complexity, minus the double-counted +1 per case, so
        # a file of N trivial cases scores N rather than 2N.
        return sum(_complexity_of(b) for b in bodies)
    return _complexity_of(tree.root_node)


def classify_error(output: str, *, stage: str) -> tuple[str, str]:
    """(category, first diagnostic line) for a failed compile or test run."""
    if not output.strip():
        return ("unknown", "")
    patterns = COMPILE_ERROR_PATTERNS if stage == "compile" else RUNTIME_ERROR_PATTERNS
    category = "unknown"
    for name, pattern in patterns:
        if pattern.search(output):
            category = name
            break
    if category == "unknown" and stage == "compile":
        # A diagnostic we have no pattern for is still a compile error.
        category = "compile_error" if "error:" in output else "unknown"
    first_line = ""
    match = DIAGNOSTIC_LINE_RE.search(output)
    if match:
        line_start = output.rfind("\n", 0, match.start()) + 1
        line_end = output.find("\n", match.start())
        first_line = output[line_start: line_end if line_end != -1 else len(output)].strip()
    else:
        for raw in output.splitlines():
            if raw.strip():
                first_line = raw.strip()
                break
    return category, first_line[:300]


def failing_case_lines(output: str, test_file: str) -> set[int]:
    """Line numbers in `test_file` that the compiler flagged as errors.

    Used to drop only the broken cases and recompile the rest, which is how
    the per-case compile rate is obtained from a whole-file compiler.
    """
    stem = Path(test_file).name
    lines = set()
    for match in DIAGNOSTIC_LINE_RE.finditer(output):
        if Path(match.group("file")).name == stem:
            lines.add(int(match.group("line")))
    return lines


def parse_gtest_json(text: str) -> dict:
    """Per-case results from `--gtest_output=json`.

    Returns {"cases": {"Suite.Name": "passed"|"failed"|"skipped"}, ...}.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"cases": {}, "total": 0, "passed": 0, "failed": 0}
    cases: dict[str, str] = {}
    for suite in data.get("testsuites", []) or []:
        suite_name = suite.get("name", "")
        for case in suite.get("testsuite", []) or []:
            name = f"{suite_name}.{case.get('name', '')}"
            if case.get("result") == "skipped" or case.get("status") == "NOTRUN":
                cases[name] = "skipped"
            elif case.get("failures"):
                cases[name] = "failed"
            else:
                cases[name] = "passed"
    return {
        "cases": cases,
        "total": len(cases),
        "passed": sum(1 for v in cases.values() if v == "passed"),
        "failed": sum(1 for v in cases.values() if v == "failed"),
    }


def _iter_gcovr_files(report: dict):
    for entry in report.get("files", []) or []:
        yield entry.get("file", ""), entry


def focal_coverage(report: dict, focal_file: str, start_line: int, end_line: int) -> dict:
    """Line/branch coverage restricted to one method's line range.

    gcovr reports whole files, but the paper scores the focal method, so the
    per-line records are filtered to [start_line, end_line] before totalling.
    """
    target = Path(focal_file).as_posix()
    lines_found = lines_hit = branches_found = branches_hit = 0
    for name, entry in _iter_gcovr_files(report):
        posix = Path(name).as_posix()
        if not (posix.endswith(target) or target.endswith(posix)):
            continue
        for record in entry.get("lines", []) or []:
            number = record.get("line_number", 0)
            if not (start_line <= number <= end_line):
                continue
            lines_found += 1
            if record.get("count", 0) > 0:
                lines_hit += 1
            for branch in record.get("branches", []) or []:
                branches_found += 1
                if branch.get("count", 0) > 0:
                    branches_hit += 1
    return {
        "line_coverage": round(100.0 * lines_hit / lines_found, 2) if lines_found else 0.0,
        "branch_coverage": round(100.0 * branches_hit / branches_found, 2) if branches_found else 0.0,
        "lines_found": lines_found,
        "lines_hit": lines_hit,
        "branches_found": branches_found,
        "branches_hit": branches_hit,
        "touched_focal": lines_hit > 0,
    }


def _llvm_file_entry(report: dict, focal_file: str) -> dict | None:
    """The `llvm-cov export` record for the file holding the focal method.

    llvm-cov keys files by the path the compiler saw, which may be absolute
    or relative depending on how CMake was invoked, so the match is on a
    path suffix rather than on equality.
    """
    target = Path(focal_file).as_posix()
    for entry in (report.get("data") or [{}])[0].get("files", []) or []:
        name = Path(entry.get("filename", "")).as_posix()
        if name.endswith(target) or target.endswith(name):
            return entry
    return None


def _rate(hit: int, found: int) -> float:
    return round(100.0 * hit / found, 2) if found else 0.0


def _opens_region(segment: list) -> bool:
    """True when a segment starts a real region, as llvm-cov defines it.

    A gap region is layout, not code: llvm-cov emits one to bridge the text
    between two regions and never lets it decide a line's count.
    """
    return (
        len(segment) > 5
        and bool(segment[3])
        and bool(segment[4])
        and not bool(segment[5])
    )


def _llvm_line_counts(
    segments: list, start_line: int, end_line: int
) -> dict[int, int]:
    """Per-line execution counts over a line range, as llvm-cov computes them.

    A segment records where a region *starts*, not which lines it spans, so a
    line carrying no segment of its own still executes at the count of the
    region that is open across it. llvm-cov reconstructs this by keeping the
    last segment before each line -- the wrapped segment -- and preferring the
    regions that open on the line itself. Reading the segments alone, without
    that carry, marks every continuation line unexecuted and reports roughly
    half the true line coverage.
    """
    ordered = sorted(
        (s for s in segments or [] if len(s) >= 4), key=lambda s: (s[0], s[1])
    )
    counts: dict[int, int] = {}
    index = 0
    wrapped: list | None = None
    for line in range(start_line, end_line + 1):
        while index < len(ordered) and ordered[index][0] < line:
            wrapped = ordered[index]
            index += 1
        on_line = []
        probe = index
        while probe < len(ordered) and ordered[probe][0] == line:
            on_line.append(ordered[probe])
            probe += 1

        # A line whose first segment enters a region with no count at all is
        # skipped code -- an #if branch the preprocessor removed, say -- and
        # is not executable, so it belongs in neither the numerator nor the
        # denominator.
        skipped = bool(on_line) and not on_line[0][3] and bool(
            len(on_line[0]) > 4 and on_line[0][4]
        )
        opening = [s for s in on_line if _opens_region(s)]
        mapped = not skipped and (
            bool(opening) or (wrapped is not None and bool(wrapped[3]))
        )
        if not mapped:
            continue

        # The count is the larger of the region still open across the line
        # and any region opening on it. Taking only the opening region would
        # read zero on a continuation line of a multi-line condition, whose
        # own region has not been entered separately.
        count = wrapped[2] if (wrapped is not None and wrapped[3]) else 0
        for segment in opening:
            count = max(count, segment[2])
        counts[line] = count
    return counts


def _owned_branch_spans(report: dict, focal_file: str) -> set | None:
    """Branch spans that some function of the file actually owns.

    A file's flat `branches` list can carry a region no function claims --
    re2/prog.cc lists 177 spans where its 33 functions own 176 -- and
    llvm-cov's own summary is built by adding up functions, so it never sees
    the orphan. Keeping it would inflate the denominator by a branch no test
    could ever reach. Returns None when the export carries no function
    records, in which case the caller keeps the flat list.
    """
    target = Path(focal_file).as_posix()
    functions = (report.get("data") or [{}])[0].get("functions")
    if not functions:
        return None
    owned: set = set()
    for function in functions:
        names = [Path(n).as_posix() for n in function.get("filenames") or []]
        if not any(n.endswith(target) or target.endswith(n) for n in names):
            continue
        for branch in function.get("branches") or []:
            if len(branch) >= 6:
                owned.add(tuple(branch[:4]))
    return owned


def _branch_totals(records: list) -> tuple[int, int]:
    """Branch outcomes covered and in total, over a set of branch records.

    A template instantiated N times emits the same source branch N times --
    string-to-double.cc reports 464 records for 206 distinct branches -- and
    counting each copy would weight a header by how many types instantiate it.
    llvm-cov merges records sharing a span, summing their counts, so a branch
    taken in one instantiation counts as taken. Doing the same reproduces its
    covered total exactly; its denominator agrees to within one region in 205,
    a residue we report rather than tune away.
    """
    merged: dict[tuple, list[int]] = {}
    for record in records:
        if len(record) < 6:
            continue
        key = tuple(record[:4]) + tuple(record[6:8])
        if key in merged:
            merged[key][0] += record[4]
            merged[key][1] += record[5]
        else:
            merged[key] = [record[4], record[5]]
    found = 2 * len(merged)
    hit = sum((1 if t else 0) + (1 if f else 0) for t, f in merged.values())
    return found, hit


def _expansion_branches(entry: dict, start_line: int, end_line: int) -> list:
    """Branch records inside macros expanded within a line range.

    llvm-cov keeps these out of a file's own `branches` list yet still counts
    them in its summary, so reading only that list undercounts every file
    built on macros -- json.cpp loses 11 of 334 records. A record's own
    coordinates point into the macro definition, in another file entirely, so
    it is the expansion's source region, the place the macro was written, that
    decides whether the record belongs to the focal method.
    """
    collected: list = []

    def collect(expansion: dict) -> None:
        collected.extend(expansion.get("branches") or [])
        for nested in expansion.get("expansions") or []:
            collect(nested)

    for expansion in entry.get("expansions") or []:
        region = expansion.get("source_region") or []
        if region and start_line <= region[0] <= end_line:
            collect(expansion)
    return collected


def _instantiation_branch_totals(
    report: dict, focal_file: str, start_line: int, end_line: int
) -> tuple[int, int] | None:
    """Branch outcomes over a line range, counted the way llvm-cov counts them.

    A template instantiated for several types emits one function record per
    instantiation, all mapping to the same source. llvm-cov collects them into
    an instantiation group and merges their statistics by taking a maximum,
    not a union:

        Covered = std::max(Covered, RHS.Covered);
        NumBranches = std::max(NumBranches, RHS.NumBranches);

    so a branch reached only by the `int` instantiation does not add to the
    credit earned by the `double` one. Merging by source span instead -- the
    intuitive reading -- overstates coverage on template-heavy code: re2's
    dfa.cc scores 254 outcomes that way against llvm-cov's 250.

    Branches inside macro expansions live outside the function records and are
    added separately. Returns None when the export carries no function
    records, leaving the caller its own fallback.
    """
    target = Path(focal_file).as_posix()
    functions = (report.get("data") or [{}])[0].get("functions")
    if not functions:
        return None

    groups: dict[tuple, list[int]] = {}
    for function in functions:
        names = [Path(n).as_posix() for n in function.get("filenames") or []]
        index = next(
            (i for i, name in enumerate(names)
             if name.endswith(target) or target.endswith(name)), None)
        if index is None:
            continue
        found = covered = 0
        for branch in function.get("branches") or []:
            if len(branch) < 6 or (len(branch) > 6 and branch[6] != index):
                continue
            if not (start_line <= branch[0] <= end_line):
                continue
            found += 2
            covered += (1 if branch[4] else 0) + (1 if branch[5] else 0)
        # Instantiations of one template share a starting position; that is
        # what llvm-cov groups on.
        regions = function.get("regions") or []
        anchor = tuple(regions[0][:2]) if regions else (function.get("name"),)
        slot = groups.setdefault((index, anchor), [0, 0])
        slot[0] = max(slot[0], found)
        slot[1] = max(slot[1], covered)

    found = sum(slot[0] for slot in groups.values())
    covered = sum(slot[1] for slot in groups.values())
    # Expansion records are NOT merged by span. Two uses of one macro emit
    # records with identical coordinates, because those coordinates point
    # into the macro definition rather than into this file, and llvm-cov
    # counts each use. Merging them costs one region per repeated macro:
    # ninja's graph.cc reports 163 branch regions where merging finds 161.
    records = _expansion_branches(entry_of(report, focal_file),
                                  start_line, end_line)
    found += 2 * len(records)
    covered += sum((1 if b[4] else 0) + (1 if b[5] else 0) for b in records)
    return found, covered


def entry_of(report: dict, focal_file: str) -> dict:
    """The file record, or an empty one when the export does not carry it."""
    return _llvm_file_entry(report, focal_file) or {}


def llvm_focal_coverage(
    report: dict, focal_file: str, start_line: int, end_line: int
) -> dict:
    """Coverage of one method's line range, from `llvm-cov export` output.

    Lines follow llvm-cov's own segment walk (see `_llvm_line_counts`). A
    branch record is [lineStart, colStart, lineEnd, colEnd, trueCount,
    falseCount, ...], and llvm-cov scores its two outcomes separately, which
    is why each record contributes two to the denominator. A method holding
    no branch at all reports `branches_found` of zero; the rate is left at
    zero but the two counts are what callers should average, so that a
    branchless method neither earns nor loses credit it has no basis for.
    """
    entry = _llvm_file_entry(report, focal_file)
    if entry is None:
        return {
            "line_coverage": 0.0, "branch_coverage": 0.0,
            "lines_found": 0, "lines_hit": 0,
            "branches_found": 0, "branches_hit": 0, "touched_focal": False,
        }

    counts = _llvm_line_counts(entry.get("segments", []), start_line, end_line)

    totals = _instantiation_branch_totals(
        report, focal_file, start_line, end_line)
    if totals is None:
        owned = _owned_branch_spans(report, focal_file)
        in_range = [
            b for b in (entry.get("branches") or [])
            if len(b) >= 6 and start_line <= b[0] <= end_line
            and (owned is None or tuple(b[:4]) in owned)
        ]
        in_range += _expansion_branches(entry, start_line, end_line)
        totals = _branch_totals(in_range)
    branches_found, branches_hit = totals

    lines_found = len(counts)
    lines_hit = sum(1 for value in counts.values() if value > 0)
    return {
        "line_coverage": _rate(lines_hit, lines_found),
        "branch_coverage": _rate(branches_hit, branches_found),
        "lines_found": lines_found,
        "lines_hit": lines_hit,
        "branches_found": branches_found,
        "branches_hit": branches_hit,
        "touched_focal": lines_hit > 0,
    }


def llvm_whole_file_coverage(report: dict, focal_file: str) -> dict:
    """Coverage of the entire file holding the focal method.

    CITYWALK's released evaluator scores this, not the method: it reads the
    per-file rows of `llvm-cov report` and keeps the one whose filename
    matches the focal source, so a test exercising one method is credited
    against every line of the file it lives in. Its paper describes the
    metric as lines "within the focal methods", which its own code does not
    do, so both numbers are recorded here and the comparison is made on
    their footing rather than ours. A file with no branches reports a rate
    of zero while still counting, which is what their averaging does with
    the empty branch column.
    """
    entry = _llvm_file_entry(report, focal_file)
    if entry is None:
        return {
            "line_coverage": 0.0, "branch_coverage": 0.0,
            "lines_found": 0, "lines_hit": 0,
            "branches_found": 0, "branches_hit": 0,
        }
    summary = entry.get("summary", {}) or {}
    lines = summary.get("lines", {}) or {}
    branches = summary.get("branches", {}) or {}
    return {
        "line_coverage": round(float(lines.get("percent", 0.0)), 2),
        "branch_coverage": round(float(branches.get("percent", 0.0)), 2),
        "lines_found": int(lines.get("count", 0)),
        "lines_hit": int(lines.get("covered", 0)),
        "branches_found": int(branches.get("count", 0)),
        "branches_hit": int(branches.get("covered", 0)),
    }


def whole_file_coverage(report: dict, focal_file: str) -> dict:
    """Coverage of the entire file holding the focal method.

    CITYWALK's published evaluator scores this, not the method: its
    `parse_coverage_file` reads the per-file rows of `llvm-cov report` and
    keeps the ones whose filename matches the focal file, so a test that
    exercises one method is credited against every line of the file it lives
    in. Their paper describes the metric as lines "within the focal methods",
    which their own code does not do -- so both numbers are reported here and
    the comparison is made on their footing, not ours.
    """
    target = Path(focal_file).as_posix()
    lines_found = lines_hit = branches_found = branches_hit = 0
    for name, entry in _iter_gcovr_files(report):
        posix = Path(name).as_posix()
        if not (posix.endswith(target) or target.endswith(posix)):
            continue
        for record in entry.get("lines", []) or []:
            lines_found += 1
            if record.get("count", 0) > 0:
                lines_hit += 1
            for branch in record.get("branches", []) or []:
                branches_found += 1
                if branch.get("count", 0) > 0:
                    branches_hit += 1
    return {
        "line_coverage": round(100.0 * lines_hit / lines_found, 2) if lines_found else 0.0,
        # A file with no branches is `-` in their report and contributes 0 to
        # the average while still counting in the denominator; keeping the
        # rate at 0.0 here reproduces that.
        "branch_coverage": round(100.0 * branches_hit / branches_found, 2) if branches_found else 0.0,
        "lines_found": lines_found,
        "lines_hit": lines_hit,
        "branches_found": branches_found,
        "branches_hit": branches_hit,
    }


def extract_case_source(text: str, case_id: str) -> str | None:
    """The full `TEST(...) { ... }` block for one case id, braces balanced."""
    for match in GTEST_CASE_RE.finditer(text):
        if f"{match.group(2)}.{match.group(3)}" != case_id:
            continue
        brace = text.find("{", match.end())
        if brace == -1:
            return None
        depth, index = 0, brace
        while index < len(text):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    return text[match.start(): index + 1]
            index += 1
    return None


def strip_cases(text: str, drop: set[str]) -> str:
    """Remove the named cases from a test file, leaving includes and fixtures."""
    if not drop:
        return text
    result = text
    for case_id in drop:
        block = extract_case_source(result, case_id)
        if block:
            result = result.replace(block, "")
    return result


def cases_covering_lines(text: str, lines: set[int]) -> set[str]:
    """Case ids whose source spans any of the given 1-based line numbers."""
    if not lines:
        return set()
    hits = set()
    for match in GTEST_CASE_RE.finditer(text):
        case_id = f"{match.group(2)}.{match.group(3)}"
        block = extract_case_source(text, case_id)
        if not block:
            continue
        start = text.count("\n", 0, match.start()) + 1
        end = start + block.count("\n")
        if any(start <= line <= end for line in lines):
            hits.add(case_id)
    return hits


def node_text(node: Node) -> str:
    return _node_text(node)
