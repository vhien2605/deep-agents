"""`python -m bench.cli` — run the CityWalk-style experiment end to end.

Four subcommands, each usable on its own:

    discover  list the focal methods that would be benchmarked, and why
    gen       run the agent once per focal method, writing test files
    eval      compile/run/measure whatever `gen` produced
    run       gen followed by eval

`gen` and `eval` are separate because generation is the expensive half: a
scoring change only requires re-running `eval` over the test files already on
disk.
"""

import argparse
import itertools
import json
import random
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from bench import harness, report, runner
from bench.discover import FocalMethod, discover
from bench.schema import Row, append_jsonl, read_jsonl, write_csv

MANIFEST_NAME = "manifest.json"
ROWS_NAME = "rows.jsonl"
CSV_NAME = "results.csv"
GENERATED_DIRNAME = "generated"


def _select(
    methods: list[FocalMethod],
    *,
    file_pattern: str,
    method_pattern: str,
    min_cc: int,
    max_per_file: int,
    limit: int,
    sample: int,
    seed: int,
) -> list[FocalMethod]:
    """Narrow the focal set. Filters compose; sampling happens last."""
    selected = methods
    if file_pattern:
        regex = re.compile(file_pattern)
        selected = [m for m in selected if regex.search(m.file)]
    if method_pattern:
        regex = re.compile(method_pattern)
        selected = [m for m in selected if regex.search(m.name)]
    if min_cc > 1:
        selected = [m for m in selected if m.complexity >= min_cc]
    if max_per_file > 0:
        per_file: dict[str, int] = {}
        capped = []
        for method in selected:
            count = per_file.get(method.file, 0)
            if count >= max_per_file:
                continue
            per_file[method.file] = count + 1
            capped.append(method)
        selected = capped
    if sample > 0 and sample < len(selected):
        selected = sorted(
            random.Random(seed).sample(selected, sample),
            key=lambda m: (m.file, m.start_line),
        )
    if limit > 0:
        selected = selected[:limit]
    return selected


def _add_selection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--filter-file", default="", help="Regex on the focal file path.")
    parser.add_argument("--filter-method", default="", help="Regex on the focal method name.")
    parser.add_argument("--min-cc", type=int, default=1,
                        help="Skip methods below this cyclomatic complexity.")
    parser.add_argument("--max-per-file", type=int, default=0,
                        help="Cap methods taken from any one file (0 = no cap).")
    parser.add_argument("--limit", type=int, default=0, help="Keep only the first N methods.")
    parser.add_argument("--sample", type=int, default=0,
                        help="Randomly sample N methods (applied before --limit).")
    parser.add_argument("--seed", type=int, default=42, help="Seed for --sample.")
    parser.add_argument("--citywalk-focal", action="store_true",
                        help="Restrict the focal set the way CITYWALK does: drop "
                             "private/protected members and the files its evaluator "
                             "skips by name. Free functions are kept.")
    parser.add_argument("--paper-sample", action="store_true",
                        help="Draw as many methods as Table 3 of the paper reports "
                             "for this project. The paper does not publish which "
                             "methods it chose, so the draw is seeded instead.")


def _selection_from(args) -> list[FocalMethod]:
    project = Path(args.project)
    methods, strategy = discover(project, min_lines=args.min_lines)
    print(f"discovery: {len(methods)} methods via {strategy}", file=sys.stderr)

    if getattr(args, "citywalk_focal", False):
        from bench.discover import citywalk_excluded_file, class_access_index, is_citywalk_focal

        access = class_access_index(project)
        before = len(methods)
        methods = [
            m for m in methods
            if not citywalk_excluded_file(project.resolve().name, m.file)
            and is_citywalk_focal(m.name, access)
        ]
        print(f"citywalk focal set: {len(methods)}/{before}", file=sys.stderr)

    sample = args.sample
    if getattr(args, "paper_sample", False):
        from bench import projects as project_config

        count = project_config.paper_focal_count(project.resolve().name)
        if count is None:
            print(f"warning: no paper count for {project.name}; sampling disabled",
                  file=sys.stderr)
        else:
            sample = count
            print(f"paper sample: drawing {count} methods (Table 3)", file=sys.stderr)

    selected = _select(
        methods,
        file_pattern=args.filter_file,
        method_pattern=args.filter_method,
        min_cc=args.min_cc,
        max_per_file=args.max_per_file,
        limit=args.limit,
        sample=sample,
        seed=args.seed,
    )
    print(f"selected:  {len(selected)} methods", file=sys.stderr)
    return selected


