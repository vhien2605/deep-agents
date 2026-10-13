"""Tests for src/agent.py — model selection and subagent/tool wiring."""

import pytest
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from src import agent as agent_module
from src.agent import create_cpp_unittest_agent
from src import config as config_module
from src.llm import build_chat_model


@pytest.fixture
def project(tmp_path):
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n")
    return tmp_path


def test_missing_project_root_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="project root not found"):
        create_cpp_unittest_agent(tmp_path / "nope", model=FakeListChatModel(responses=["x"]))


def test_explicit_model_is_used_without_touching_the_env(project, monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("must not build a model when one is supplied")

    monkeypatch.setattr(agent_module, "build_chat_model", fail)
    assert create_cpp_unittest_agent(project, model=FakeListChatModel(responses=["x"])) is not None


def _configure(monkeypatch, provider, base_url=""):
    monkeypatch.setattr(config_module, "LLM_PROVIDER", provider)
    monkeypatch.setattr(config_module, "LLM_MODEL", "test-model")
    monkeypatch.setattr(config_module, "LLM_BASE_URL", base_url)
    monkeypatch.setattr(config_module, "LLM_API_KEY", "secret")


def _capture(monkeypatch, module, name):
    built = {}

    def fake(**kwargs):
        built.update(kwargs)
        return FakeListChatModel(responses=["x"])

    monkeypatch.setattr(module, name, fake)
    return built


def test_openai_compatible_endpoint(monkeypatch):
    import langchain_openai

    _configure(monkeypatch, "openai", "http://endpoint/v1")
    built = _capture(monkeypatch, langchain_openai, "ChatOpenAI")
    build_chat_model()
    assert built["model"] == "test-model"
    assert built["base_url"] == "http://endpoint/v1"
    assert built["temperature"] == config_module.LLM_TEMPERATURE
    assert built["timeout"] == config_module.LLM_TIMEOUT_SECONDS
    assert built["n"] == 1
    assert built["disable_streaming"] is True
    # No max_tokens: a long test file is never truncated mid-file.
    assert "max_tokens" not in built


def test_openrouter_defaults_its_base_url(monkeypatch):
    import langchain_openai

    _configure(monkeypatch, "openrouter")
    built = _capture(monkeypatch, langchain_openai, "ChatOpenAI")
    build_chat_model()
    assert built["base_url"] == config_module.OPENROUTER_BASE_URL


def test_anthropic_uses_claude_client(monkeypatch):
    import langchain_anthropic

    _configure(monkeypatch, "anthropic")
    built = _capture(monkeypatch, langchain_anthropic, "ChatAnthropic")
    build_chat_model()
    assert built["model"] == "test-model"
    assert "base_url" not in built
    assert built["max_tokens"] > 0


def test_incomplete_llm_config_fails_fast(project, monkeypatch):
    _configure(monkeypatch, "openai", "")
    with pytest.raises(RuntimeError, match="Missing LLM_BASE_URL"):
        create_cpp_unittest_agent(project)


def test_unknown_provider_fails_fast(monkeypatch):
    _configure(monkeypatch, "gemini")
    with pytest.raises(RuntimeError, match="Unknown LLM_PROVIDER"):
        build_chat_model()


def test_symbolic_tools_are_available_to_the_orchestrator(project, monkeypatch):
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return "agent"

    monkeypatch.setattr(agent_module, "create_deep_agent", fake_create)
    create_cpp_unittest_agent(project, model=FakeListChatModel(responses=["x"]))

    names = {t.name for t in captured["tools"]}
    assert {"solve_branch_inputs", "extract_path_conditions", "solve_path_constraints"} <= names


def test_path_solver_subagent_is_registered_with_the_solver_tools(project, monkeypatch):
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return "agent"

    monkeypatch.setattr(agent_module, "create_deep_agent", fake_create)
    create_cpp_unittest_agent(project, model=FakeListChatModel(responses=["x"]))

    subagents = {s["name"]: s for s in captured["subagents"]}
    assert "path-solver" in subagents
    assert "solve_branch_inputs" in {t.name for t in subagents["path-solver"]["tools"]}
    # The generator needs them too, so it can solve a branch it was not given.
    assert "solve_branch_inputs" in {t.name for t in subagents["test-generator"]["tools"]}


def test_path_solver_runs_before_generation_in_the_orchestrator_prompt(project, monkeypatch):
    captured = {}
    monkeypatch.setattr(agent_module, "create_deep_agent", lambda **kw: captured.update(kw) or "a")
    create_cpp_unittest_agent(project, model=FakeListChatModel(responses=["x"]))

    prompt = captured["system_prompt"]
    assert prompt.index("SOLVE:") < prompt.index("GENERATE:")


def test_skills_directory_is_passed_through(project, monkeypatch):
    captured = {}
    monkeypatch.setattr(agent_module, "create_deep_agent", lambda **kw: captured.update(kw) or "a")
    create_cpp_unittest_agent(project, model=FakeListChatModel(responses=["x"]))
    assert captured["skills"] and captured["skills"][0].endswith("skills")


def test_coverage_target_reaches_the_prompt(project, monkeypatch):
    captured = {}
    monkeypatch.setattr(agent_module, "create_deep_agent", lambda **kw: captured.update(kw) or "a")
    create_cpp_unittest_agent(project, coverage_target=95.0, model=FakeListChatModel(responses=["x"]))
    assert "95.0" in captured["system_prompt"]


def test_todo_middleware_is_registered(project, monkeypatch):
    """deepagents >= 0.7 dropped the built-in todo list, but the orchestrator
    prompt still tells the agent to plan with one — so we must pass it."""
    captured = {}
    monkeypatch.setattr(agent_module, "create_deep_agent", lambda **kw: captured.update(kw) or "a")
    create_cpp_unittest_agent(project, model=FakeListChatModel(responses=["x"]))
    assert any(isinstance(m, TodoListMiddleware) for m in captured["middleware"])


def test_write_todos_is_bound_to_the_real_agent(project):
    """End-to-end guard: the compiled graph actually exposes write_todos."""
    agent = create_cpp_unittest_agent(project, model=FakeListChatModel(responses=["x"]))
    tools_node = agent.nodes["tools"]
    tools_node = getattr(tools_node, "bound", tools_node)
    assert "write_todos" in tools_node.tools_by_name
