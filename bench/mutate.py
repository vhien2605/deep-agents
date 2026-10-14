"""Mutation analysis of the generated tests, over the same focal methods.

The four benchmark metrics reward tests that compile, run, and execute
lines; none of them rewards an assertion that would fail if the
implementation were wrong. This pass measures that directly. Each focal
method's own line range is mutated one operator at a time, the method's
already-scored test is recompiled against the mutant, and the mutant is
killed when a case that passed on the original now fails, crashes, or
times out.

Conventions, each chosen so that a kill can only come from the test:

* Only the cases that passed on the original run execute (a gtest filter),
  so an assertion that was already failing cannot count as a kill.
* The test source is the one the scoring pass finally compiled -- pruned
  and repaired as recorded -- and it is linked against the same build tree.
* Mutants are drawn deterministically from every candidate in the range,
  seeded by the method's identity, at most --max-mutants per method.
* A mutant that does not compile is stillborn and leaves the denominator.
* Equivalent mutants are not detected, so every score is a lower bound.
"""

from __future__ import annotations

import json
import random
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from bench import harness
from bench.schema import read_jsonl

ROWS_NAME = "rows.jsonl"
OUTPUT_NAME = "mutation.jsonl"

# Operators. Each is applied to a line with strings and comments masked, so
# a `<` inside a string literal is never touched; the spaces around the
# binary operators keep template brackets, `->`, `++` and unary minus out.
BINARY_OPS: list[tuple[str, str, str]] = [
    ("ROR", " < ", " <= "), ("ROR", " <= ", " < "),
    ("ROR", " > ", " >= "), ("ROR", " >= ", " > "),
    ("ROR", " == ", " != "), ("ROR", " != ", " == "),
    ("AOR", " + ", " - "), ("AOR", " - ", " + "),
    ("AOR", " * ", " / "), ("AOR", " / ", " * "), ("AOR", " % ", " + "),
    ("AOR", " += ", " -= "), ("AOR", " -= ", " += "),
    ("LCR", " && ", " || "), ("LCR", " || ", " && "),
]
BOOL_LITERALS = [("true", "false"), ("false", "true")]
INT_LITERAL = re.compile(r"(?<![\w.\[])(\d+)(?![\w.])")
CONDITION_HEAD = re.compile(r"\b(if|while)\s*\(")


@dataclass
class Mutant:
    line: int          # 1-based line in the focal file
    col: int           # 0-based column of the replaced span
    op: str
    old: str
    new: str

    @property
    def key(self) -> str:
        return f"{self.line}:{self.col}:{self.op}"


def _mask(line: str, in_block_comment: bool) -> tuple[str, bool]:
    """Blank out string/char literals and comments, preserving length."""
    out = list(line)
    i = 0
    n = len(line)
    while i < n:
        if in_block_comment:
            end = line.find("*/", i)
            stop = n if end < 0 else end + 2
            for k in range(i, stop):
                out[k] = " "
            i = stop
            in_block_comment = end < 0
            continue
        two = line[i:i + 2]
        if two == "//":
            for k in range(i, n):
                out[k] = " "
            break
        if two == "/*":
            in_block_comment = True
            continue
        if line[i] in "\"'":
            quote = line[i]
            j = i + 1
            while j < n and line[j] != quote:
                j += 2 if line[j] == "\\" else 1
            for k in range(i, min(j + 1, n)):
                out[k] = " "
            i = j + 1
            continue
        i += 1
    return "".join(out), in_block_comment


def _matching_paren(text: str, open_at: int) -> int:
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def candidates(lines: list[str], start: int, end: int) -> list[Mutant]:
    """Every mutant the operators can produce inside lines [start, end]."""
    found: list[Mutant] = []
    in_comment = False
    for number in range(1, len(lines) + 1):
        raw = lines[number - 1]
        masked, in_comment = _mask(raw, in_comment)
        if number < start or number > end:
            continue
        if masked.lstrip().startswith("#"):
            continue
        for op, old, new in BINARY_OPS:
            pos = masked.find(old)
            while pos >= 0:
                # ` < ` inside ` <= ` is not a separate site: the longer
                # operator is listed on its own.
                if not (old.strip() in ("<", ">") and masked[pos:pos + len(old) + 1] in (" <= ", " >= ")):
                    found.append(Mutant(number, pos, op, old, new))
                pos = masked.find(old, pos + 1)
        for old, new in BOOL_LITERALS:
            for m in re.finditer(rf"\b{old}\b", masked):
                found.append(Mutant(number, m.start(), "BLR", old, new))
        for m in INT_LITERAL.finditer(masked):
            value = int(m.group(1))
            found.append(Mutant(number, m.start(), "CRP", m.group(1), str(value + 1)))
        for m in CONDITION_HEAD.finditer(masked):
            open_at = m.end() - 1
            close_at = _matching_paren(masked, open_at)
            if close_at < 0:
                continue
            inner = raw[open_at + 1:close_at]
            found.append(Mutant(number, open_at, "COR",
                                raw[open_at:close_at + 1], f"(!({inner}))"))
    return found