def cmd_discover(args) -> int:
    selected = _selection_from(args)
    if args.json:
        print(json.dumps([asdict(m) for m in selected], indent=2))
    else:
        for method in selected:
            print(f"{method.file}:{method.start_line}\t{method.name}\tcc={method.complexity}")
    return 0


def _write_manifest(
    out_dir: Path, project: Path, entries: list[dict], config: dict | None = None
) -> None:
    """Persist the sweep so `eval` can run later, elsewhere.

    The project path is stored relative to the working directory when possible:
    a sweep generated on the host and scored in the container would otherwise
    carry a `D:\\...` path that means nothing on the other side of the mount.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        stored = project.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        stored = str(project)
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(
            {"project": stored, "config": config or {}, "entries": entries}, indent=2
        ),
        encoding="utf-8",
    )


def _resolve_project(raw: str) -> Path:
    """Interpret a manifest's project path on whatever machine reads it."""
    path = Path(raw)
    if path.is_dir():
        return path.resolve()
    # An absolute path from another OS: keep only the tail and look for it here.
    tail = Path(raw.replace("\\", "/")).name
    candidate = Path.cwd() / "data" / tail
    if candidate.is_dir():
        return candidate.resolve()
    raise SystemExit(
        f"project path from the manifest does not exist here: {raw}\n"
        "Pass --project to point at it explicitly."
    )


def _read_manifest(out_dir: Path) -> dict:
    path = out_dir / MANIFEST_NAME
    if not path.is_file():
        raise SystemExit(f"no manifest at {path} — run `gen` first, or point --out at its output")
    return json.loads(path.read_text(encoding="utf-8"))


# Files that need not be copied into a worker's private tree: version control
# metadata and the build output of whoever ran the project before.
_WORKSPACE_IGNORE = shutil.ignore_patterns(
    ".git", "build", "build_*", "_build", "cmake-build-*", "*.o", "*.a",
    "*.gcda", "*.gcno", "bench_generated_tests",
)


def _workspace_root(out_dir: Path) -> Path:
    """Where worker copies live.

    Container-local storage when available, not the sweep directory: that
    directory is a bind mount from the host, where copying a project and then
    reading it back file-by-file is an order of magnitude slower than local
    disk. The copies are disposable, so nothing is lost by keeping them off the
    mount.
    """
    local = Path("/tmp/bench-workspaces")
    try:
        local.mkdir(parents=True, exist_ok=True)
        return local
    except OSError:
        return out_dir / "workers"


def _worker_workspace(out_dir: Path, project: Path, worker_id: int) -> Path:
    """A private copy of the project for one worker.

    Workers each get their own tree because the agent's shell backend is rooted
    at the project: two agents running a build in the same directory would
    overwrite each other's objects. Copying is cheap next to the cost of a
    generation run, and it makes parallel `gen` safe by construction rather
    than by trusting the agent not to build.
    """
    workspace = _workspace_root(out_dir) / f"w{worker_id}" / project.name
    if not workspace.exists():
        workspace.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(project, workspace, ignore=_WORKSPACE_IGNORE, symlinks=True)
    return workspace


def _failed_from_manifest(
    out_dir: Path, cause: str = ""
) -> list[tuple[int, FocalMethod]]:
    """(index, method) for every focal method a previous sweep failed to write.

    A retry has to reuse the original index so the regenerated file lands on the
    same path and replaces the old row rather than creating a duplicate.

    `cause` narrows the retry to failures whose recorded error matches it. The
    two failure modes are not equivalent: a request that timed out carries no
    information about the method, while a run that exhausted its step budget is
    the bounded-agent design doing what it promises. Retrying the first repairs
    the measurement; retrying the second would quietly relax the budget.
    """
    manifest = _read_manifest(out_dir)
    pattern = re.compile(cause, re.IGNORECASE) if cause else None
    out: list[tuple[int, FocalMethod]] = []
    for entry in manifest["entries"]:
        if entry.get("wrote_file"):
            continue
        if pattern and not pattern.search(entry.get("error") or ""):
            continue
        out.append((entry["index"], FocalMethod(**entry["focal"])))
    return out


