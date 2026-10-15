"""Tests for main.py — CLI entrypoint and the _print_update trace helper.

These tests run under pytest OR as a plain script
(`python tests/test_main.py`), using only the standard library, so they do
not depend on pytest being installed.

Environment note: this repo's `src` package pulls in `deepagents` ->
`langchain` -> `asyncio`, and on a Windows host with a broken Winsock catalog
`import asyncio` raises WinError 10106. To stay hermetic we therefore stub the
`src` package (injecting a fake `create_cpp_unittest_agent`) and load only
`src/config.py` directly (it depends solely on dotenv/os/pathlib, no asyncio).
No `unittest.mock` is used either (it also imports asyncio); a tiny patch
helper replaces it.
"""

import contextlib
import importlib.util
import inspect
import io
import sys
import tempfile
import types
from pathlib import Path
from contextlib import redirect_stdout

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _install_src_stub() -> None:
    """Inject a fake `src` package + real `src.config` into sys.modules.

    This prevents `import main` from triggering the heavy deepagents/langchain
    import chain (which needs asyncio). `create_cpp_unittest_agent` is a
    placeholder overwritten per-test via _patch.
    """
    src_stub = types.ModuleType("src")
    src_stub.__path__ = []  # mark as a package so `from src.config import ...` works
    src_stub.create_cpp_unittest_agent = lambda *a, **k: None  # patched per-test
    sys.modules["src"] = src_stub

    # Load the real src/config.py (dotenv only — no asyncio).
    spec = importlib.util.spec_from_file_location(
        "src.config", _REPO_ROOT / "src" / "config.py"
    )
    config_mod = importlib.util.module_from_spec(spec)
    sys.modules["src.config"] = config_mod
    spec.loader.exec_module(config_mod)
    src_stub.config = config_mod


_install_src_stub()

import main  # noqa: E402  (must follow sys.modules stub setup)


# ---------------------------------------------------------------------------
# Minimal patch helpers (replace unittest.mock, which imports asyncio)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _patch(obj, attr, value):
    """Temporarily set `obj.attr = value`, restoring the old value on exit."""
    old = getattr(obj, attr)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        setattr(obj, attr, old)


@contextlib.contextmanager
def _patch_argv(argv):
    old = sys.argv
    sys.argv = argv
    try:
        yield
    finally:
        sys.argv = old


class _FakeAgent:
    """Stand-in for the compiled LangGraph agent; records stream() calls."""

    def __init__(self, updates=None):
        self._updates = list(updates or [])
        self.stream_calls = []

    def stream(self, data, **kwargs):
        self.stream_calls.append((data, kwargs))
        return iter(self._updates)