def apply(lines: list[str], mutant: Mutant) -> list[str]:
    out = list(lines)
    line = out[mutant.line - 1]
    assert line[mutant.col:mutant.col + len(mutant.old)] == mutant.old, mutant
    out[mutant.line - 1] = (line[:mutant.col] + mutant.new
                            + line[mutant.col + len(mutant.old):])
    return out


def _final_test_source(work_dir: Path, generated: Path) -> Path:
    """The source the scoring pass last compiled successfully."""
    for prefix in ("pruned_", "repaired_"):
        candidate = work_dir / f"{prefix}{generated.name}"
        if candidate.is_file():
            return candidate
    return generated


def _run_filtered(binary: Path, cases: list[str], work_dir: Path,
                  timeout: int = harness.RUN_TIMEOUT_S) -> harness.CommandResult:
    env = dict(harness.os.environ)
    env["LLVM_PROFILE_FILE"] = str(work_dir / "mutant.profraw")
    return harness.run(
        [str(binary), f"--gtest_filter={':'.join(cases)}"],
        work_dir, timeout, env=env,
    )


def _mutant_timeout(baseline_s: float) -> int:
    """How long a mutant may run before it counts as killed by non-termination.

    A mutated loop bound turns a millisecond test into an infinite one; the
    full run timeout would spend two minutes on each. Ten times the original
    run, with a floor for timer noise, is generous for any mutant that still
    terminates.
    """
    return int(min(harness.RUN_TIMEOUT_S, max(10, 10 * baseline_s)))


def _select(pool: list[Mutant], k: int, seed: str) -> list[Mutant]:
    if k <= 0 or len(pool) <= k:
        return pool
    rng = random.Random(seed)
    chosen = rng.sample(pool, k)
    return sorted(chosen, key=lambda m: (m.line, m.col, m.op))


def mutate_method(
    project: harness.BuiltProject, out_dir: Path, work_dir: Path,
    entry: dict, row, max_mutants: int,
) -> dict:
    focal = entry["focal"]
    record = {
        "project": row.project, "file": focal["file"], "method": focal["name"],
        "line": focal["start_line"], "end_line": focal["end_line"],
        "focal_cc": row.focal_cc, "touched_focal": row.touched_focal,
        "n_candidates": 0, "n_mutants": 0, "killed": 0, "survived": 0,
        "stillborn": 0, "timeouts": 0, "score": None, "status": "", "mutants": [],
    }
    cases = [name for name, verdict in (row.detail.get("cases") or {}).items()
             if verdict == "passed"]
    if not cases or not entry.get("test_file"):
        record["status"] = "no passing case"
        return record

    generated = out_dir / "generated" / entry["test_file"]
    test_src = _final_test_source(work_dir, generated)
    binary = work_dir / f"{generated.stem}_runner"
    if not test_src.is_file() or not binary.is_file():
        record["status"] = "scored artefacts missing"
        return record
    record["test_source"] = test_src.name

    mut_dir = work_dir / "mut"
    mut_dir.mkdir(exist_ok=True)
    focal_path = project.root / focal["file"]
    stale_backup = focal_path.with_name(focal_path.name + ".orig")
    if stale_backup.is_file():
        # A previous run was killed between writing a mutant over the
        # original and restoring it; the backup is the original.
        shutil.move(stale_backup, focal_path)

    t0 = time.monotonic()
    baseline = _run_filtered(binary, cases, mut_dir)
    baseline_s = time.monotonic() - t0
    if not baseline.ok:
        record["status"] = f"baseline exit {baseline.exit_code}"
        return record
    run_timeout = _mutant_timeout(baseline_s)
    record["baseline_s"] = round(baseline_s, 2)
    record["run_timeout_s"] = run_timeout

    original = focal_path.read_text(encoding="utf-8", errors="replace")
    lines = original.split("\n")
    pool = candidates(lines, focal["start_line"], focal["end_line"])
    record["n_candidates"] = len(pool)
    if not pool:
        record["status"] = "no mutation site"
        return record
    chosen = _select(pool, max_mutants, f"{focal['file']}:{focal['name']}:{focal['start_line']}")
    record["n_mutants"] = len(chosen)

    includes_source = harness._includes_source(
        test_src.read_text(errors="replace"), focal["file"])
    # A test that #includes the source must see the mutant at the original
    # path; every other test gets the mutant as a sibling file, so quoted
    # includes resolve exactly as they did for the original.
    mutant_path = focal_path if includes_source else focal_path.with_name(
        f"{focal_path.stem}.mutant{focal_path.suffix}")
    backup = stale_backup if includes_source else None
    if backup is not None:
        shutil.copy2(focal_path, backup)
    try:
        for index, mutant in enumerate(chosen):
            mutated = "\n".join(apply(lines, mutant))
            mutant_path.write_text(mutated, encoding="utf-8")
            mut_binary = mut_dir / f"{generated.stem}_m{index}"
            t0 = time.monotonic()
            compiled = harness.run(
                harness.compile_command(
                    project, test_src, mut_binary,
                    focal_source=None if includes_source else mutant_path),
                mut_dir, harness.COMPILE_TIMEOUT_S,
            )
            compile_s = round(time.monotonic() - t0, 1)
            item = {"key": mutant.key, "line": mutant.line, "op": mutant.op,
                    "old": mutant.old.strip(), "new": mutant.new.strip(),
                    "compile_s": compile_s}
            if not compiled.ok:
                item["status"] = "stillborn"
                item["stderr_head"] = compiled.stderr[:300]
                record["stillborn"] += 1
            else:
                t1 = time.monotonic()
                ran = _run_filtered(mut_binary, cases, mut_dir, run_timeout)
                item["run_s"] = round(time.monotonic() - t1, 1)
                item["exit_code"] = ran.exit_code
                if ran.exit_code == 124:
                    item["status"] = "timeout"
                    record["timeouts"] += 1
                    record["killed"] += 1
                elif ran.ok:
                    item["status"] = "survived"
                    record["survived"] += 1
                else:
                    item["status"] = "killed"
                    record["killed"] += 1
            mut_binary.unlink(missing_ok=True)
            record["mutants"].append(item)
    finally:
        if backup is not None:
            shutil.move(backup, focal_path)
        elif mutant_path.exists():
            mutant_path.unlink()
        (mut_dir / "mutant.profraw").unlink(missing_ok=True)

    scored = record["killed"] + record["survived"]
    record["score"] = round(100.0 * record["killed"] / scored, 2) if scored else None
    record["status"] = "ok" if scored else "all stillborn"
    return record