def cmd_gen(args) -> int:
    project = Path(args.project).resolve()
    args.no_agent_fix = getattr(args, "no_agent_fix", False)
    out_dir = Path(args.out).resolve()

    retry_pairs: list[tuple[int, FocalMethod]] = []
    if getattr(args, "retry_failed", False):
        # Failures are not random: measured across sweeps, the methods that time
        # out or exhaust the step budget have several times the cyclomatic
        # complexity of those that succeed, so leaving them out biases every
        # rate upward. Retrying them is what keeps the sample honest.
        retry_pairs = _failed_from_manifest(
            out_dir, getattr(args, "retry_cause", "") or ""
        )
        if not retry_pairs:
            print("no failed methods to retry", file=sys.stderr)
            return 0
        selected = [m for _, m in retry_pairs]
        print(f"retrying {len(selected)} previously failed methods", file=sys.stderr)
    else:
        selected = _selection_from(args)
    if not selected:
        print("nothing selected", file=sys.stderr)
        return 1

    # Imported here so `discover`/`eval` work without LLM configuration.
    from src import create_cpp_unittest_agent
    from src.config import (
        DEFAULT_RECURSION_LIMIT, LLM_MODEL, LLM_TEMPERATURE, LLM_TIMEOUT_SECONDS,
    )

    allow_build = not args.no_agent_fix
    agent = create_cpp_unittest_agent(
        project, coverage_target=args.coverage, with_build_tools=allow_build
    )
    recursion_limit = args.recursion_limit or DEFAULT_RECURSION_LIMIT
    # Recorded with the sweep so the table can state the sampling conditions it
    # was produced under — the numbers are meaningless without them.
    sweep_config = {
        "model_requested": LLM_MODEL,
        "temperature": LLM_TEMPERATURE,
        "request_timeout_s": LLM_TIMEOUT_SECONDS,
        "recursion_limit": recursion_limit,
        "agent_may_build": allow_build,
        "repair_rounds": getattr(args, "repair_rounds", 0),
        "selection": {
            "filter_file": args.filter_file, "filter_method": args.filter_method,
            "min_cc": args.min_cc, "max_per_file": args.max_per_file,
            "limit": args.limit, "sample": args.sample, "seed": args.seed,
            "min_lines": args.min_lines,
            # Recorded separately from `sample`: these two change which methods
            # are in the set, so a table cannot be compared against another
            # without knowing whether they were on.
            "citywalk_focal": getattr(args, "citywalk_focal", False),
            "paper_sample": getattr(args, "paper_sample", False),
            "selected_count": len(selected),
        },
    }

    generated_dir = out_dir / GENERATED_DIRNAME
    generated_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    if retry_pairs:
        # Keep every existing row, not only the successes. The manifest is
        # rewritten at each checkpoint, so seeding it with the successes alone
        # erases the failures that have not been retried yet — and a pass this
        # long does get interrupted. Losing them is silent: the rows vanish
        # from results.csv and the next --retry-failed finds nothing to do.
        # Each retried method replaces its own row by index instead.
        entries = list(_read_manifest(out_dir)["entries"])
    indices = [i for i, _ in retry_pairs] if retry_pairs else list(range(len(selected)))
    started = time.monotonic()
    workers = max(1, args.workers)
    lock = threading.Lock()
    done = 0

    # Worker 0 uses the project itself; the rest get private copies. One agent
    # per workspace, since an agent is bound to the tree it was built for.
    local = threading.local()
    worker_ids = itertools.count()
    # Private copies exist so two agents cannot run a build in the same tree.
    # Without the build tools there is nothing to collide over — each method
    # writes its own uniquely named test file — so every worker shares the
    # project directly. That saves a copy per worker, which for mlx at 319 MB
    # and 32 workers would be 10 GB of disk and minutes of start-up.
    share_workspace = workers == 1 or not allow_build
    workspaces: dict[int, tuple[Path, object]] = (
        {0: (project, agent)} if share_workspace else {}
    )

    def agent_for(worker_id: int) -> tuple[Path, object]:
        if share_workspace:
            return workspaces[0]
        if worker_id not in workspaces:
            workspace = _worker_workspace(out_dir, project, worker_id)
            workspaces[worker_id] = (
                workspace,
                create_cpp_unittest_agent(
                    workspace, coverage_target=args.coverage,
                    with_build_tools=allow_build,
                ),
            )
        return workspaces[worker_id]

    def run_one(item: tuple[int, object]) -> dict:
        nonlocal done
        position, focal = item
        index = indices[position]
        if not hasattr(local, "worker_id"):
            # First task on this thread: claim an id, and with it a workspace.
            # ThreadPoolExecutor reuses threads, so this happens at most
            # `workers` times.
            with lock:
                local.worker_id = next(worker_ids)
                local.workspace, local.agent = agent_for(local.worker_id)
        result = runner.generate_for_method(
            local.agent, local.workspace, focal, index,
            recursion_limit=recursion_limit, allow_build=allow_build,
        )
        stored = generated_dir / result.output_path.name
        if result.wrote_file:
            shutil.copy2(result.output_path, stored)
        entry = {
            "focal": asdict(focal),
            "index": index,
            "test_file": stored.name if result.wrote_file else "",
            "wrote_file": result.wrote_file,
            "total_tokens": result.total_tokens,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "duration_s": result.duration_s,
            "fix_phase": result.fix_phase,
            "model": result.model,
            "error": result.error,
            "tool_calls": result.tool_calls,
        }
        status = "ok" if result.wrote_file else f"NO FILE ({result.error or 'agent wrote nothing'})"
        with lock:
            # Replace by index rather than append: on a retry the manifest
            # already carries this method's failed row.
            entries[:] = [e for e in entries if e["index"] != index]
            entries.append(entry)
            done += 1
            # Checkpoint after every run: a sweep this long must survive being
            # killed. Entries are sorted so the manifest order is the focal
            # order regardless of which worker finished first.
            entries.sort(key=lambda e: e["index"])
            _write_manifest(out_dir, project, entries, sweep_config)
            print(
                f"[{done}/{len(selected)}] w{local.worker_id} "
                f"{focal.file}:{focal.start_line} {focal.name}\n"
                f"    -> {status}  tokens={result.total_tokens} {result.duration_s}s "
                f"fix_rounds={result.fix_phase}",
                file=sys.stderr, flush=True,
            )
        return entry

    if workers == 1:
        for item in enumerate(selected):
            run_one(item)
    else:
        print(f"generating with {workers} workers", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(run_one, enumerate(selected)))

    elapsed = round(time.monotonic() - started, 1)
    wrote = sum(1 for e in entries if e["wrote_file"])
    print(f"gen done: {wrote}/{len(entries)} files in {elapsed}s -> {out_dir}", file=sys.stderr)
    return 0


