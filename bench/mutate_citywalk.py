"""Mutation analysis under the source study's protocol, on our test suite.

CITYWALK measures oracle quality on two single-file projects by mutating
the whole file with universalmutator and scoring the generated suite as a
whole: a mutant counts as killed when the suite detects it, and mutants
that do not compile are excluded as invalid. This module reproduces that
protocol exactly and substitutes our tests for theirs, so the two mutation
scores answer the same question.

Differences from `bench.mutate`, which scores each method against its own
test alone:

* the mutant population is every line of the focal files, not the focal
  methods' line ranges;
* the operator set is universalmutator's, which adds statement deletion,
  control-structure and string mutations to ours;
* any test in the project may kill a mutant, not only the one written for
  the method that contains it.

Two optimisations make it affordable. A mutant is killed by the first test
that detects it, so the remaining tests are not run; that one is exact. The
other is that a test whose profile does not reach the mutated line cannot
kill the mutant, since the mutation is the only difference between the two
programs and a run that never reaches it takes the same path. That is exact
only if the profile is read completely, and the first version of this file
did not read it completely: a macro body executes where the macro is used,
so the `#define` line carries no segment of its own and every mutant inside
a macro was recorded as an unreachable survivor. On json.cpp that covered
3,826 mutants, 41 % of the valid population, and a hand check found one in
eight of them killable after all. Expanded regions are now read alongside
the segments, and `--verify-uncovered` runs the whole suite against the
mutants the filter still calls unreachable, so the claim can be tested
rather than trusted.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bench import harness, metrics, projects
from bench.schema import read_jsonl

OUTPUT_NAME = "mutation-citywalk.jsonl"
SUMMARY_NAME = "mutation-citywalk-summary.json"
COVER_NAME = "mutation-cover.json"


# --------------------------------------------------------------------------- #
# Compiling pieces once
# --------------------------------------------------------------------------- #

def _base_flags(project: harness.BuiltProject) -> list[str]:
    config = projects.config_for(project.root.name)
    cmd = [harness.CXX, f"-std={project.cxx_standard}", *harness.COVERAGE_FLAGS,
           *config.extra_cxx_flags]
    for directory in project.include_dirs:
        cmd += ["-I", str(directory)]
    return cmd


def _link_flags(project: harness.BuiltProject) -> list[str]:
    cmd: list[str] = []
    if project.libraries:
        cmd += ["-Wl,--start-group", *[str(p) for p in project.libraries],
                "-Wl,--end-group"]
    cmd += ["-lgtest", "-lgtest_main"]
    if harness._gmock_available():
        cmd += ["-lgmock"]
    cmd += ["-lpthread", *project.extra_link_flags]
    return cmd


def compile_object(project: harness.BuiltProject, source: Path, obj: Path,
                   cwd: Path) -> harness.CommandResult:
    return harness.run(_base_flags(project) + ["-c", str(source), "-o", str(obj)],
                       cwd, harness.COMPILE_TIMEOUT_S)


def link_binary(project: harness.BuiltProject, objects: list[Path], binary: Path,
                cwd: Path) -> harness.CommandResult:
    return harness.run(
        _base_flags(project) + [str(o) for o in objects] + _link_flags(project)
        + ["-o", str(binary)],
        cwd, harness.COMPILE_TIMEOUT_S)


# --------------------------------------------------------------------------- #
# What each test covers
# --------------------------------------------------------------------------- #

def _covered_lines(binary: Path, cases: list[str], focal_files: list[str],
                   root: Path, work: Path, extent: dict[str, int]) -> dict[str, set[int]]:
    """Lines of each focal file the binary executes, running the given cases.

    The counts come from `metrics._llvm_line_counts`, llvm-cov's own segment
    walk, and not from the segment list directly: a segment records where a
    region starts, so a line in the middle of a multi-line statement carries
    none of its own and would be read as unexecuted. Treating such a line as
    uncovered would let a mutant on it be scored as surviving without ever
    running the test that in fact executes it.
    """
    profraw = work / f"{binary.stem}.cover.profraw"
    profraw.unlink(missing_ok=True)
    env = dict(harness.os.environ)
    env["LLVM_PROFILE_FILE"] = str(profraw)
    harness.run([str(binary), f"--gtest_filter={':'.join(cases)}"],
                work, harness.RUN_TIMEOUT_S, env=env)
    if not profraw.is_file():
        return {}
    profdata = work / f"{binary.stem}.cover.profdata"
    merged = harness.run(["llvm-profdata", "merge", "-sparse", "-o", str(profdata),
                          str(profraw)], work, 120)
    out: dict[str, set[int]] = {}
    if merged.ok:
        exported = harness.run(
            ["llvm-cov", "export", str(binary), f"-instr-profile={profdata}",
             *[str(root / f) for f in focal_files]], work, 300)
        if exported.ok and exported.stdout.strip():
            try:
                report = json.loads(exported.stdout)
            except json.JSONDecodeError:
                report = {}
            for entry in (report.get("data") or [{}])[0].get("files", []) or []:
                name = Path(entry.get("filename", "")).as_posix()
                rel = next((f for f in focal_files if name.endswith(f)), None)
                if rel is None:
                    continue
                counts = metrics._llvm_line_counts(
                    entry.get("segments", []), 1, extent.get(rel, 1))
                hit = {line for line, count in counts.items() if count > 0}
                # A macro body executes where the macro is used, not where it
                # is defined, so the segments never mark the `#define` line as
                # run. llvm-cov records the expanded regions separately: each
                # expansion carries the regions of the macro body with the
                # count they reached at that use site. Without them a mutant
                # inside a macro looks unreachable and is scored as surviving
                # although a test does execute it.
                for expansion in entry.get("expansions", []) or []:
                    for region in expansion.get("target_regions") or []:
                        if len(region) > 4 and region[4] > 0:
                            hit.update(range(region[0], region[2] + 1))
                out.setdefault(rel, set()).update(hit)
        profdata.unlink(missing_ok=True)
    profraw.unlink(missing_ok=True)
    return out


# --------------------------------------------------------------------------- #
# Mutants
# --------------------------------------------------------------------------- #

def restore_source(source: Path) -> bool:
    """Put the original back if a killed run left a mutant in its place.

    universalmutator copies the source aside as `<name>.um.backup.<pid>`
    before swapping a mutant in, and restores it in a `finally`. A SIGKILL
    skips that, so the mutant stays. Every backup is a copy of the same
    original, so restoring any of them is correct.

    Only this file's own backups are touched. The tool's scratch files are
    named after the running process, not the source, and several instances
    share a source directory, so removing them here would delete the file a
    concurrent instance is about to copy into place -- which is what happened
    to yaml-cpp, whose 33 focal files sit in one directory. They are swept
    once before any instance starts instead.
    """
    backups = sorted(source.parent.glob(source.name + ".um.backup.*"))
    for backup in backups:
        shutil.copyfile(backup, source)
        backup.unlink()
    return bool(backups)


def sweep_scratch(directories: set[Path]) -> int:
    """Delete universalmutator's leftover scratch files. Never run concurrently."""
    removed = 0
    for directory in directories:
        for pattern in (".tmp_mutant.*", ".um.mutant_output.*"):
            for junk in directory.glob(pattern):
                junk.unlink(missing_ok=True)
                removed += 1
    return removed


