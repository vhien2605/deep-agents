"""Per-project settings for the CITYWALK benchmark suite.

Each project in the suite needs something the others do not: a dependency
switched off, a header the compiler no longer includes implicitly, or a build
system that is not CMake. Keeping those here means `bench.harness` stays
general and the exceptions are visible in one place, each with its reason.
"""

from dataclasses import dataclass, field

# Focal-method counts reported in Table 3 of the paper, used by
# `--paper-sample` to draw a set of the same size. The paper does not publish
# which methods it selected, so a sample of matching size is the closest an
# independent replication can get; the selection itself is seeded and therefore
# reproducible.
PAPER_FOCAL_COUNTS = {
    "hjson-cpp": 25,
    "tinyxml2": 158,
    "yaml-cpp": 204,
    "re2": 146,
    "ninja": 238,
    "leveldb": 220,
    "json.cpp": 34,
    "glomap": 32,
    "papy": 25,
    "mlx": 206,
}

# Files CITYWALK's evaluator skips by name, per project (evaluator.py).
FILE_EXCLUSIONS = {
    "re2": ("unicode", "perl_groups"),
}


@dataclass(frozen=True)
class ProjectConfig:
    """How to build one project with coverage instrumentation."""

    cmake_args: tuple[str, ...] = ()
    extra_cxx_flags: tuple[str, ...] = ()
    cxx_standard: str = "c++17"
    # Sources to compile directly when the project ships no CMakeLists.txt.
    manual_source_globs: tuple[str, ...] = ()
    # Translation units to leave out of the library — an entry point cannot be
    # linked into a test binary that already has main().
    manual_exclude_names: tuple[str, ...] = ("main.cpp", "main.cc")
    link_flags: tuple[str, ...] = ()
    # Extra header search paths for compiling generated tests. Absolute paths
    # are used as-is; relative ones resolve against the build directory, which
    # is where FetchContent dependencies (`_deps/<dep>-src/...`) land.
    extra_include_dirs: tuple[str, ...] = ()
    notes: str = ""


CONFIGS: dict[str, ProjectConfig] = {
    "leveldb": ProjectConfig(
        cmake_args=(
            "-DLEVELDB_BUILD_TESTS=OFF",
            "-DLEVELDB_BUILD_BENCHMARKS=OFF",
        ),
        extra_cxx_flags=("-DLEVELDB_PLATFORM_POSIX", "-fno-rtti"),
        notes="Its benchmark and test targets need git submodules that the "
              "distributed snapshot does not carry. The build defines "
              "LEVELDB_PLATFORM_POSIX, without which port/port.h includes no "
              "implementation and every test touching db/db_impl.h fails to "
              "compile — a harness gap, not a model failure. Its CMakeLists "
              "also appends -fno-rtti, so libleveldb.a carries no typeinfo: a "
              "test compiled with RTTI that subclasses Env or EnvWrapper — the "
              "ordinary way to mock leveldb — emits a vtable referencing "
              "`typeinfo for leveldb::Env`, which the archive cannot resolve. "
              "The tests must be compiled the same way the library was.",
    ),
    "yaml-cpp": ProjectConfig(
        cmake_args=("-DYAML_CPP_BUILD_TESTS=OFF", "-DYAML_CPP_BUILD_TOOLS=OFF"),
        notes="Builds a vendored googletest whose sources no longer compile "
              "under gcc 13; the library itself is unaffected.",
    ),
    "hjson-cpp": ProjectConfig(
        extra_cxx_flags=("-include", "cstdint"),
        notes="Uses std::int64_t without including <cstdint>, which gcc 13 no "
              "longer provides transitively.",
    ),
    "mlx": ProjectConfig(
        cmake_args=("-DMLX_BUILD_TESTS=OFF", "-DMLX_BUILD_EXAMPLES=OFF",
                    "-DMLX_BUILD_BENCHMARKS=OFF", "-DMLX_BUILD_METAL=OFF",
                    "-DLAPACK_INCLUDE_DIRS=/usr/include",
                    "-DBLAS_INCLUDE_DIRS=/usr/include"),
        link_flags=("-lblas", "-llapack", "-ldl"),
        notes="Metal is macOS-only. Its CMake reads the LAPACK/BLAS header "
              "directories from cache variables that FindLAPACK leaves unset "
              "on Debian layouts, so they are pointed at /usr/include here.",
    ),
    "glomap": ProjectConfig(
        cmake_args=("-DTESTS_ENABLED=OFF",),
        extra_include_dirs=("/usr/include/eigen3", "_deps/colmap-src/src"),
        notes="Needs Eigen3, installed in the image. Glomap's own sources "
              "include COLMAP headers (`colmap/...`) that FetchContent places "
              "under the build directory, and Eigen lives in a versioned "
              "include directory; generated tests need both on the search "
              "path or every include of them reads as a model failure.",
    ),
    "papy": ProjectConfig(
        cxx_standard="c++23",
        manual_source_globs=("src/*.cpp",),
        link_flags=("-lssl", "-lcrypto", "-lz"),
        notes="Ships a hand-written makefile that produces an executable, not "
              "a library, so its sources are compiled directly instead.",
    ),
}

DEFAULT_CONFIG = ProjectConfig()


def _project_key(name: str) -> str:
    """Strip the commit suffix from a dataset directory name."""
    # Directory names look like `re2-bc42365...`; the commit is 40 hex chars.
    parts = name.rsplit("-", 1)
    if len(parts) == 2 and len(parts[1]) >= 32 and all(
        c in "0123456789abcdef" for c in parts[1].lower()
    ):
        return parts[0]
    return name


def config_for(project_name: str) -> ProjectConfig:
    return CONFIGS.get(_project_key(project_name), DEFAULT_CONFIG)


def paper_focal_count(project_name: str) -> int | None:
    return PAPER_FOCAL_COUNTS.get(_project_key(project_name))


def excluded_prefixes(project_name: str) -> tuple[str, ...]:
    return FILE_EXCLUSIONS.get(_project_key(project_name), ())
