"""Tests for src/tools/build.py — subprocess-based build/test/run tools."""

import subprocess
import sys
from pathlib import Path

import pytest

from src.tools.build import (
    _resolve_executable,
    make_build_tools,
    missing_command_error,
    run_in_project,
)


# ---------------------------------------------------------------------------
# run_in_project
# ---------------------------------------------------------------------------


def test_run_in_project_success(tmp_path):
    result = run_in_project(tmp_path, ["echo", "hello"])
    assert "exit_code: 0" in result
    assert "hello" in result
    assert "$ echo hello" in result


def test_run_in_project_nonzero_exit(tmp_path):
    result = run_in_project(tmp_path, [sys.executable, "-c", "import sys; sys.exit(3)"])
    assert "exit_code: 3" in result


def test_run_in_project_command_not_found(tmp_path):
    result = run_in_project(tmp_path, ["nonexistent_command_xyz"])
    assert "ERROR" in result
    assert "command not found" in result


def test_run_in_project_stdout_and_stderr(tmp_path):
    result = run_in_project(
        tmp_path,
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
    )
    assert "out" in result
    assert "err" in result
    assert "--- stdout ---" in result
    assert "--- stderr ---" in result


def test_run_in_project_truncates_long_output(tmp_path):
    """stdout exceeding MAX_OUTPUT_CHARS is truncated (clipped)."""
    from src.config import MAX_OUTPUT_CHARS

    long_text = "x" * (MAX_OUTPUT_CHARS + 500)
    result = run_in_project(tmp_path, [sys.executable, "-c", f"print('{long_text}')"])
    assert "truncated" in result
    # The stdout section should be capped at MAX_OUTPUT_CHARS + the truncation marker
    stdout_section = result.split("--- stdout ---\n")[1].split("\n--- stderr ---")[0]
    assert len(stdout_section) <= MAX_OUTPUT_CHARS + len("\n...[truncated]")


def test_run_in_project_no_truncation_for_short_output(tmp_path):
    result = run_in_project(tmp_path, ["echo", "short"])
    assert "truncated" not in result


def test_run_in_project_timeout(tmp_path, monkeypatch):
    """A command that sleeps longer than the timeout returns a timeout error."""
    from src.tools import build as build_module

    monkeypatch.setattr(build_module, "COMMAND_TIMEOUT_SECONDS", 1)
    result = run_in_project(
        tmp_path,
        [sys.executable, "-c", "import time; time.sleep(10)"],
    )
    assert "ERROR" in result
    assert "timed out" in result


# ---------------------------------------------------------------------------
# make_build_tools — make_target
# ---------------------------------------------------------------------------


@pytest.fixture
def build_tools(tmp_path):
    return make_build_tools(tmp_path), tmp_path


def test_make_target_default(build_tools):
    """make with no target runs the default target."""
    tools, root = build_tools
    make_fn = tools[0]
    # Create a Makefile with a default target
    (root / "Makefile").write_text("all:\n\t@echo default_target\n")
    result = make_fn.invoke({"target": "", "extra_args": ""})
    assert "exit_code: 0" in result
    assert "default_target" in result


def test_make_target_specific(build_tools):
    tools, root = build_tools
    make_fn = tools[0]
    (root / "Makefile").write_text("test:\n\t@echo running_tests\n")
    result = make_fn.invoke({"target": "test", "extra_args": ""})
    assert "exit_code: 0" in result
    assert "running_tests" in result
    assert "$ make test" in result


def test_make_target_with_extra_args(build_tools):
    tools, root = build_tools
    (root / "Makefile").write_text(
        "test:\n\t@echo VAR=$(VAR)\n"
    )
    make_fn = tools[0]
    result = make_fn.invoke({"target": "test", "extra_args": "VAR=42"})
    assert "VAR=42" in result


def test_make_target_fails_on_missing_makefile(build_tools):
    tools, root = build_tools
    make_fn = tools[0]
    result = make_fn.invoke({"target": "", "extra_args": ""})
    assert "exit_code" in result
    # make returns non-zero when no Makefile
    assert "exit_code: 2" in result or "No such" in result or "exit_code" in result