def summarize(records: list[dict]) -> dict:
    scored = [r for r in records if r.get("score") is not None]
    killed = sum(r["killed"] for r in scored)
    survived = sum(r["survived"] for r in scored)
    by_op: dict[str, list[int]] = {}
    for r in scored:
        for m in r["mutants"]:
            if m["status"] in ("killed", "survived", "timeout"):
                k = by_op.setdefault(m["op"], [0, 0])
                k[0] += m["status"] != "survived"
                k[1] += 1
    return {
        "methods": len(records),
        "methods_scored": len(scored),
        "methods_skipped": {s: sum(1 for r in records if r["status"] == s)
                            for s in sorted({r["status"] for r in records if r["status"] != "ok"})},
        "mutants": sum(r["n_mutants"] for r in records),
        "stillborn": sum(r["stillborn"] for r in records),
        "killed": killed, "survived": survived,
        "timeouts": sum(r["timeouts"] for r in scored),
        "score_pooled": round(100.0 * killed / (killed + survived), 2) if killed + survived else None,
        "score_mean": round(sum(r["score"] for r in scored) / len(scored), 2) if scored else None,
        "score_by_operator": {op: round(100.0 * k / n, 1) for op, (k, n) in sorted(by_op.items())},
        "mean_compile_s": round(sum(m["compile_s"] for r in records for m in r["mutants"])
                                / max(1, sum(len(r["mutants"]) for r in records)), 1),
    }


def main(args) -> int:
    from bench.cli import _read_manifest, _resolve_project  # noqa: PLC0415

    out_dir = Path(args.out).resolve()
    manifest = _read_manifest(out_dir)
    root = Path(args.project).resolve() if args.project else _resolve_project(manifest["project"])
    project = harness.load_built(root, Path(args.build_dir))
    if project is None:
        print(f"ERROR: no instrumented build under {args.build_dir}", file=sys.stderr)
        return 3
    print(f"{root.name}: linking against {[p.name for p in project.libraries]}", file=sys.stderr)

    rows = {(r.file, r.method, r.line): r for r in read_jsonl(out_dir / ROWS_NAME)}
    output = out_dir / OUTPUT_NAME
    records: list[dict] = []
    if args.resume and output.is_file():
        records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines() if line.strip()]
    done = {(r["file"], r["method"], r["line"]) for r in records}
    work_dir = out_dir / "work"

    todo = 0
    with output.open("a" if args.resume else "w", encoding="utf-8") as sink:
        for entry in manifest["entries"]:
            focal = entry["focal"]
            identity = (focal["file"], focal["name"], focal["start_line"])
            if identity in done:
                continue
            row = rows.get(identity)
            if row is None:
                continue
            if args.limit and todo >= args.limit:
                break
            todo += 1
            t0 = time.monotonic()
            record = mutate_method(project, out_dir, work_dir, entry, row, args.max_mutants)
            record["duration_s"] = round(time.monotonic() - t0, 1)
            records.append(record)
            done.add(identity)
            sink.write(json.dumps(record) + "\n")
            sink.flush()
            print(f"{focal['file']}:{focal['start_line']} {focal['name']}  "
                  f"mutants={record['n_mutants']} killed={record['killed']} "
                  f"survived={record['survived']} stillborn={record['stillborn']} "
                  f"score={record['score']} [{record['status']}] {record['duration_s']}s",
                  file=sys.stderr, flush=True)

    summary = summarize(records)
    (out_dir / "mutation-summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0
