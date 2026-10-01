"""The result row: one record per focal method, one CSV column per metric."""

import csv
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

# Column order is the reporting contract — CSV readers and the paper's tables
# both depend on it, so it lives here rather than being derived per writer.
COLUMNS = [
    "project", "file", "method", "line", "model",
    "n_cases", "n_cases_compiled", "n_cases_passed", "csr", "epr",
    "line_coverage", "branch_coverage", "touched_focal",
    "total_tokens", "duration_s", "fix_phase",
    "error_category", "error_first_line",
    "focal_cc", "test_loc", "test_cc", "uses_mock",
]


@dataclass
class Row:
    """One benchmark observation.

    Every field is filled even when a stage fails: a run that never produced a
    test file still reports n_cases=0 and the error that stopped it, so failed
    runs stay in the denominator instead of silently vanishing from the table.
    """

    project: str = ""
    file: str = ""            # focal source file, relative to the project root
    method: str = ""          # focal method, qualified (Class::method)
    line: int = 0             # first line of the focal method definition
    model: str = ""           # model id the endpoint actually served

    n_cases: int = 0          # test cases the agent wrote
    n_cases_compiled: int = 0  # cases surviving compilation
    n_cases_passed: int = 0   # cases that also passed at run time
    csr: float = 0.0          # compile success rate = compiled / written
    epr: float = 0.0          # execution pass rate  = passed / compiled

    line_coverage: float = 0.0    # % of focal-method lines covered
    branch_coverage: float = 0.0  # % of focal-method branches covered
    touched_focal: bool = False   # did any test actually execute the method?

    total_tokens: int = 0
    duration_s: float = 0.0
    fix_phase: int = 0        # build/fix rounds the agent went through

    error_category: str = ""   # see metrics.classify_error
    error_first_line: str = ""  # first diagnostic line, verbatim

    focal_cc: int = 0         # cyclomatic complexity of the focal method
    test_loc: int = 0         # non-blank, non-comment lines in the test file
    test_cc: int = 0          # cyclomatic complexity summed over test cases
    uses_mock: bool = False   # test file pulls in gmock

    # Not a CSV column: kept in the JSONL sidecar for debugging a single run.
    detail: dict = field(default_factory=dict)

    def as_csv_dict(self) -> dict:
        data = asdict(self)
        return {col: data[col] for col in COLUMNS}


def write_csv(rows: list[Row], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_csv_dict())


def append_jsonl(row: Row, path: Path) -> None:
    """Append one full row (detail included) as it finishes.

    Written incrementally so a benchmark killed halfway still leaves every
    completed run on disk — a full sweep costs too much to redo for a crash.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[Row]:
    """Load previously completed rows, for --resume."""
    if not path.is_file():
        return []
    known = {f.name for f in fields(Row)}
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(Row(**{k: v for k, v in json.loads(line).items() if k in known}))
    return rows