class _Recording:
    """Wrap a callable and record how many times / with what it was called."""

    def __init__(self, func=None):
        self.func = func
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.func(*args, **kwargs) if self.func else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capture(func, *args, **kwargs):
    """Run func and return (return_value, stdout_text)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = func(*args, **kwargs)
    return result, buf.getvalue()


def _msg(kind, content="", **extra):
    """Build a lightweight message object with a .type attribute."""
    return types.SimpleNamespace(type=kind, content=content, **extra)


# ===========================================================================
# _print_update — empty / non-dict payloads
# ===========================================================================


def test_print_update_empty_dict():
    _, out = _capture(main._print_update, {})
    assert out == ""


def test_print_update_non_dict_payload_skipped():
    """A node whose payload is not a dict is skipped entirely."""
    _, out = _capture(main._print_update, {"node": "not a dict"})
    assert out == ""


def test_print_update_payload_without_messages_key():
    _, out = _capture(main._print_update, {"node": {"other": 1}})
    assert out == ""


def test_print_update_empty_messages_list():
    _, out = _capture(main._print_update, {"node": {"messages": []}})
    assert out == ""


# ===========================================================================
# _print_update — AI messages
# ===========================================================================


def test_print_update_ai_text_string():
    msg = _msg("ai", "hello world")
    _, out = _capture(main._print_update, {"node": {"messages": [msg]}})
    assert "[node]" in out
    assert "hello world" in out


def test_print_update_ai_empty_text_not_printed():
    msg = _msg("ai", "   ")
    _, out = _capture(main._print_update, {"node": {"messages": [msg]}})
    assert out == ""


def test_print_update_ai_content_as_list_of_dicts():
    msg = _msg(
        "ai",
        [{"type": "text", "text": "part1"}, {"type": "text", "text": "part2"}],
    )
    _, out = _capture(main._print_update, {"node": {"messages": [msg]}})
    assert "part1" in out
    assert "part2" in out


def test_print_update_ai_content_list_with_non_dict_ignored():
    msg = _msg("ai", ["not a dict", 42, {"type": "text", "text": "kept"}])
    _, out = _capture(main._print_update, {"node": {"messages": [msg]}})
    assert "kept" in out


def test_print_update_ai_content_list_all_non_dict():
    msg = _msg("ai", ["x", 42])
    _, out = _capture(main._print_update, {"node": {"messages": [msg]}})
    # join yields "" -> stripped -> no print
    assert out == ""


def test_print_update_ai_with_tool_calls():
    msg = _msg(
        "ai",
        "",
        tool_calls=[{"name": "run_tool", "args": {"k": "v"}}],
    )
    _, out = _capture(main._print_update, {"node": {"messages": [msg]}})
    assert "run_tool" in out
    assert "tool" in out


def test_print_update_ai_tool_calls_none():
    """tool_calls=None should not raise (the `or []` guard)."""
    msg = _msg("ai", "text", tool_calls=None)
    _, out = _capture(main._print_update, {"node": {"messages": [msg]}})
    assert "text" in out


def test_print_update_ai_tool_calls_empty_list():
    msg = _msg("ai", "visible", tool_calls=[])
    _, out = _capture(main._print_update, {"node": {"messages": [msg]}})
    assert "visible" in out


def test_print_update_ai_long_args_truncated():
    long_args = {"k": "x" * 500}
    msg = _msg("ai", "", tool_calls=[{"name": "t", "args": long_args}])
    _, out = _capture(main._print_update, {"node": {"messages": [msg]}})
    # str(args) is truncated to 200 chars, so the full 500 x's must NOT all
    # appear (truncation is in effect). 200 chars hold ~193 x's, so "xxx" is
    # still present — the real guarantee is that far fewer than 500 appear.
    assert out.count("x") < 500


# ===========================================================================
# _print_update — tool messages
# ===========================================================================


def test_print_update_tool_message_string_content():
    msg = _msg("tool", "result data", name="my_tool")
    _, out = _capture(main._print_update, {"node": {"messages": [msg]}})
    assert "my_tool" in out
    assert "result data" in out


def test_print_update_tool_message_non_string_content():
    msg = _msg("tool", {"key": "value"}, name="my_tool")
    _, out = _capture(main._print_update, {"node": {"messages": [msg]}})
    assert "my_tool" in out
    assert "value" in out


def test_print_update_tool_message_no_name_attr():
    """Falls back to 'tool' when the message has no .name attribute."""
    msg = types.SimpleNamespace(type="tool", content="data")
    _, out = _capture(main._print_update, {"node": {"messages": [msg]}})
    assert "<- tool:" in out


def test_print_update_tool_message_long_content_truncated():
    long_text = "A" * 600
    msg = _msg("tool", long_text, name="t")
    _, out = _capture(main._print_update, {"node": {"messages": [msg]}})
    # content is truncated to 400 chars
    assert out.count("A") < 600


# ===========================================================================
# _print_update — unknown message kind / mixed
# ===========================================================================


def test_print_update_unknown_kind_ignored():
    msg = _msg("human", "should not appear")
    _, out = _capture(main._print_update, {"node": {"messages": [msg]}})
    assert out == ""


def test_print_update_mixed_messages():
    ai_msg = _msg("ai", "thinking")
    tool_msg = _msg("tool", "output", name="t1")
    _, out = _capture(
        main._print_update, {"node": {"messages": [ai_msg, tool_msg]}}
    )
    assert "thinking" in out
    assert "output" in out


def test_print_update_multiple_nodes():
    msg1 = _msg("ai", "first")
    msg2 = _msg("ai", "second")
    _, out = _capture(
        main._print_update,
        {"n1": {"messages": [msg1]}, "n2": {"messages": [msg2]}},
    )
    assert "first" in out
    assert "second" in out


# ===========================================================================
# main() — argument parsing and agent invocation
# ===========================================================================


def _run_main(argv_extra, updates=None, tmp_path=None):
    """Invoke main() with patched argv and a fake agent factory.

    Returns (return_code, stdout, fake_create_agent, fake_agent).
    `fake_create_agent` is a _Recording wrapping the factory.
    """
    fake_agent = _FakeAgent(updates)
    fake_create = _Recording(func=lambda *a, **k: fake_agent)
    with _patch(main, "create_cpp_unittest_agent", fake_create):
        with _patch_argv(["main.py"] + argv_extra):
            code, out = _capture(main.main)
    return code, out, fake_create, fake_agent


def test_main_basic_run(tmp_path):
    updates = [{"node": {"messages": [_msg("ai", "hello")]}}]
    code, out, fake_create, agent = _run_main(["--project", str(tmp_path)], updates, tmp_path)
    assert code == 0
    assert len(fake_create.calls) == 1
    args, kwargs = fake_create.calls[0]
    assert args[0] == str(tmp_path)
    assert kwargs["coverage_target"] == main.DEFAULT_COVERAGE_TARGET
    assert len(agent.stream_calls) == 1
    assert "Run finished" in out


def test_main_custom_coverage(tmp_path):
    code, out, fake_create, agent = _run_main(
        ["--project", str(tmp_path), "--coverage", "95"], updates=[], tmp_path=tmp_path
    )
    assert code == 0
    _args, kwargs = fake_create.calls[0]
    assert kwargs["coverage_target"] == 95.0


def test_main_with_task(tmp_path):
    code, out, fake_create, agent = _run_main(
        ["--project", str(tmp_path), "--task", "focus on parser.cpp"],
        updates=[],
        tmp_path=tmp_path,
    )
    assert code == 0
    data, _kw = agent.stream_calls[0]
    messages = data["messages"]
    task_content = messages[0]["content"]
    assert "focus on parser.cpp" in task_content
    assert "Additional instructions" in task_content


def test_main_without_task_no_extra_instructions(tmp_path):
    code, out, fake_create, agent = _run_main(
        ["--project", str(tmp_path)], updates=[], tmp_path=tmp_path
    )
    assert code == 0
    data, _kw = agent.stream_calls[0]
    task_content = data["messages"][0]["content"]
    assert "Additional instructions" not in task_content


def test_main_recursion_limit_passed(tmp_path):
    code, out, fake_create, agent = _run_main(
        ["--project", str(tmp_path), "--recursion-limit", "50"],
        updates=[],
        tmp_path=tmp_path,
    )
    assert code == 0
    _data, kw = agent.stream_calls[0]
    assert kw["config"]["recursion_limit"] == 50


def test_main_default_recursion_limit(tmp_path):
    """No flag -> the configured default, not LangGraph's own 200."""
    from src.config import DEFAULT_RECURSION_LIMIT

    code, out, fake_create, agent = _run_main(
        ["--project", str(tmp_path)], updates=[], tmp_path=tmp_path
    )
    assert code == 0
    _data, kw = agent.stream_calls[0]
    assert kw["config"]["recursion_limit"] == DEFAULT_RECURSION_LIMIT
    assert DEFAULT_RECURSION_LIMIT > 200