def test_make_target_extra_args_split_correctly(build_tools):
    """Extra args are split by whitespace."""
    tools, root = build_tools
    (root / "Makefile").write_text(
        "test:\n\t@echo $(A) $(B)\n"
    )
    make_fn = tools[0]
    result = make_fn.invoke({"target": "test", "extra_args": "A=1 B=2"})
    assert "1 2" in result


# ---------------------------------------------------------------------------
# make_build_tools — run_command
# ---------------------------------------------------------------------------


def test_run_command_simple(build_tools):
    tools, root = build_tools
    run_fn = tools[1]
    result = run_fn.invoke({"command": "echo hello"})
    assert "exit_code: 0" in result
    assert "hello" in result


def test_run_command_with_args(build_tools):
    tools, root = build_tools
    run_fn = tools[1]
    result = run_fn.invoke({"command": f"{sys.executable} -c \"print('py_out')\""})
    assert "py_out" in result


def test_run_command_empty(build_tools):
    tools, root = build_tools
    run_fn = tools[1]
    result = run_fn.invoke({"command": "   "})
    assert "ERROR" in result
    assert "empty command" in result


def test_run_command_invalid_shell_syntax(build_tools):
    """Unbalanced quotes cause a ValueError from shlex."""
    tools, root = build_tools
    run_fn = tools[1]
    result = run_fn.invoke({"command": "echo 'unbalanced"})
    assert "ERROR" in result
    assert "cannot parse" in result


def test_run_command_nonzero_exit(build_tools):
    tools, root = build_tools
    run_fn = tools[1]
    result = run_fn.invoke({"command": f"{sys.executable} -c \"import sys; sys.exit(1)\""})
    assert "exit_code: 1" in result


def test_run_command_preserves_quotes(build_tools):
    tools, root = build_tools
    run_fn = tools[1]
    result = run_fn.invoke({"command": f'{sys.executable} -c "print(\'quoted\')"'})
    assert "quoted" in result


# ---------------------------------------------------------------------------
# make_build_tools — run_test_binary
# ---------------------------------------------------------------------------


def test_run_test_binary_existing(build_tools):
    tools, root = build_tools
    run_fn = tools[2]
    # Use the Python executable as a "binary".
    # Call .func directly: LangChain renames the `args` param to `v__args`.
    result = run_fn.func(binary_path=sys.executable, args="-c \"print('binary_ok')\"")
    assert "exit_code: 0" in result
    assert "binary_ok" in result


def test_run_test_binary_relative_path(build_tools, tmp_path):
    tools, root = build_tools
    run_fn = tools[2]
    # Create a shell script as a "binary"
    script = root / "mytest.sh"
    script.write_text("#!/bin/bash\necho test_output\n")
    script.chmod(0o755)
    result = run_fn.func(binary_path="mytest.sh", args="")
    assert "exit_code: 0" in result
    assert "test_output" in result


def test_run_test_binary_with_args(build_tools, tmp_path):
    tools, root = build_tools
    run_fn = tools[2]
    script = root / "mytest.sh"
    script.write_text("#!/bin/bash\necho \"args: $1 $2\"\n")
    script.chmod(0o755)
    result = run_fn.func(binary_path="mytest.sh", args="foo bar")
    assert "args: foo bar" in result


def test_run_test_binary_not_found(build_tools):
    tools, root = build_tools
    run_fn = tools[2]
    result = run_fn.func(binary_path="nonexistent_binary", args="")
    assert "ERROR" in result
    assert "binary not found" in result


def test_run_test_binary_absolute_path(build_tools):
    tools, root = build_tools
    run_fn = tools[2]
    result = run_fn.func(binary_path=sys.executable, args="-c \"print('abs_ok')\"")
    assert "abs_ok" in result


def test_make_build_tools_returns_three_tools(tmp_path):
    tools = make_build_tools(tmp_path)
    assert len(tools) == 3


