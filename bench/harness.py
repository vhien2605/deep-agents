"""Build, run, and measure one generated test file.

The project under test is compiled once with gcov instrumentation into a static
library; every generated test file then links against that library. Coverage is
attributed per run by deleting the accumulated `.gcda` counters before each
test binary executes, so one row's numbers never leak into the next.

This module assumes a POSIX toolchain (g++, gcov, gcovr) on PATH — that is what
the benchmark container provides.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from bench import metrics, projects, repair

COMPILE_TIMEOUT_S = 300
RUN_TIMEOUT_S = 120
# glomap's FetchContent chain (COLMAP + PoseLib) legitimately needs more than
# an hour of compilation on a loaded machine; killing the build at 3600s made
# the project look unbuildable when it merely builds slowly.
BUILD_TIMEOUT_S = int(os.environ.get("BENCH_BUILD_TIMEOUT_S", "3600"))

# Rounds of "drop the cases the compiler rejected, try again". Bounded because
# each round costs a full compile; three is enough to separate a couple of bad
# cases from a file that is broken as a whole.
MAX_COMPILE_ROUNDS = 3

# clang's source-based coverage, the instrumentation CITYWALK uses
# (`utils.py:285-296`). Unlike gcov's `--coverage`, it records regions
# rather than basic blocks, so it does not count the implicit exception
# edges GCC emits around every C++ call — the reason gcov reports a much
# lower branch coverage for the same tests.
COVERAGE_FLAGS = ["-fprofile-instr-generate", "-fcoverage-mapping", "-O0", "-g"]
CXX = "clang++"
CC = "clang"


@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def output(self) -> str:
        return f"{self.stdout}\n{self.stderr}".strip()


@dataclass
class BuiltProject:
    """An instrumented build of the project under test."""

    root: Path
    build_dir: Path
    libraries: list[Path]
    include_dirs: list[Path]
    cxx_standard: str = "c++17"
    extra_link_flags: list[str] = field(default_factory=list)


@dataclass
class EvalResult:
    """Everything measured for one generated test file."""

    n_cases: int = 0
    n_cases_compiled: int = 0
    n_cases_passed: int = 0
    line_coverage: float = 0.0
    branch_coverage: float = 0.0
    touched_focal: bool = False
    error_category: str = ""
    error_first_line: str = ""
    test_loc: int = 0
    test_cc: int = 0
    uses_mock: bool = False
    detail: dict = field(default_factory=dict)


def run(cmd: list[str], cwd: Path, timeout: int, env: dict | None = None) -> CommandResult:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            env=env, errors="replace",
        )
        return CommandResult(" ".join(cmd), proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or b"")
        text = partial.decode(errors="replace") if isinstance(partial, bytes) else str(partial)
        return CommandResult(" ".join(cmd), 124, text, f"TIMEOUT after {timeout}s")
    except (FileNotFoundError, OSError) as exc:
        return CommandResult(" ".join(cmd), 127, "", str(exc))


def _guess_include_dirs(root: Path) -> list[Path]:
    """Header search paths, matching how the project includes its own headers.

    Projects disagree on this and the difference is not cosmetic: re2 writes
    `#include "re2/re2.h"` against the project root, while hjson-cpp keeps its
    header at `include/hjson/hjson.h` and writes `#include "hjson.h"`, relying
    on CMake exporting `include/hjson` as an include directory. Offering the
    root, the conventional directories, and each immediate subdirectory of
    `include/` covers both conventions.

    Getting this wrong does not fail loudly — it makes every generated test for
    that project fail to compile with `fatal error: ... No such file`, which
    reads as a model failure rather than a harness one.
    """
    candidates = [root]
    for name in ("include", "src", "inc"):
        path = root / name
        if path.is_dir():
            candidates.append(path)
            # include/<lib>/header.h is the common layout for a library whose
            # own sources include just <header.h>.
            candidates.extend(sorted(p for p in path.iterdir() if p.is_dir()))
    return candidates


def _link_txt_libraries(build_dir: Path, project_libraries: list[Path]) -> list[str]:
    """Link arguments taken from the build's own link line.

    Collecting `*.a` from the build tree finds a project's own archives but
    not what those archives need. glomap's tests compile once its PoseLib
    headers are reachable, then fail to link against Boost, glog and gflags,
    because those are system libraries no directory walk would turn up. CMake
    already wrote the answer: `link.txt` under `CMakeFiles/<target>.dir` holds
    the exact library list, in the order the linker needs.

    The target chosen is the one whose line mentions a library we already
    found, so a project's own executable is preferred over a dependency's,
    and the longest such line wins -- an executable links everything a test
    for the same code will need. Returns an empty list for a project whose
    build has no link line to read, leaving the caller its own list.
    """
    own = {library.name for library in project_libraries}
    best: list[str] = []
    best_rank = (False, -1)
    for link_txt in sorted(build_dir.rglob("CMakeFiles/*.dir/link.txt")):
        try:
            words = shlex.split(link_txt.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # CMake runs the link step from the target's own build directory,
        # three levels above link.txt (<dir>/CMakeFiles/<target>.dir/link.txt),
        # and writes archive paths relative to
        # it; they must be anchored there to mean the same thing elsewhere.
        target_dir = link_txt.parents[2]
        libraries: list[str] = []
        for word in words:
            if word.startswith(("-l", "-L", "-Wl,-rpath")):
                libraries.append(word)
            elif word.endswith(".a") or ".so" in word:
                path = Path(word)
                if not path.is_absolute():
                    path = (target_dir / path).resolve()
                libraries.append(str(path))
        if not any(Path(word).name in own for word in libraries):
            continue
        # A fetched dependency's own executable (colmap's, under _deps) can
        # link more than the project's, but its line is written for its
        # directory and its needs, not ours; the project's own target wins.
        rank = ("_deps" not in link_txt.parts, len(libraries))
        if rank > best_rank:
            best, best_rank = libraries, rank
    return best


def _compile_db_include_dirs(build_dir: Path) -> list[Path]:
    """Header search paths taken from the build's own compile commands.

    Guessing from the source layout covers a project that includes its own
    headers, and `extra_include_dirs` patches the rest by hand, but neither
    reaches a dependency CMake fetched during configuration: glomap's PoseLib
    lands under `_deps/poselib-src`, and a test that includes
    `PoseLib/misc/colmap_models.h` fails to compile for a reason that has
    nothing to do with the model. `compile_commands.json` records the exact
    flags the project used for its own translation units, so it answers the
    question rather than approximating it. Ordered as the build ordered them,
    duplicates removed.
    """
    database = build_dir / "compile_commands.json"
    if not database.is_file():
        return []
    try:
        entries = json.loads(database.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    found: list[Path] = []
    seen: set[str] = set()
    for entry in entries:
        arguments = entry.get("arguments")
        if arguments is None:
            arguments = shlex.split(entry.get("command", ""))
        index = 0
        while index < len(arguments):
            argument = arguments[index]
            raw = ""
            if argument in ("-I", "-isystem") and index + 1 < len(arguments):
                raw = arguments[index + 1]
                index += 1
            elif argument.startswith("-I") and len(argument) > 2:
                raw = argument[2:]
            elif argument.startswith("-isystem") and len(argument) > 8:
                raw = argument[8:]
            index += 1
            if not raw:
                continue
            path = Path(raw)
            if not path.is_absolute():
                path = build_dir / path
            key = path.as_posix()
            if key not in seen and path.is_dir():
                seen.add(key)
                found.append(path)
    return found


CXX_STANDARD_RE = re.compile(
    r"set\s*\(\s*CMAKE_CXX_STANDARD\s+(\d+)|cxx_std_(\d+)", re.IGNORECASE
)


def detect_cxx_standard(root: Path, fallback: str = "c++17") -> str:
    """The C++ standard the project builds itself with.

    Generated tests must be compiled the same way: mlx sets CMAKE_CXX_STANDARD
    20 and uses defaulted comparison operators, so compiling its tests as C++17
    fails inside the project's own headers — an error that reads as a bad test
    but is a harness mismatch.
    """
    versions: list[int] = []
    for cmake_file in list(root.glob("CMakeLists.txt")) + list(root.glob("*/CMakeLists.txt")):
        for match in CXX_STANDARD_RE.finditer(cmake_file.read_text(errors="replace")):
            value = match.group(1) or match.group(2)
            if value and value.isdigit():
                versions.append(int(value))
    if not versions:
        return fallback
    # Never go below the fallback: several projects still declare C++11, but
    # the GoogleTest the tests link against needs C++14 or newer, so honouring
    # the declaration literally would break projects that compile fine today.
    # A newer standard than the project asks for is safe; an older one is not.
    floor = int(fallback.removeprefix("c++"))
    return f"c++{max(max(versions), floor)}"


def build_manually(
    root: Path, build_dir: Path, config: "projects.ProjectConfig", jobs: int = 0
) -> tuple[BuiltProject | None, CommandResult]:
    """Compile a project's sources into a static library without a build system.

    For projects that ship only a hand-written makefile producing an
    executable: a test binary cannot link against an executable, so the same
    translation units are compiled here and archived instead. Entry points are
    left out, since the test binary supplies its own main().
    """
    build_dir.mkdir(parents=True, exist_ok=True)
    sources: list[Path] = []
    for pattern in config.manual_source_globs:
        sources.extend(
            p for p in sorted(root.glob(pattern))
            if p.name not in config.manual_exclude_names
        )
    if not sources:
        return None, CommandResult(
            "collect sources", 1, "", f"no sources matched {config.manual_source_globs}"
        )

    objects: list[Path] = []
    for source in sources:
        obj = build_dir / f"{source.stem}.o"
        result = run(
            [CXX, f"-std={config.cxx_standard}", *COVERAGE_FLAGS, "-c",
             str(source), "-I", str(root), "-I", str(root / "src"),
             *config.extra_cxx_flags, "-o", str(obj)],
            cwd=root, timeout=COMPILE_TIMEOUT_S,
        )
        if not result.ok:
            return None, result
        objects.append(obj)

    library = build_dir / "libproject.a"
    archived = run(["ar", "rcs", str(library), *[str(o) for o in objects]],
                   cwd=build_dir, timeout=COMPILE_TIMEOUT_S)
    if not archived.ok:
        return None, archived
    return (
        BuiltProject(
            root=root, build_dir=build_dir, libraries=[library],
            include_dirs=_guess_include_dirs(root),
            cxx_standard=config.cxx_standard,
            extra_link_flags=list(config.link_flags),
        ),
        archived,
    )


def build_instrumented(
    root: Path,
    build_dir: Path,
    *,
    cxx_standard: str = "c++17",
    jobs: int = 0,
    extra_cmake_args: list[str] | None = None,
) -> tuple[BuiltProject | None, CommandResult]:
    """Configure and build the project with coverage instrumentation.

    Returns (project, last command) — project is None when the build failed,
    in which case every focal method in it is unmeasurable and the caller
    should record the failure once rather than per method.
    """
    # Absolute paths throughout: cmake runs with cwd=root, so a relative -S
    # would be resolved against the project directory a second time.
    root = root.resolve()
    build_dir = build_dir.resolve()
    config = projects.config_for(root.name)
    if config.cxx_standard != "c++17":
        cxx_standard = config.cxx_standard      # explicit per-project override
    elif cxx_standard == "c++17":
        cxx_standard = detect_cxx_standard(root)  # otherwise follow the project

    if config.manual_source_globs:
        return build_manually(root, build_dir, config, jobs)

    build_dir.mkdir(parents=True, exist_ok=True)
    flags = " ".join([*COVERAGE_FLAGS, *config.extra_cxx_flags]) + f" -std={cxx_standard}"
    configure = run(
        [
            "cmake", "-S", str(root), "-B", str(build_dir),
            "-DCMAKE_BUILD_TYPE=Debug",
            # The library and the tests must be instrumented by the same
            # compiler: a profile written by clang cannot be read against
            # objects GCC produced.
            f"-DCMAKE_CXX_COMPILER={CXX}",
            f"-DCMAKE_C_COMPILER={CC}",
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
            f"-DCMAKE_CXX_FLAGS={flags}",
            f"-DCMAKE_C_FLAGS={' '.join(COVERAGE_FLAGS)}",
            "-DCMAKE_EXE_LINKER_FLAGS=-fprofile-instr-generate",
            *config.cmake_args,
            *(extra_cmake_args or []),
        ],
        cwd=root,
        timeout=BUILD_TIMEOUT_S,
    )
    if not configure.ok:
        return None, configure

    build_cmd = ["cmake", "--build", str(build_dir)]
    if jobs:
        build_cmd += ["--parallel", str(jobs)]
    built = run(build_cmd, cwd=root, timeout=BUILD_TIMEOUT_S)
    if not built.ok:
        return None, built
    return _assemble_project(root, build_dir, config, cxx_standard, built)


def load_built(root: Path, build_dir: Path) -> BuiltProject | None:
    """Describe an instrumented build that already exists, without building.

    A scoring pass that only recompiles tests -- mutation analysis, a metric
    correction -- must link against exactly the libraries the evaluated rows
    linked against; re-running the build here would risk a different tree.
    Returns None when the directory holds no library.
    """
    root = root.resolve()
    build_dir = build_dir.resolve()
    config = projects.config_for(root.name)
    cxx_standard = (config.cxx_standard if config.cxx_standard != "c++17"
                    else detect_cxx_standard(root))
    project, _ = _assemble_project(
        root, build_dir, config, cxx_standard,
        CommandResult("load existing build", 0, "", ""))
    return project


def _assemble_project(
    root: Path, build_dir: Path, config: "projects.ProjectConfig",
    cxx_standard: str, built: CommandResult,
) -> tuple[BuiltProject | None, CommandResult]:
    """Locate what a finished build produced and what tests need to use it."""
    # CMake's own probe artifacts live under CMakeFiles/ (e.g. the LTO test
    # emits a libfoo.a); linking tests against one of those instead of the
    # project fails every method with undefined references. Only archives the
    # project itself produced count.
    def _project_libs(pattern: str) -> list[Path]:
        return sorted(
            p for p in build_dir.rglob(pattern)
            if p.is_file() and "CMakeFiles" not in p.parts
        )

    libraries = _project_libs("*.a") or _project_libs("*.so")
    if not libraries:
        # OBJECT libraries (add_library(... OBJECT ...)) produce no archive at
        # all — ninja is one. Their compiled objects sit in
        # CMakeFiles/<target>.dir/; archive them so tests have code to link.
        objects = sorted(
            o for o in build_dir.rglob("CMakeFiles/*.dir/**/*.o")
            if o.is_file() and "_CMake" not in str(o)
        )
        # Objects belonging to executables define main(). The C runtime's
        # undefined `main` is resolved while scanning this archive — before
        # gtest_main is reached — so leaving them in makes every test binary
        # run the project's own main instead of the tests. Keep only
        # main-free objects.
        def _defines_main(obj: Path) -> bool:
            nm = run(["nm", "-g", "--defined-only", str(obj)], build_dir, 30)
            return any(line.endswith(" T main") for line in nm.stdout.splitlines())

        objects = [o for o in objects if not _defines_main(o)]
        if objects:
            archive = build_dir / "libbench_objects.a"
            packed = run(["ar", "rcs", str(archive), *map(str, objects)],
                         cwd=build_dir, timeout=BUILD_TIMEOUT_S)
            if packed.ok and archive.is_file():
                libraries = [archive]
    if not libraries:
        return None, CommandResult(
            "locate library", 1, "", f"no static/shared library produced under {build_dir}"
        )
    # Per-project header paths a generated test needs but the project root
    # does not cover: FetchContent dependencies land under the build dir, and
    # some system packages live in versioned include directories.
    extra_includes: list[Path] = []
    for raw in config.extra_include_dirs:
        path = Path(raw) if Path(raw).is_absolute() else build_dir / raw
        if path.is_dir():
            extra_includes.append(path)
    return (
        BuiltProject(
            root=root,
            build_dir=build_dir,
            libraries=libraries,
            include_dirs=(_guess_include_dirs(root) + extra_includes
                          + _compile_db_include_dirs(build_dir)),
            cxx_standard=cxx_standard,
            extra_link_flags=(list(config.link_flags)
                              + _link_txt_libraries(build_dir, libraries)),
        ),
        built,
    )


def _gmock_available() -> bool:
    """gmock is optional: linking -lgmock when it is absent fails every compile."""
    return Path("/usr/include/gmock/gmock.h").is_file() or Path(
        "/usr/local/include/gmock/gmock.h"
    ).is_file()


def compile_command(
    project: BuiltProject, test_file: Path, binary: Path,
    focal_source: Path | None = None,
) -> list[str]:
    """The command that turns one generated test into a runnable binary.

    CITYWALK compiles the file under test alongside the test rather than
    relying on the archive alone (`utils.py:285-296`), and that difference is
    not cosmetic: a static archive only yields an object when some symbol in
    it is still undefined, so vtables, typeinfo and anything with internal
    linkage never arrive, and the test fails to link for a reason that has
    nothing to do with the model. Naming the source directly resolves those,
    and makes a file the build system cannot compile here — Metal, CUDA — fail
    as the compile error it is rather than silently linking against nothing.
    """
    config = projects.config_for(project.root.name)
    cmd = [CXX, f"-std={project.cxx_standard}", *COVERAGE_FLAGS,
           *config.extra_cxx_flags]
    for directory in project.include_dirs:
        cmd += ["-I", str(directory)]
    cmd.append(str(test_file))
    if focal_source is not None and focal_source.is_file():
        cmd.append(str(focal_source))
    # Static archives resolve symbols left to right, so an alphabetical
    # listing fails whenever one project library calls into another
    # (json.cpp's libjson.a into libdouble-conversion.a). --start-group
    # lets ld retry the group until every reference resolves.
    if project.libraries:
        cmd += ["-Wl,--start-group", *[str(lib) for lib in project.libraries],
                "-Wl,--end-group"]
    cmd += ["-lgtest", "-lgtest_main"]
    if _gmock_available():
        cmd += ["-lgmock"]
    cmd += ["-lpthread"]
    cmd += project.extra_link_flags
    cmd += ["-o", str(binary)]
    return cmd


def _clear_counters(*roots: Path) -> None:
    """Reset gcov counters so each test binary is measured in isolation."""
    for root in roots:
        for gcda in root.rglob("*.gcda"):
            gcda.unlink(missing_ok=True)


def _includes_source(test_source: str, focal_file: str) -> bool:
    """Whether the test pulls the file under test in through the preprocessor."""
    name = re.escape(Path(focal_file).name)
    return re.search(rf'#\s*include\s*[<"][^">]*{name}[">]', test_source) is not None


def _compile_with_pruning(
    project: BuiltProject,
    test_file: Path,
    binary: Path,
    work_dir: Path,
    repair_model=None,
    max_repair_rounds: int = 0,
    repair_log: list | None = None,
    focal_source: Path | None = None,
) -> tuple[CommandResult, str, set[str], list[dict]]:
    """Compile, dropping rejected cases and retrying.

    Returns (final result, final source, dropped case ids, attempt log). The
    per-case compile rate comes from this pruning: a case that had to be
    removed for the file to build did not compile.
    """
    source = test_file.read_text(errors="replace")
    dropped: set[str] = set()
    attempts: list[dict] = []
    pruned_file = work_dir / f"pruned_{test_file.name}"
    repaired_file = work_dir / f"repaired_{test_file.name}"
    repairs_used = 0

    current_file = test_file
    for round_index in range(MAX_COMPILE_ROUNDS + 1):
        result = run(
            compile_command(project, current_file, binary, focal_source),
            work_dir, COMPILE_TIMEOUT_S,
        )
        attempts.append({
            "round": round_index,
            "exit_code": result.exit_code,
            "dropped_so_far": sorted(dropped),
            "stderr_head": result.stderr[:1500],
        })
        if result.ok:
            return result, source, dropped, attempts
        if round_index == MAX_COMPILE_ROUNDS:
            return result, source, dropped, attempts

        # Repair before pruning: the paper's order is fix first, and only then
        # drop whatever still refuses to compile. Pruning first would discard
        # cases the repair round could have saved, understating CSR.
        if repair_model is not None and repairs_used < max_repair_rounds:
            repairs_used += 1
            fixed, info = repair.repair_once(repair_model, source, result.output)
            if repair_log is not None:
                repair_log.append({"round": repairs_used, **info,
                                   "applied": fixed is not None})
            if fixed is not None:
                source = fixed
                repaired_file.write_text(fixed, encoding="utf-8")
                current_file = repaired_file
                continue

        bad_lines = metrics.failing_case_lines(result.output, current_file.name)
        offenders = metrics.cases_covering_lines(source, bad_lines)
        offenders -= dropped
        if not offenders:
            # The failure is outside any test case (bad include, broken
            # fixture): pruning cases cannot fix it, so stop here.
            return result, source, dropped, attempts
        dropped |= offenders
        source = metrics.strip_cases(source, offenders)
        if not metrics.count_gtest_cases(source):
            return result, source, dropped, attempts
        pruned_file.write_text(source, encoding="utf-8")
        current_file = pruned_file
    return result, source, dropped, attempts


def _gcda_for(build_dir: Path, focal_file: str) -> Path | None:
    """The instrumented object's counter file for one source, if it exists.

    CMake names it `<obj-dir>/<source>.gcda`, so the source's stem identifies
    it; test binaries produce their own `.gcda` elsewhere and are excluded by
    searching only under the project build directory.
    """
    stem = Path(focal_file).name
    candidates = [p for p in build_dir.rglob(f"{stem}.gcda")]
    if not candidates:
        candidates = [p for p in build_dir.rglob(f"{Path(stem).stem}.gcda")]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # A basename is not unique: mlx ships 39 of them twice (backend/cpu and
    # backend/metal both define sort.cpp, binary.cpp, reduce.cpp...), so
    # taking the first match can score a different translation unit than the
    # focal method lives in. Prefer the counter file whose path agrees with
    # the focal source over the most directory components.
    parts = Path(focal_file).as_posix().split("/")

    def agreement(path: Path) -> int:
        posix = path.as_posix()
        for keep in range(len(parts), 0, -1):
            if posix.endswith("/".join(parts[-keep:]) + ".gcda"):
                return keep
        return 0

    return max(candidates, key=agreement)


def _clear_profiles(*roots: Path) -> None:
    """Drop stale profiles so each test binary is measured in isolation."""
    for root in roots:
        for stale in root.rglob("*.profraw"):
            stale.unlink(missing_ok=True)
        for stale in root.rglob("*.profdata"):
            stale.unlink(missing_ok=True)


def _run_instrumented(binary: Path, work_dir: Path, gtest_json: Path) -> tuple:
    """Run one test binary so it writes its own profile.

    Returns (result, profraw). clang's runtime writes to the path named by
    LLVM_PROFILE_FILE; giving each binary its own file is what keeps one
    method's coverage from being attributed to the next.
    """
    profraw = work_dir / f"{binary.stem}.profraw"
    profraw.unlink(missing_ok=True)
    env = dict(os.environ)
    env["LLVM_PROFILE_FILE"] = str(profraw)
    result = run(
        [str(binary), f"--gtest_output=json:{gtest_json}"],
        work_dir, RUN_TIMEOUT_S, env=env,
    )
    return result, profraw


def _llvm_export(binary: Path, profraw: Path, focal_file: str,
                 project_root: Path, work_dir: Path) -> dict:
    """Coverage of the focal source as `llvm-cov export` JSON.

    The profile is merged first because llvm-cov reads an indexed profile,
    not the raw one the runtime writes. Restricting the export to the focal
    source keeps the document small: without it llvm-cov emits every
    translation unit linked into the binary, which for a project the size of
    mlx is tens of megabytes per method.
    """
    if not profraw.is_file():
        return {}
    profdata = profraw.with_suffix(".profdata")
    merged = run(
        ["llvm-profdata", "merge", "-sparse", str(profraw), "-o", str(profdata)],
        work_dir, RUN_TIMEOUT_S,
    )
    if not merged.ok or not profdata.is_file():
        return {}
    source = project_root / focal_file
    result = run(
        ["llvm-cov", "export", str(binary),
         f"-instr-profile={profdata}", str(source)],
        work_dir, RUN_TIMEOUT_S,
    )
    if not result.ok or not result.stdout.strip():
        # A source llvm-cov does not know about is not an error worth
        # failing the row for: it means the file was never instrumented,
        # which the caller reports as zero coverage.
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def _gcov_report(gcda: Path) -> dict:
    """Coverage for a single translation unit, via gcov's own JSON output.

    gcovr re-runs gcov over every object in the project — 26 s per method on
    re2, which dominates a full sweep. Only the focal file's counters matter
    here, so gcov is invoked directly on its one .gcda: same numbers, a
    fraction of a second. The shape returned matches gcovr's so callers do not
    care which path produced it.
    """
    result = run(
        ["gcov", "--json-format", "--stdout", "--branch-probabilities", gcda.name],
        cwd=gcda.parent,
        timeout=RUN_TIMEOUT_S,
    )
    if not result.ok or not result.stdout.strip():
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        # gcov emits one JSON document per input file; take the first.
        first = result.stdout.split("\n{", 1)[0]
        try:
            return json.loads(first)
        except json.JSONDecodeError:
            return {}


def _gcovr_report(project: BuiltProject, work_dir: Path) -> dict:
    """Coverage of the project sources as gcovr JSON."""
    # Search paths are passed positionally: `--search-path` only exists in
    # gcovr 8.6+, and `--gcov-ignore-parse-errors` needs the `=value` form or
    # it swallows the following argument as its own value.
    result = run(
        [
            "gcovr", "--root", str(project.root), "--json",
            "--gcov-ignore-parse-errors=all",
            "--exclude", r".*/gtest.*",
            str(project.build_dir), str(work_dir),
        ],
        cwd=project.root,
        timeout=RUN_TIMEOUT_S * 4,
    )
    if not result.ok or not result.stdout.strip():
        # Very old gcovr lacks --gcov-ignore-parse-errors entirely.
        result = run(
            ["gcovr", "--root", str(project.root), "--json",
             str(project.build_dir), str(work_dir)],
            cwd=project.root,
            timeout=RUN_TIMEOUT_S * 4,
        )
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return {"files": [], "_error": result.stderr[:500]}


def evaluate_test_file(
    project: BuiltProject,
    test_file: Path,
    focal_file: str,
    focal_start: int,
    focal_end: int,
    work_dir: Path,
    repair_model=None,
    max_repair_rounds: int = 0,
) -> EvalResult:
    """Compile, run, and measure coverage for one generated test file."""
    work_dir.mkdir(parents=True, exist_ok=True)
    source = test_file.read_text(errors="replace")
    result = EvalResult(
        n_cases=len(metrics.count_gtest_cases(source)),
        test_loc=metrics.test_loc(source),
        test_cc=metrics.test_complexity(source),
        uses_mock=metrics.uses_mock(source),
    )
    if result.n_cases == 0:
        result.error_category = "no_test_cases"
        result.error_first_line = "generated file declares no GoogleTest cases"
        return result

    binary = work_dir / f"{test_file.stem}_runner"
    repair_log: list[dict] = []
    compiled, final_source, dropped, attempts = _compile_with_pruning(
        project, test_file, binary, work_dir,
        repair_model=repair_model, max_repair_rounds=max_repair_rounds,
        repair_log=repair_log,
        # A generated test that #includes the source under test already carries
        # its definitions; naming the file again would be a second translation
        # unit for the same symbols and every one of them collides.
        focal_source=None if _includes_source(source, focal_file)
        else project.root / focal_file,
    )
    result.detail["compile_attempts"] = attempts
    result.detail["repairs"] = repair_log
    result.detail["dropped_cases"] = sorted(dropped)

    if not compiled.ok:
        result.n_cases_compiled = 0
        result.error_category, result.error_first_line = metrics.classify_error(
            compiled.output, stage="compile"
        )
        return result

    surviving = metrics.count_gtest_cases(final_source)
    result.n_cases_compiled = len(surviving)
    if repair_log:
        # A repair round may add or remove cases; CSR is measured against what
        # the generator originally wrote, so n_cases stays as first counted and
        # compiled cases are capped at it.
        result.n_cases_compiled = min(result.n_cases_compiled, result.n_cases)
    if dropped:
        # The file only built after pruning; report why the rest failed.
        result.error_category, result.error_first_line = metrics.classify_error(
            attempts[0]["stderr_head"], stage="compile"
        )

    _clear_profiles(project.build_dir, work_dir)
    gtest_json = work_dir / f"{test_file.stem}_gtest.json"
    gtest_json.unlink(missing_ok=True)
    executed, profraw = _run_instrumented(binary, work_dir, gtest_json)
    result.detail["run_exit_code"] = executed.exit_code
    result.detail["run_output_head"] = executed.output[:1500]

    if gtest_json.is_file():
        parsed = metrics.parse_gtest_json(gtest_json.read_text(errors="replace"))
        result.n_cases_passed = parsed["passed"]
        result.detail["cases"] = parsed["cases"]
        # The case count so far is a count of TEST macros in the text. A case
        # the preprocessor removed -- one wrapped in #ifdef NDEBUG, say, in a
        # build that never defines it -- is in the text and not in the binary,
        # and a run that passes every case the binary holds must not be scored
        # as one short. The binary is the authority on how many cases exist.
        if 0 < parsed["total"] < result.n_cases_compiled:
            result.detail["cases_excluded_by_preprocessor"] = (
                result.n_cases_compiled - parsed["total"])
            result.n_cases_compiled = parsed["total"]
    else:
        # No report at all: the binary crashed before writing it.
        result.n_cases_passed = 0

    if result.n_cases_passed < result.n_cases_compiled and not result.error_category:
        result.error_category, result.error_first_line = metrics.classify_error(
            executed.output, stage="runtime"
        )

    # One export serves both conventions: the focal method's own line range,
    # which is the metric the source study defines, and the whole enclosing
    # file, which is the metric its released evaluator computes.
    report = _llvm_export(binary, profraw, focal_file, project.root, work_dir)
    coverage = metrics.llvm_focal_coverage(
        report, focal_file, focal_start, focal_end
    )
    result.detail["coverage_source"] = "llvm-cov" if report else "unavailable"
    result.line_coverage = coverage["line_coverage"]
    result.branch_coverage = coverage["branch_coverage"]
    result.touched_focal = coverage["touched_focal"]
    result.detail["coverage"] = {k: v for k, v in coverage.items() if k != "touched_focal"}
    # The same report scored CITYWALK's way, so the two conventions can be
    # compared without a second run.
    result.detail["coverage_whole_file"] = metrics.llvm_whole_file_coverage(
        report, focal_file
    )
    return result


def toolchain_report() -> dict[str, str | None]:
    """Which required binaries are present — checked before a sweep starts."""
    return {name: shutil.which(name) for name in ("g++", "cmake", "make", "gcov", "gcovr")}


def gtest_available() -> bool:
    return Path("/usr/include/gtest/gtest.h").is_file() or bool(
        list(Path("/usr/local/include").glob("gtest/gtest.h"))
    )