def cmd_eval(args) -> int:
    out_dir = Path(args.out).resolve()
    args.repair_rounds = getattr(args, "repair_rounds", 0)
    manifest = _read_manifest(out_dir)
    project = Path(args.project).resolve() if args.project else _resolve_project(manifest["project"])
    entries = manifest["entries"]
    shard_k = shard_n = None
    if getattr(args, "shard", ""):
        shard_k, shard_n = (int(x) for x in args.shard.split("/"))
        entries = [e for i, e in enumerate(entries) if i % shard_n == shard_k]
        print(f"shard {shard_k}/{shard_n}: {len(entries)} entries", file=sys.stderr)

    missing = [name for name, path in harness.toolchain_report().items() if path is None]
    if missing:
        print(f"ERROR: missing toolchain: {', '.join(missing)}", file=sys.stderr)
        print("Run this inside the benchmark container (see bench/Dockerfile).", file=sys.stderr)
        return 2
    if not harness.gtest_available():
        print("ERROR: GoogleTest headers not found", file=sys.stderr)
        return 2

    repair_model = None
    if args.repair_rounds > 0:
        from src.llm import build_chat_model
        # A bare chat model, not an agent: the repair round must be exactly one
        # call with the file and the diagnostics, as the paper specifies.
        repair_model = build_chat_model()
        print(f"repair: up to {args.repair_rounds} LLM round(s) per failing file",
              file=sys.stderr)

    work_dir = out_dir / ("work" if shard_k is None else f"work-shard{shard_k}")
    build_dir = Path(args.build_dir).resolve() if args.build_dir else work_dir / "build"
    print(f"building {project.name} with coverage instrumentation ...", file=sys.stderr)
    project_build, last = harness.build_instrumented(
        project, build_dir, cxx_standard=args.std, jobs=args.jobs,
        extra_cmake_args=args.cmake_arg or [],
    )
    if project_build is None:
        print(f"ERROR: instrumented build failed\n{last.output[-3000:]}", file=sys.stderr)
        return 3
    print(f"  linked libraries: {[p.name for p in project_build.libraries]}", file=sys.stderr)

    rows_path = out_dir / (ROWS_NAME if shard_k is None
                           else f"rows.shard{shard_k}.jsonl")
    done = {(r.file, r.method, r.line) for r in read_jsonl(rows_path)} if args.resume else set()
    if not args.resume:
        rows_path.unlink(missing_ok=True)

    rows: list[Row] = read_jsonl(rows_path) if args.resume else []
    generated_dir = out_dir / GENERATED_DIRNAME

    for entry in entries:
        focal = entry["focal"]
        identity = (focal["file"], focal["name"], focal["start_line"])
        if identity in done:
            continue
        label = f"{focal['file']}:{focal['start_line']} {focal['name']}"
        row = Row(
            project=project.name,
            file=focal["file"],
            method=focal["name"],
            line=focal["start_line"],
            model=entry.get("model", ""),
            total_tokens=entry.get("total_tokens", 0),
            duration_s=entry.get("duration_s", 0.0),
            fix_phase=entry.get("fix_phase", 0),
            focal_cc=focal["complexity"],
        )
        test_file = generated_dir / entry["test_file"] if entry.get("test_file") else None
        if test_file is None or not test_file.is_file():
            row.error_category = "no_output"
            row.error_first_line = entry.get("error", "") or "agent produced no test file"
        else:
            evaluated = harness.evaluate_test_file(
                project_build, test_file, focal["file"], focal["start_line"],
                focal["end_line"], work_dir,
                repair_model=repair_model, max_repair_rounds=args.repair_rounds,
            )
            row.n_cases = evaluated.n_cases
            row.n_cases_compiled = evaluated.n_cases_compiled
            row.n_cases_passed = evaluated.n_cases_passed
            row.line_coverage = evaluated.line_coverage
            row.branch_coverage = evaluated.branch_coverage
            row.touched_focal = evaluated.touched_focal
            row.error_category = evaluated.error_category
            row.error_first_line = evaluated.error_first_line
            row.test_loc = evaluated.test_loc
            row.test_cc = evaluated.test_cc
            row.uses_mock = evaluated.uses_mock
            row.detail = evaluated.detail
        row.csr = round(row.n_cases_compiled / row.n_cases, 4) if row.n_cases else 0.0
        row.epr = round(row.n_cases_passed / row.n_cases_compiled, 4) if row.n_cases_compiled else 0.0

        rows.append(row)
        append_jsonl(row, rows_path)
        print(
            f"{label}\n    cases={row.n_cases} compiled={row.n_cases_compiled} "
            f"passed={row.n_cases_passed} line={row.line_coverage}% "
            f"branch={row.branch_coverage}% {row.error_category or 'ok'}",
            file=sys.stderr, flush=True,
        )

    csv_path = out_dir / CSV_NAME
    write_csv(rows, csv_path)
    _print_summary(rows)
    print(f"\nwrote {csv_path}", file=sys.stderr)
    return 0