def sample_lines(root: Path, focal_files: list[str], budget: int,
                 seed: int) -> dict[str, list[int]]:
    """A uniform random sample of `budget` lines drawn across the focal files.

    The unit sampled is the line rather than the mutant, because the tool
    filters by line and so only pays for the lines it is given. Every line of
    every focal file has the same chance of being drawn, so the mutants on
    the drawn lines are a cluster sample of the mutant population and the
    ratio killed/valid over them estimates the ratio over all of it. The
    clusters are lines, so the confidence interval must be resampled over
    lines, not over mutants.
    """
    population = [(rel, n) for rel in focal_files
                  for n in range(1, 1 + len((root / rel).read_text(
                      encoding="utf-8", errors="replace").split("\n")))]
    rng = random.Random(seed)
    drawn = population if budget >= len(population) else rng.sample(population, budget)
    out: dict[str, list[int]] = {}
    for rel, line in drawn:
        out.setdefault(rel, []).append(line)
    return {rel: sorted(v) for rel, v in sorted(out.items())}


def generate_mutants(project: harness.BuiltProject, source: Path,
                     mutant_dir: Path, lines: list[int] | None = None) -> list[Path]:
    """universalmutator's valid mutants for one file, the tool as shipped.

    The tool is given the project's own compile command, so it applies its
    own definitions of invalid (does not compile) and redundant (identical
    to another mutant) rather than ours. Running it with `--noCheck` and
    compile-checking afterwards is not the same thing: `--noCheck` also
    disables the dead-code heuristic that runs before the check, which
    changes which mutants exist at all and therefore the denominator the
    mutation score is taken over.
    """
    mutant_dir.mkdir(parents=True, exist_ok=True)
    stamp = mutant_dir / "generation.json"
    if stamp.is_file():
        return sorted(p for p in mutant_dir.glob("*.mutant.*"))

    # universalmutator 1.14.1 accepts the check command in two forms. The one
    # that names MUTANT crashes: `cmdHandler` leaves `backupName` at None on
    # that path and then calls os.remove on it, catching only OSError. The
    # other form -- a command that compiles the source file, with the mutant
    # copied over it and restored afterwards -- runs the same check through
    # the tool's supported path, so it is used instead of patching the tool.
    # Before anything reads the source: the tool swaps each mutant into it to
    # run the check, so a run killed mid-check leaves a mutant on disk with
    # the original beside it as a backup. Restoring has to happen here, not
    # after generating, or the mutant would be taken for the original and
    # every mutant derived from it -- and every later measurement of this
    # project -- would carry that first mutation.
    restore_source(source)

    check = " ".join(_base_flags(project)
                     + ["-c", source.name, "-o", str(mutant_dir / "check.o")])
    assert "MUTANT" not in check, "check command must not name MUTANT"
    argv = ["mutate", source.name, "--cmd", check, "--mutantDir", str(mutant_dir)]
    if lines is not None:
        line_file = mutant_dir / "lines.txt"
        line_file.write_text("\n".join(str(n) for n in lines), encoding="utf-8")
        argv += ["--lines", str(line_file)]
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    started = time.monotonic()
    result = subprocess.run(
        argv, cwd=source.parent, capture_output=True, text=True, errors="replace",
        timeout=86400,
    )
    restore_source(source)
    if hashlib.sha256(source.read_bytes()).hexdigest() != before:
        raise SystemExit(f"{source} was left modified by mutant generation")
    mutants = sorted(p for p in mutant_dir.glob("*.mutant.*"))
    tail = [l for l in result.stdout.splitlines() if "MUTANTS" in l or "Percentage" in l]
    # No mutant is a real answer, not a failure: every candidate on the
    # sampled lines can be dead code or a comment, which the tool filters
    # before its check. yaml-cpp's graphbuilder.cpp is one such file. Only a
    # tool that never reported at all has actually failed.
    if not mutants and not any("GENERATED BY RULES" in l for l in tail):
        raise SystemExit(f"universalmutator failed on {source}:\n"
                         f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}")
    stamp.write_text(json.dumps({
        "source": source.name, "valid_mutants": len(mutants),
        "tool_report": tail, "check_command": check,
        "sampled_lines": len(lines) if lines is not None else None,
        "generation_s": round(time.monotonic() - started, 1)}, indent=1), encoding="utf-8")
    print(f"  universalmutator: {'; '.join(tail)}", file=sys.stderr)
    (mutant_dir / "check.o").unlink(missing_ok=True)
    return mutants


