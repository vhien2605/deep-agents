"""Coverage measurement tools using gcov/lcov instrumentation."""

import json
import shutil
import subprocess
from pathlib import Path

from langchain_core.tools import tool

from src.tools.build import run_in_project


def _demangle(names: list[str]) -> list[str]:
    """Demangle C++ symbol names with c++filt when available."""
    mangled = [n for n in names if n.startswith("_Z")]
    if not mangled or shutil.which("c++filt") is None:
        return names
    try:
        out = subprocess.run(
            ["c++filt"], input="\n".join(mangled), capture_output=True, text=True, timeout=10
        ).stdout.splitlines()
        table = dict(zip(mangled, out))
        return [table.get(n, n) for n in names]
    except (subprocess.SubprocessError, OSError):
        return names


def parse_lcov_tracefile(text: str, max_uncovered_per_file: int = 40) -> dict:
    """Parse an lcov .info tracefile into a summary.

    Handles both the classic function records (FNDA:<hits>,<name>) and the
    lcov 2.x format (FNA:<index>,<hits>,<name>).
    """
    files: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("SF:"):
            current = {
                "file": line[3:],
                "lines_found": 0,
                "lines_hit": 0,
                "uncovered_lines": [],
                "uncovered_functions": [],
            }
        elif current is None:
            continue
        elif line.startswith("DA:"):
            lineno, hits = line[3:].split(",")[:2]
            current["lines_found"] += 1
            if int(hits) > 0:
                current["lines_hit"] += 1
            elif len(current["uncovered_lines"]) < max_uncovered_per_file:
                current["uncovered_lines"].append(int(lineno))
        elif line.startswith("FNDA:"):
            hits, name = line[5:].split(",", 1)
            if int(hits) == 0:
                current["uncovered_functions"].append(name)
        elif line.startswith("FNA:"):
            _idx, hits, name = line[4:].split(",", 2)
            if int(hits) == 0:
                current["uncovered_functions"].append(name)
        elif line == "end_of_record":
            found, hit = current["lines_found"], current["lines_hit"]
            current["line_coverage_pct"] = round(100.0 * hit / found, 2) if found else 0.0
            current["uncovered_functions"] = _demangle(current["uncovered_functions"])
            files.append(current)
            current = None

    total_found = sum(f["lines_found"] for f in files)
    total_hit = sum(f["lines_hit"] for f in files)
    return {
        "total_line_coverage_pct": round(100.0 * total_hit / total_found, 2) if total_found else 0.0,
        "files": files,
    }