def _print_summary(rows: list[Row]) -> None:
    if not rows:
        return
    total_cases = sum(r.n_cases for r in rows)
    total_compiled = sum(r.n_cases_compiled for r in rows)
    total_passed = sum(r.n_cases_passed for r in rows)
    measured = [r for r in rows if r.n_cases_passed > 0]
    print("\n" + "=" * 60, file=sys.stderr)
    print(f"methods            : {len(rows)}", file=sys.stderr)
    print(f"cases written      : {total_cases}", file=sys.stderr)
    print(
        f"CSR (compiled/written): {total_compiled}/{total_cases} = "
        f"{100.0 * total_compiled / total_cases:.1f}%" if total_cases else "CSR: n/a",
        file=sys.stderr,
    )
    print(
        f"EPR (passed/compiled) : {total_passed}/{total_compiled} = "
        f"{100.0 * total_passed / total_compiled:.1f}%" if total_compiled else "EPR: n/a",
        file=sys.stderr,
    )
    if measured:
        line_avg = sum(r.line_coverage for r in measured) / len(measured)
        touched = sum(1 for r in measured if r.touched_focal)
        print(f"avg line coverage  : {line_avg:.1f}% (over {len(measured)} runnable rows)",
              file=sys.stderr)
        # A branchless method scores 0% branch coverage by arithmetic, not by
        # any failure of the tests; averaging those in understates the result,
        # so they are excluded and counted separately.
        branchy = [
            r for r in measured
            if (r.detail.get("coverage") or {}).get("branches_found", 0) > 0
        ]
        if branchy:
            branch_avg = sum(r.branch_coverage for r in branchy) / len(branchy)
            print(f"avg branch coverage: {branch_avg:.1f}% "
                  f"(over {len(branchy)} methods that have branches)", file=sys.stderr)
        else:
            print("avg branch coverage: n/a (no method under test has branches)",
                  file=sys.stderr)
        print(f"touched focal      : {touched}/{len(measured)}", file=sys.stderr)
    print(f"tokens             : {sum(r.total_tokens for r in rows):,}", file=sys.stderr)
    print(f"wall clock (gen)   : {sum(r.duration_s for r in rows):.0f}s", file=sys.stderr)