def mutated_line(original: list[str], mutant: Path) -> int | None:
    """The 1-based line universalmutator changed, or None if it changed none."""
    other = mutant.read_text(encoding="utf-8", errors="replace").split("\n")
    for i, (a, b) in enumerate(zip(original, other)):
        if a != b:
            return i + 1
    return None if len(original) == len(other) else min(len(original), len(other)) + 1


# --------------------------------------------------------------------------- #
# Scoring one mutant
# --------------------------------------------------------------------------- #

class Scorer:
    def __init__(self, project, out_dir, focal_files, tests, cover, work,
                 verify_uncovered=False):
        self.verify_uncovered = verify_uncovered
        self.project = project
        self.out_dir = out_dir
        self.focal_files = focal_files
        self.tests = tests          # list of dicts: name, obj, focal_file, cases
        self.cover = cover          # test name -> {focal file: covered lines}
        self.work = work
        self.focal_objects = {}     # rel path -> original .o

    def candidates_for(self, rel: str, line: int) -> list[dict]:
        """Tests that execute the mutated line, the owning method's test first."""
        hits = [t for t in self.tests
                if line in self.cover.get(t["name"], {}).get(rel, ())]
        hits.sort(key=lambda t: (t["focal_file"] != rel,
                                 not (t["start"] <= line <= t["end"])))
        return hits

    def score(self, rel: str, mutant: Path, line: int, slot: int) -> dict:
        scratch = self.work / f"slot{slot}"
        scratch.mkdir(parents=True, exist_ok=True)
        record = {"file": rel, "mutant": mutant.name, "line": line,
                  "status": "", "tests_run": 0, "killed_by": ""}
        mutant_obj = scratch / "mutant.o"
        t0 = time.monotonic()
        compiled = compile_object(self.project, mutant, mutant_obj, scratch)
        record["compile_s"] = round(time.monotonic() - t0, 1)
        if not compiled.ok:
            record["status"] = "invalid"
            return record

        pool = self.candidates_for(rel, line)
        if not pool:
            # No profile in the suite reaches the line. Declaring the mutant a
            # survivor here rests on the profile being complete, so when asked
            # the claim is checked by running the suite anyway; whatever that
            # costs, it is the only way the reachability filter can be shown
            # not to be manufacturing survivors.
            record["reason"] = "no covering test"
            if not self.verify_uncovered:
                record["status"] = "survived"
                return record
            pool = self.tests
            record["verified"] = True

        binary = scratch / "runner"
        env = dict(harness.os.environ)
        env["LLVM_PROFILE_FILE"] = str(scratch / "m.profraw")
        for test in pool:
            objects = [test["obj"], mutant_obj]
            if test["focal_file"] != rel and test["focal_file"] in self.focal_objects:
                objects.append(self.focal_objects[test["focal_file"]])
            linked = link_binary(self.project, objects, binary, scratch)
            if not linked.ok:
                continue
            record["tests_run"] += 1
            ran = harness.run([str(binary), f"--gtest_filter={':'.join(test['cases'])}"],
                              scratch, test["timeout"], env=env)
            if not ran.ok:
                record["status"] = "killed"
                record["killed_by"] = test["name"]
                record["exit_code"] = ran.exit_code
                break
        else:
            record["status"] = "survived" if record["tests_run"] else "invalid"
            if not record["tests_run"]:
                record["reason"] = "no covering test links"
        binary.unlink(missing_ok=True)
        mutant_obj.unlink(missing_ok=True)
        (scratch / "m.profraw").unlink(missing_ok=True)
        return record


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

