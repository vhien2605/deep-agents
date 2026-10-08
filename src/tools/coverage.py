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