def test_main_stream_mode_is_updates(tmp_path):
    code, out, fake_create, agent = _run_main(
        ["--project", str(tmp_path)], updates=[], tmp_path=tmp_path
    )
    assert code == 0
    _data, kw = agent.stream_calls[0]
    assert kw["stream_mode"] == "updates"


def test_main_stream_input_shape(tmp_path):
    """stream() receives {"messages": [{"role":"user","content":...}]}."""
    code, out, fake_create, agent = _run_main(
        ["--project", str(tmp_path)], updates=[], tmp_path=tmp_path
    )
    data, _kw = agent.stream_calls[0]
    assert "messages" in data
    assert data["messages"][0]["role"] == "user"
    assert "coverage" in data["messages"][0]["content"]


def test_main_calls_print_update(tmp_path):
    """Each streamed update is passed to _print_update."""
    updates = [
        {"n1": {"messages": [_msg("ai", "a")]}},
        {"n2": {"messages": [_msg("tool", "b", name="t")]}},
    ]
    fake_agent = _FakeAgent(updates)
    fake_create = _Recording(func=lambda *a, **k: fake_agent)
    call_count = [0]

    real_print = main._print_update

    def counting_print(update):
        call_count[0] += 1
        return real_print(update)

    with _patch(main, "create_cpp_unittest_agent", fake_create):
        with _patch(main, "_print_update", counting_print):
            with _patch_argv(["main.py", "--project", str(tmp_path)]):
                code, out = _capture(main.main)
    assert code == 0
    assert call_count[0] == 2


def test_main_missing_project_arg_exits():
    """argparse should exit with code 2 when --project is missing."""
    with _patch_argv(["main.py"]):
        try:
            main.main()
            assert False, "expected SystemExit"
        except SystemExit as e:
            assert e.code == 2


def test_main_separator_line_printed(tmp_path):
    code, out, fake_create, agent = _run_main(
        ["--project", str(tmp_path)], updates=[], tmp_path=tmp_path
    )
    assert "=" * 72 in out


def test_main_coverage_value_embedded_in_task(tmp_path):
    code, out, fake_create, agent = _run_main(
        ["--project", str(tmp_path), "--coverage", "88.5"],
        updates=[],
        tmp_path=tmp_path,
    )
    assert code == 0
    data, _kw = agent.stream_calls[0]
    assert "88.5" in data["messages"][0]["content"]


# ===========================================================================
# Script runner (fallback when pytest is unavailable)
# ===========================================================================


def _run_all():
    """Discover and run every test_* function in this module (plain runner)."""
    funcs = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and inspect.isfunction(obj)
    ]
    passed = 0
    failed = 0
    for name, fn in funcs:
        kwargs = {}
        for p in inspect.signature(fn).parameters.values():
            if p.name == "tmp_path":
                kwargs["tmp_path"] = Path(tempfile.mkdtemp())
        try:
            fn(**kwargs)
            passed += 1
            print(f"PASS {name}")
        except Exception as e:
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