def make_coverage_tools(project_root: Path) -> list:
    """Build lcov/gcov coverage tools scoped to `project_root`."""

    @tool
    def run_coverage_build(make_coverage_target: str = "coverage") -> str:
        """Run the project's coverage make target (default: `make coverage`).
        The target is expected to rebuild with `--coverage` (gcov instrumentation),
        run the tests, and produce an lcov tracefile (usually coverage.info).
        Returns the raw command output. If the target doesn't exist, fall back to
        building manually with CXXFLAGS+="--coverage" and then call
        capture_lcov_tracefile.
        """
        return run_in_project(project_root, ["make", make_coverage_target])

    @tool
    def capture_lcov_tracefile(output_file: str = "coverage.info", directory: str = ".") -> str:
        """Run `lcov --capture` over gcov data (.gcda files produced by running an
        instrumented test binary) and write a tracefile. Use when the Makefile has
        no coverage target: build with --coverage, run tests, then call this.
        """
        return run_in_project(
            project_root,
            [
                "lcov", "--capture",
                "--directory", directory,
                "--output-file", output_file,
                "--rc", "branch_coverage=1",
                "--ignore-errors", "inconsistent,unsupported,unused,empty",
            ],
        )

    @tool
    def coverage_report(tracefile: str = "coverage.info", source_filter: str = "") -> str:
        """Parse an lcov tracefile and return a JSON coverage report: total line
        coverage %, per-file coverage, uncovered line numbers, and functions never
        executed. `source_filter` (optional substring, e.g. "src/") keeps only
        matching files — use it to exclude system headers and test files, so the
        percentage reflects the code under test.
        """
        path = Path(tracefile)
        full = path if path.is_absolute() else project_root / path
        if not full.is_file():
            return (
                f"ERROR: tracefile not found: {full}. "
                "Run run_coverage_build or capture_lcov_tracefile first."
            )
        report = parse_lcov_tracefile(full.read_text(errors="replace"))
        if source_filter:
            report["files"] = [f for f in report["files"] if source_filter in f["file"]]
            found = sum(f["lines_found"] for f in report["files"])
            hit = sum(f["lines_hit"] for f in report["files"])
            report["total_line_coverage_pct"] = round(100.0 * hit / found, 2) if found else 0.0
        return json.dumps(report, indent=2, ensure_ascii=False)

    @tool
    def coverage_py_report(json_file: str = "coverage.json", source_filter: str = "") -> str:
        """Parse a coverage.py JSON report (Python projects) and return the same
        JSON shape as coverage_report: total line coverage %, per-file coverage,
        and uncovered line numbers. Produce the input file first by running the
        tests under coverage, e.g. run_command("python -m coverage run -m pytest")
        then run_command("python -m coverage json"). `source_filter` keeps only
        matching paths (e.g. "src/") so test files don't skew the percentage.
        """
        path = Path(json_file)
        full = path if path.is_absolute() else project_root / path
        if not full.is_file():
            return (
                f"ERROR: report not found: {full}. Run the tests under coverage "
                "first: run_command('python -m coverage run -m pytest') then "
                "run_command('python -m coverage json')."
            )
        try:
            data = json.loads(full.read_text(errors="replace"))
        except json.JSONDecodeError as exc:
            return f"ERROR: invalid coverage JSON: {exc}"

        files = []
        for name, info in (data.get("files") or {}).items():
            if source_filter and source_filter not in name:
                continue
            summary = info.get("summary", {})
            found = int(summary.get("num_statements", 0))
            missing = list(info.get("missing_lines", []))[:40]
            files.append({
                "file": name,
                "lines_found": found,
                "lines_hit": found - int(summary.get("missing_lines", 0)),
                "line_coverage_pct": round(float(summary.get("percent_covered", 0.0)), 2),
                "uncovered_lines": missing,
                "uncovered_functions": [
                    fn for fn, finfo in (info.get("functions") or {}).items()
                    if fn and int(finfo.get("summary", {}).get("covered_lines", 1)) == 0
                ],
            })
        total_found = sum(f["lines_found"] for f in files)
        total_hit = sum(f["lines_hit"] for f in files)
        report = {
            "total_line_coverage_pct": round(100.0 * total_hit / total_found, 2)
            if total_found else 0.0,
            "files": files,
        }
        return json.dumps(report, indent=2, ensure_ascii=False)

    @tool
    def jacoco_report(
        xml_file: str = "target/site/jacoco/jacoco.xml", source_filter: str = ""
    ) -> str:
        """Parse a JaCoCo XML report (Java/Maven projects) and return the same
        JSON shape as coverage_report: total line coverage %, per-file coverage,
        uncovered line numbers, and methods never executed (Class.method).
        Produce the input file first with run_command("mvn test jacoco:report")
        — requires the jacoco-maven-plugin in pom.xml (see the coverage-jacoco
        skill). `source_filter` keeps only matching paths (e.g. "com/mycompany").
        """
        import xml.etree.ElementTree as ET

        path = Path(xml_file)
        full = path if path.is_absolute() else project_root / path
        if not full.is_file():
            return (
                f"ERROR: report not found: {full}. Run "
                "run_command('mvn test jacoco:report') first (jacoco-maven-plugin "
                "must be configured in pom.xml — see the coverage-jacoco skill)."
            )
        try:
            root = ET.fromstring(full.read_text(errors="replace"))
        except ET.ParseError as exc:
            return f"ERROR: invalid JaCoCo XML: {exc}"

        files = []
        for pkg in root.iter("package"):
            pkg_name = pkg.get("name", "")
            # Methods never executed, grouped by source file.
            uncovered_by_file: dict[str, list[str]] = {}
            for cls in pkg.findall("class"):
                src_name = cls.get("sourcefilename", "")
                cls_short = cls.get("name", "").split("/")[-1]
                for method in cls.findall("method"):
                    m_name = method.get("name", "")
                    if m_name in ("<init>", "<clinit>"):
                        m_name = "constructor" if m_name == "<init>" else "static-init"
                    counters = {
                        c.get("type"): c for c in method.findall("counter")
                    }
                    # NB: explicit None checks — empty ET.Element is falsy.
                    line_counter = counters.get("LINE")
                    if line_counter is None:
                        line_counter = counters.get("INSTRUCTION")
                    if line_counter is not None and int(line_counter.get("covered", 0)) == 0:
                        uncovered_by_file.setdefault(src_name, []).append(
                            f"{cls_short}.{m_name}"
                        )
            for sf in pkg.findall("sourcefile"):
                rel = f"{pkg_name}/{sf.get('name', '')}" if pkg_name else sf.get("name", "")
                if source_filter and source_filter not in rel:
                    continue
                missed_lines = [
                    int(line.get("nr", 0))
                    for line in sf.findall("line")
                    if int(line.get("ci", 0)) == 0  # no covered instructions
                ]
                line_counter = next(
                    (c for c in sf.findall("counter") if c.get("type") == "LINE"), None
                )
                missed = int(line_counter.get("missed", 0)) if line_counter is not None else 0
                covered = int(line_counter.get("covered", 0)) if line_counter is not None else 0
                found = missed + covered
                files.append({
                    "file": rel,
                    "lines_found": found,
                    "lines_hit": covered,
                    "line_coverage_pct": round(100.0 * covered / found, 2) if found else 0.0,
                    "uncovered_lines": missed_lines[:40],
                    "uncovered_functions": uncovered_by_file.get(sf.get("name", ""), []),
                })

        total_found = sum(f["lines_found"] for f in files)
        total_hit = sum(f["lines_hit"] for f in files)
        report = {
            "total_line_coverage_pct": round(100.0 * total_hit / total_found, 2)
            if total_found else 0.0,
            "files": files,
        }
        return json.dumps(report, indent=2, ensure_ascii=False)

    return [
        run_coverage_build,
        capture_lcov_tracefile,
        coverage_report,
        coverage_py_report,
        jacoco_report,
    ]
