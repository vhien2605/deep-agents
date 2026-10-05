"""Build and test-run tools that drive the target project's Makefile."""

import shutil
import subprocess
from pathlib import Path

from langchain_core.tools import tool

from src.config import COMMAND_TIMEOUT_SECONDS, MAX_OUTPUT_CHARS

# How to obtain each toolchain binary, so a missing one is reported as an
# actionable prerequisite instead of something the model tries to work around.
INSTALL_HINTS = {
    "make": "GNU make — Windows: MSYS2/MinGW-w64 (installs `mingw32-make`) or run under WSL; Linux: apt install build-essential; macOS: xcode-select --install",
    "mingw32-make": "MSYS2/MinGW-w64 toolchain",
    "g++": "GCC — Windows: MSYS2/MinGW-w64 or WSL; Linux: apt install g++; macOS: xcode-select --install",
    "gcc": "GCC — see the g++ hint",
    "clang++": "LLVM/clang toolchain",
    "cmake": "CMake — https://cmake.org/download/",
    "gcov": "ships with GCC; installing g++ provides it",
    "lcov": "lcov — Linux: apt install lcov; macOS: brew install lcov; Windows: only via WSL/MSYS2",
    "gcovr": "pip install gcovr (a lcov alternative that works on Windows)",
    "mvn": "Apache Maven — https://maven.apache.org/download.cgi",
    "java": "a JDK (Temurin, Zulu, ...)",
}


def missing_command_error(exe: str) -> str:
    """Terminal error for a binary that is not installed.

    Retrying cannot help, so the message says so explicitly: without it the
    model reads `command not found` as a transient failure and burns the whole
    recursion budget trying variants of the same command.
    """
    hint = INSTALL_HINTS.get(exe)
    return (
        f"ERROR: command not found: {exe}\n"
        f"TOOLCHAIN MISSING — this is a prerequisite failure, not a transient one. "
        f"Every invocation of `{exe}` will fail identically.\n"
        f"Do NOT retry this command, and do NOT try spelling variants or alternative "
        f"front-ends for the same tool.\n"
        + (f"Install hint: {hint}\n" if hint else "")
        + "Stop the current build/coverage loop, report this to the user as an unmet "
        "prerequisite, and continue only with work that does not need this tool."
    )


def _resolve_executable(project_root: Path, exe: str) -> str | None:
    """Absolute path to run `exe` with, or None when nothing provides it.

    A command can be a binary on PATH (`make`), a path relative to the project
    (`./test_runner`, `build_env.bat`), or an absolute path. Project-relative
    ones are resolved explicitly: the server's own working directory is not the
    project, and on Windows CreateProcess searches the *parent's* directory
    rather than the child's `cwd`.
    """
    path = Path(exe)
    if path.is_absolute():
        return str(path) if path.is_file() else None
    in_project = project_root / path
    if in_project.is_file():
        return str(in_project.resolve())
    if path.is_file():  # relative to this process's cwd
        return str(path.resolve())
    return shutil.which(exe)


def run_in_project(project_root: Path, cmd: list[str]) -> str:
    """Run a command in the project root and format its result for the model."""
    # Check before spawning: on Windows a missing binary surfaces in several
    # different ways, and we want one consistent, unmistakable message.
    resolved = _resolve_executable(project_root, cmd[0])
    if resolved is None:
        return missing_command_error(cmd[0])
    # Execute the resolved path, but echo the command the model asked for.
    run_cmd = [resolved, *cmd[1:]]
    try:
        proc = subprocess.run(
            run_cmd,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return missing_command_error(cmd[0])
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {COMMAND_TIMEOUT_SECONDS}s: {' '.join(cmd)}"
    except OSError as exc:
        # e.g. a file that exists but is not executable on this platform
        # (WinError 193 for a .sh on Windows). Returned, not raised: an
        # exception escaping a tool aborts the whole agent run.
        return (
            f"ERROR: cannot execute {cmd[0]}: {exc}\n"
            "The file exists but is not an executable this platform can run. "
            "Do not retry it as-is."
        )

    def clip(s: str) -> str:
        return s if len(s) <= MAX_OUTPUT_CHARS else s[:MAX_OUTPUT_CHARS] + "\n...[truncated]"

    return (
        f"$ {' '.join(cmd)}\n"
        f"exit_code: {proc.returncode}\n"
        f"--- stdout ---\n{clip(proc.stdout)}\n"
        f"--- stderr ---\n{clip(proc.stderr)}"
    )