def prepare(project, out_dir, work, jobs):
    """Compile every test once and record what it covers."""
    from bench.cli import _read_manifest  # noqa: PLC0415
    manifest = _read_manifest(out_dir)
    rows = {(r.file, r.method, r.line): r for r in read_jsonl(out_dir / "rows.jsonl")}
    focal_files = sorted({e["focal"]["file"] for e in manifest["entries"]})
    obj_dir = work / "obj"
    obj_dir.mkdir(parents=True, exist_ok=True)

    tests = []
    for entry in manifest["entries"]:
        focal = entry["focal"]
        row = rows.get((focal["file"], focal["name"], focal["start_line"]))
        if row is None or not entry.get("test_file"):
            continue
        cases = [c for c, v in (row.detail.get("cases") or {}).items() if v == "passed"]
        if not cases:
            continue
        generated = out_dir / "generated" / entry["test_file"]
        source = generated
        for prefix in ("pruned_", "repaired_"):
            candidate = out_dir / "work" / f"{prefix}{generated.name}"
            if candidate.is_file():
                source = candidate
        if not source.is_file():
            continue
        tests.append({"name": generated.stem, "source": source,
                      "obj": obj_dir / f"{generated.stem}.o",
                      "focal_file": focal["file"], "start": focal["start_line"],
                      "end": focal["end_line"], "cases": cases, "timeout": 30})

    print(f"tests with a passing case: {len(tests)}", file=sys.stderr)
    todo = [t for t in tests if not t["obj"].is_file()]
    if todo:
        print(f"compiling {len(todo)} test objects ...", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            list(pool.map(lambda t: compile_object(project, t["source"], t["obj"], work), todo))
    tests = [t for t in tests if t["obj"].is_file()]
    print(f"test objects available: {len(tests)}", file=sys.stderr)

    extent = {rel: len((project.root / rel).read_text(
        encoding="utf-8", errors="replace").split("\n")) for rel in focal_files}
    cover_path = out_dir / COVER_NAME
    cover = {}
    if cover_path.is_file():
        cover = {k: {f: set(v) for f, v in d.items()}
                 for k, d in json.loads(cover_path.read_text(encoding="utf-8")).items()}
    missing = [t for t in tests if t["name"] not in cover]
    if missing:
        print(f"measuring coverage of {len(missing)} tests ...", file=sys.stderr)
        cover_dir = work / "cover"
        cover_dir.mkdir(parents=True, exist_ok=True)

        def measure(test):
            binary = out_dir / "work" / f"{test['name']}_runner"
            if not binary.is_file():
                binary = cover_dir / f"{test['name']}_runner"
                if not link_binary(project, [test["obj"]], binary, cover_dir).ok:
                    return test["name"], {}
            return test["name"], _covered_lines(binary, test["cases"], focal_files,
                                                project.root, cover_dir, extent)

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for name, lines in pool.map(measure, missing):
                cover[name] = lines
        cover_path.write_text(json.dumps(
            {k: {f: sorted(v) for f, v in d.items()} for k, d in cover.items()}),
            encoding="utf-8")
    return manifest, focal_files, tests, cover


def _line_bootstrap(records: list[dict], seed: int, draws: int = 2000) -> list | None:
    """95 % interval for the score, resampling the sampled lines.

    The sampling unit is the line, so the mutants on one line rise and fall
    together and an interval computed over individual mutants would be too
    narrow. Lines are resampled with replacement instead.
    """
    by_line: dict[tuple, list[int]] = {}
    for r in records:
        if r["status"] in ("killed", "survived"):
            by_line.setdefault((r["file"], r["line"]), [0, 0])
            by_line[(r["file"], r["line"])][r["status"] == "survived"] += 1
    clusters = list(by_line.values())
    if len(clusters) < 2:
        return None
    rng = random.Random(seed)
    scores = []
    for _ in range(draws):
        picked = [clusters[rng.randrange(len(clusters))] for _ in clusters]
        k = sum(c[0] for c in picked)
        n = k + sum(c[1] for c in picked)
        if n:
            scores.append(100.0 * k / n)
    scores.sort()
    if not scores:
        return None
    return [round(scores[int(0.025 * len(scores))], 2),
            round(scores[int(0.975 * len(scores)) - 1], 2)]


def main(args) -> int:
    out_dir = Path(args.out).resolve()
    from bench.cli import _read_manifest, _resolve_project  # noqa: PLC0415
    manifest = _read_manifest(out_dir)
    root = Path(args.project).resolve() if args.project else _resolve_project(manifest["project"])
    project = harness.load_built(root, Path(args.build_dir))
    if project is None:
        print(f"ERROR: no instrumented build under {args.build_dir}", file=sys.stderr)
        return 3

    # Sweep the whole project before reading anything: a run killed during
    # generation leaves mutants standing in for sources, and the file that is
    # poisoned need not be the one this invocation mutates.
    focal_all = sorted({e["focal"]["file"] for e in manifest["entries"]})
    recovered = [rel for rel in focal_all if restore_source(root / rel)]
    if recovered:
        print(f"restored {len(recovered)} source(s) left mutated by an earlier run: "
              f"{', '.join(recovered)}", file=sys.stderr)
    swept = sweep_scratch({(root / rel).parent for rel in focal_all})
    if swept:
        print(f"swept {swept} leftover scratch file(s)", file=sys.stderr)

    work = out_dir / "mut-cw"
    work.mkdir(parents=True, exist_ok=True)
    manifest, focal_files, tests, cover = prepare(project, out_dir, work, args.jobs)
    if not tests:
        print("ERROR: no test has a passing case", file=sys.stderr)
        return 3

    scorer = Scorer(project, out_dir, focal_files, tests, cover, work,
                    verify_uncovered=args.verify_uncovered)
    for rel in focal_files:
        obj = work / "obj" / f"focal_{Path(rel).stem}.o"
        if not obj.is_file():
            compile_object(project, root / rel, obj, work)
        if obj.is_file():
            scorer.focal_objects[rel] = obj

    # A sampled run must not overwrite an exhaustive one: the two are different
    # measurements and the exhaustive one is what validates the sampled one.
    suffix = f"-l{args.sample_lines}s{args.seed}" if args.sample_lines else ""
    output = out_dir / OUTPUT_NAME.replace(".jsonl", suffix + ".jsonl")
    summary_path = out_dir / SUMMARY_NAME.replace(".json", suffix + ".json")
    done = set()
    records = []
    if args.resume and output.is_file():
        for line in output.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                records.append(r)
                done.add((r["file"], r["mutant"]))

    mutated_files = focal_files
    if args.files:
        wanted = {f.strip() for part in args.files for f in part.split(",") if f.strip()}
        unknown = wanted - set(focal_files)
        if unknown:
            print(f"ERROR: not focal files of this sweep: {sorted(unknown)}", file=sys.stderr)
            return 2
        mutated_files = [f for f in focal_files if f in wanted]
        print(f"mutating only: {', '.join(mutated_files)}", file=sys.stderr)

    drawn = None
    if args.sample_lines:
        drawn = sample_lines(root, mutated_files, args.sample_lines, args.seed)
        total = sum(len((root / f).read_text(encoding="utf-8", errors="replace").split("\n"))
                    for f in mutated_files)
        print(f"line sample: {sum(len(v) for v in drawn.values())} of {total} lines "
              f"across {len(drawn)} files (seed {args.seed})", file=sys.stderr)

    # Generation is the slow half: universalmutator compiles every candidate
    # mutant itself, one at a time. Different files are independent -- each
    # tool instance swaps mutants only into its own source and names its temp
    # files after its own pid -- so files are generated concurrently. A single
    # file cannot be split this way, since the instances would swap mutants
    # into the same source in parallel.
    todo = [rel for rel in mutated_files if drawn is None or rel in drawn]
    stems = {rel: (Path(rel).stem if drawn is None
                   else f"{Path(rel).stem}-l{args.sample_lines}s{args.seed}")
             for rel in todo}
    if len(set(stems.values())) != len(stems):          # basenames repeat across dirs
        stems = {rel: v + "-" + hashlib.sha1(rel.encode()).hexdigest()[:6]
                 for rel, v in stems.items()}

    def generate_one(rel: str) -> tuple[str, list[Path]]:
        return rel, generate_mutants(
            project, root / rel, work / "mutants" / stems[rel],
            drawn.get(rel) if drawn is not None else None)

    jobs: list[tuple[str, Path, int]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.gen_jobs)) as pool:
        for rel, mutants in pool.map(generate_one, todo):
            original = (root / rel).read_text(encoding="utf-8", errors="replace").split("\n")
            print(f"{rel}: {len(mutants)} mutants generated", file=sys.stderr, flush=True)
            for mutant in mutants:
                line = mutated_line(original, mutant)
                if line is None:
                    continue
                jobs.append((rel, mutant, line))

    # Shuffled even for a full run, so that the mutants scored at any moment
    # are a uniform random sample of the population: a run stopped early
    # still estimates the score without bias, and --sample is then just a
    # prefix of the same order.
    random.Random(args.seed).shuffle(jobs)
    if args.sample and len(jobs) > args.sample:
        jobs = jobs[:args.sample]
        print(f"sampling {len(jobs)} mutants (seed {args.seed})", file=sys.stderr)
    jobs = [j for j in jobs if (j[0], j[1].name) not in done]
    print(f"scoring {len(jobs)} mutants with {args.jobs} workers", file=sys.stderr)

    started = time.monotonic()
    counter = {"n": 0}
    with output.open("a" if args.resume else "w", encoding="utf-8") as sink:
        def work_one(item):
            rel, mutant, line = item
            slot = counter["n"] % args.jobs
            return scorer.score(rel, mutant, line, slot)

        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            for record in pool.map(work_one, jobs):
                counter["n"] += 1
                records.append(record)
                sink.write(json.dumps(record) + "\n")
                sink.flush()
                if counter["n"] % 25 == 0:
                    rate = (time.monotonic() - started) / counter["n"]
                    left = (len(jobs) - counter["n"]) * rate / 3600
                    k = sum(1 for r in records if r["status"] == "killed")
                    s = sum(1 for r in records if r["status"] == "survived")
                    print(f"  {counter['n']}/{len(jobs)}  killed={k} survived={s} "
                          f"invalid={sum(1 for r in records if r['status'] == 'invalid')}  "
                          f"score={100*k/max(1,k+s):.2f}%  {rate:.1f}s/mutant  eta {left:.1f}h",
                          file=sys.stderr, flush=True)

    killed = sum(1 for r in records if r["status"] == "killed")
    survived = sum(1 for r in records if r["status"] == "survived")
    invalid = sum(1 for r in records if r["status"] == "invalid")
    summary = {
        "project": root.name, "protocol": "citywalk (universalmutator, whole file, whole suite)",
        "mutated_files": mutated_files,
        "tests": len(tests), "mutants_scored": killed + survived,
        "valid_mutants": killed + survived, "killed": killed, "survived": survived,
        "invalid": invalid,
        "mutation_score": round(100.0 * killed / (killed + survived), 2) if killed + survived else None,
        "valid_rate": round(100.0 * (killed + survived) / max(1, len(records)), 2),
        "sampled": args.sample or None, "seed": args.seed,
        "sampled_lines": args.sample_lines or None,
        "ci95": _line_bootstrap(records, args.seed),
        "survived_uncovered": sum(1 for r in records
                                  if r.get("reason") == "no covering test"
                                  and r["status"] == "survived"),
        "uncovered_verified": sum(1 for r in records if r.get("verified")),
        "uncovered_killed_anyway": sum(1 for r in records if r.get("verified")
                                       and r["status"] == "killed"),
    }
    summary_path.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0