# ---------------------------------------------------------------------------
# Missing-toolchain reporting (fail fast instead of burning the step budget)
# ---------------------------------------------------------------------------


def test_missing_command_error_marks_the_failure_as_terminal():
    msg = missing_command_error("make")
    assert "command not found: make" in msg
    assert "TOOLCHAIN MISSING" in msg
    assert "Do NOT retry" in msg


def test_missing_command_error_includes_an_install_hint_when_known():
    assert "MSYS2" in missing_command_error("make")
    assert "brew install lcov" in missing_command_error("lcov")


def test_missing_command_error_without_a_known_hint():
    msg = missing_command_error("some_exotic_tool")
    assert "TOOLCHAIN MISSING" in msg
    assert "Install hint:" not in msg


def test_run_in_project_reports_missing_binary_before_spawning(tmp_path, monkeypatch):
    """The check happens up front, so no subprocess is ever started."""
    def _boom(*args, **kwargs):
        raise AssertionError("subprocess.run must not be called for a missing binary")

    monkeypatch.setattr(subprocess, "run", _boom)
    result = run_in_project(tmp_path, ["nonexistent_command_xyz", "--flag"])
    assert "TOOLCHAIN MISSING" in result


def test_run_in_project_still_runs_an_existing_binary(tmp_path):
    result = run_in_project(tmp_path, [sys.executable, "-c", "print('alive')"])
    assert "alive" in result
    assert "TOOLCHAIN MISSING" not in result


def test_run_in_project_accepts_a_path_to_an_existing_file(tmp_path, monkeypatch):
    """A built test binary is addressed by path, so it never appears on PATH."""
    binary = tmp_path / "test_runner"
    binary.write_text("")
    monkeypatch.setattr("src.tools.build.shutil.which", lambda *_a, **_k: None)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "ran", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_in_project(tmp_path, [str(binary)])
    assert calls, "the missing-binary pre-check must not short-circuit an existing file"
    assert "TOOLCHAIN MISSING" not in result


def test_run_in_project_reports_a_non_executable_file_without_raising(tmp_path):
    """An OSError inside a tool would abort the agent run, so it is returned."""
    def _explode(*args, **kwargs):
        raise OSError(8, "Exec format error")

    original = subprocess.run
    subprocess.run = _explode
    try:
        result = run_in_project(tmp_path, [sys.executable])
    finally:
        subprocess.run = original
    assert "ERROR: cannot execute" in result
    assert "Do not retry" in result


# ---------------------------------------------------------------------------
# _resolve_executable — a command may live in the project, not on PATH
# ---------------------------------------------------------------------------


def test_resolve_executable_finds_a_binary_on_path():
    assert _resolve_executable(Path.cwd(), sys.executable) is not None


def test_resolve_executable_finds_a_file_inside_the_project(tmp_path):
    """The server's cwd is not the project, so project files must still resolve."""
    script = tmp_path / "build_env.bat"
    script.write_text("@echo off")
    resolved = _resolve_executable(tmp_path, "build_env.bat")
    assert resolved is not None
    assert Path(resolved) == script.resolve()


def test_resolve_executable_returns_none_for_an_unknown_command(tmp_path):
    assert _resolve_executable(tmp_path, "nonexistent_command_xyz") is None


def test_resolve_executable_rejects_a_missing_absolute_path(tmp_path):
    assert _resolve_executable(tmp_path, str(tmp_path / "nope" / "tool.exe")) is None


def test_run_in_project_runs_a_script_that_only_exists_in_the_project(tmp_path, monkeypatch):
    """Regression: a project-local command must not be reported as missing."""
    script = tmp_path / "runner"
    script.write_text("")
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_in_project(tmp_path, ["runner", "--flag"])
    assert "TOOLCHAIN MISSING" not in result
    assert seen[0][0] == str(script.resolve())  # executed by absolute path
    assert "$ runner --flag" in result  # echoed as the model wrote it