def cmd_report(args) -> int:
    """Render the summary tables from a finished (or partial) sweep."""
    out_dir = Path(args.out).resolve()
    rows = read_jsonl(out_dir / ROWS_NAME)
    if not rows:
        raise SystemExit(f"no rows in {out_dir / ROWS_NAME} — run `eval` first")
    config = {}
    manifest_path = out_dir / MANIFEST_NAME
    if manifest_path.is_file():
        config = json.loads(manifest_path.read_text(encoding="utf-8")).get("config", {})

    if getattr(args, "citywalk_focal", False):
        # Reproduce CITYWALK's focal set as closely as its published parser
        # allows: a focal *class file* (one with a matching header), and every
        # definition in it except private and protected members — their
        # pre_parser.py skips exactly those two access levels and keeps free
        # functions. Applied to rows already measured; costs no tokens.
        from bench.discover import (citywalk_excluded_file, class_access_index,
                                     is_citywalk_focal)

        project = (Path(args.project).resolve() if getattr(args, "project", "")
                   else _resolve_project(json.loads(
                       manifest_path.read_text(encoding="utf-8"))["project"]))
        access = class_access_index(project)
        before = len(rows)
        rows = [
            r for r in rows
            if not citywalk_excluded_file(project.name, r.file)
            and is_citywalk_focal(r.method, access)
        ]
        if not rows:
            raise SystemExit("no rows left after the CITYWALK focal filter")
        print(f"citywalk-focal: {len(rows)}/{before} rows kept", file=sys.stderr)
        config = {**config, "focal_set": (
            "CITYWALK-equivalent (per their pre_parser.py and evaluator.py): "
            "private/protected members excluded, free functions kept, "
            f"unicode*/perl_groups* files skipped — {len(rows)} of {before}")}

    if getattr(args, "public_only", False):
        # Narrow to the focal set a published benchmark would have used, so the
        # numbers are comparable. Applied to rows already measured — no
        # regeneration, no further model calls.
        from bench.discover import is_public_class_method, public_method_index

        project = (Path(args.project).resolve() if getattr(args, "project", "")
                   else _resolve_project(json.loads(
                       manifest_path.read_text(encoding="utf-8"))["project"]))
        index = public_method_index(project)
        before = len(rows)
        rows = [r for r in rows if is_public_class_method(r.method, index)]
        if not rows:
            raise SystemExit("no public class methods among the measured rows")
        print(f"public-only: {len(rows)}/{before} rows kept", file=sys.stderr)
        config = {**config, "focal_set": (
            "public class methods only (free functions and private helpers "
            f"excluded): {len(rows)} of {before}")}
    text = report.render_markdown(rows, config)
    destination = Path(args.output) if args.output else out_dir / "summary.md"
    destination.write_text(text, encoding="utf-8")
    print(text)
    print(f"\nwrote {destination}", file=sys.stderr)
    return 0


def cmd_run(args) -> int:
    code = cmd_gen(args)
    if code:
        return code
    code = cmd_eval(args)
    if code:
        return code
    # A sweep is only useful once summarised, so `run` finishes the job.
    args.output = ""
    return cmd_report(args)


