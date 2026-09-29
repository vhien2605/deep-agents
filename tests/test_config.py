"""Tests for src/config.py — configuration and LLM config validation."""

import os
from pathlib import Path

import pytest

from src import config as config_module
from src.config import (
    COMMAND_TIMEOUT_SECONDS,
    DEFAULT_COVERAGE_TARGET,
    DEFAULT_RECURSION_LIMIT,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    MAX_OUTPUT_CHARS,
    REPO_ROOT,
    SKILLS_DIR,
    require_llm_config,
)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


def test_repo_root_is_directory():
    assert REPO_ROOT.is_dir()


def test_skills_dir_under_repo_root():
    assert SKILLS_DIR == REPO_ROOT / "skills"


def test_default_coverage_target_is_80():
    assert DEFAULT_COVERAGE_TARGET == 80.0


def test_command_timeout_positive():
    assert COMMAND_TIMEOUT_SECONDS > 0


def test_max_output_chars_positive():
    assert MAX_OUTPUT_CHARS > 0


def test_llm_config_values_are_strings():
    assert isinstance(LLM_BASE_URL, str)
    assert isinstance(LLM_API_KEY, str)
    assert isinstance(LLM_MODEL, str)


# ---------------------------------------------------------------------------
# require_llm_config
# ---------------------------------------------------------------------------


def test_require_llm_config_raises_when_missing(monkeypatch):
    """When all three env vars are empty, RuntimeError is raised."""
    monkeypatch.setattr(config_module, "LLM_BASE_URL", "")
    monkeypatch.setattr(config_module, "LLM_API_KEY", "")
    monkeypatch.setattr(config_module, "LLM_MODEL", "")
    with pytest.raises(RuntimeError, match="Missing"):
        require_llm_config()


def test_require_llm_config_raises_when_partial(monkeypatch):
    """When only some vars are set, the missing ones are reported."""
    monkeypatch.setattr(config_module, "LLM_BASE_URL", "http://localhost:8080/v1")
    monkeypatch.setattr(config_module, "LLM_API_KEY", "")
    monkeypatch.setattr(config_module, "LLM_MODEL", "")
    with pytest.raises(RuntimeError) as exc_info:
        require_llm_config()
    msg = str(exc_info.value)
    assert "LLM_API_KEY" in msg
    assert "LLM_MODEL" in msg
    assert "LLM_BASE_URL" not in msg.split("—")[0]  # not in the missing list


def test_require_llm_config_passes_when_all_set(monkeypatch):
    """When all three vars are set, no exception is raised."""
    monkeypatch.setattr(config_module, "LLM_BASE_URL", "http://localhost:8080/v1")
    monkeypatch.setattr(config_module, "LLM_API_KEY", "sk-test")
    monkeypatch.setattr(config_module, "LLM_MODEL", "test-model")
    require_llm_config()  # should not raise


def test_require_llm_config_error_mentions_env_file(monkeypatch):
    """The error message points the user to .env.example."""
    monkeypatch.setattr(config_module, "LLM_BASE_URL", "")
    monkeypatch.setattr(config_module, "LLM_API_KEY", "")
    monkeypatch.setattr(config_module, "LLM_MODEL", "")
    with pytest.raises(RuntimeError) as exc_info:
        require_llm_config()
    assert ".env" in str(exc_info.value)


def test_require_llm_config_error_lists_all_three(monkeypatch):
    """When all are missing, all three names are listed."""
    monkeypatch.setattr(config_module, "LLM_BASE_URL", "")
    monkeypatch.setattr(config_module, "LLM_API_KEY", "")
    monkeypatch.setattr(config_module, "LLM_MODEL", "")
    with pytest.raises(RuntimeError) as exc_info:
        require_llm_config()
    msg = str(exc_info.value)
    assert "LLM_BASE_URL" in msg
    assert "LLM_API_KEY" in msg
    assert "LLM_MODEL" in msg


# ---------------------------------------------------------------------------
# Recursion budget
# ---------------------------------------------------------------------------


def test_default_recursion_limit_exceeds_langgraph_default():
    """One agent turn costs 3 super-steps, so 200 is only ~66 tool calls."""
    assert DEFAULT_RECURSION_LIMIT > 200


def test_int_env_reads_the_variable(monkeypatch):
    monkeypatch.setenv("AGENT_RECURSION_LIMIT_TEST", "1234")
    assert config_module._int_env("AGENT_RECURSION_LIMIT_TEST", 600) == 1234


def test_int_env_falls_back_when_unset():
    assert config_module._int_env("DEFINITELY_UNSET_VAR_XYZ", 600) == 600


@pytest.mark.parametrize("raw", ["", "abc", "12.5", "0", "-5"])
def test_int_env_falls_back_on_junk_or_nonpositive(monkeypatch, raw):
    monkeypatch.setenv("AGENT_RECURSION_LIMIT_TEST", raw)
    assert config_module._int_env("AGENT_RECURSION_LIMIT_TEST", 600) == 600


# ---------------------------------------------------------------------------
# Sampling temperature


def test_llm_temperature_defaults_to_greedy_decoding():
    """A benchmark run must be reproducible, so sampling is off by default."""
    assert config_module._float_env("LLM_TEMPERATURE_UNSET_XYZ", 0.0) == 0.0


def test_float_env_reads_a_configured_value(monkeypatch):
    monkeypatch.setenv("BENCH_TEMP_TEST", "0.7")
    assert config_module._float_env("BENCH_TEMP_TEST", 0.0) == 0.7


def test_float_env_falls_back_on_junk(monkeypatch):
    monkeypatch.setenv("BENCH_TEMP_TEST", "hot")
    assert config_module._float_env("BENCH_TEMP_TEST", 0.0) == 0.0


def test_float_env_rejects_negative_values(monkeypatch):
    monkeypatch.setenv("BENCH_TEMP_TEST", "-1.5")
    assert config_module._float_env("BENCH_TEMP_TEST", 0.3) == 0.3


def test_float_env_accepts_zero(monkeypatch):
    """Zero is the meaningful default, so it must not be treated as unset."""
    monkeypatch.setenv("BENCH_TEMP_TEST", "0")
    assert config_module._float_env("BENCH_TEMP_TEST", 0.9) == 0.0
