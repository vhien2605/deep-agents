"""Central configuration for the C++ unit-test deep agent.

Model settings come from .env (repo root) or the environment:
    LLM_PROVIDER  — openai (any OpenAI-compatible endpoint, default),
                    openrouter, or anthropic (Claude API)
    LLM_BASE_URL  — endpoint URL; required for openai, optional otherwise
                    (openrouter defaults to https://openrouter.ai/api/v1)
    LLM_API_KEY   — API key for that provider
    LLM_MODEL     — model name, e.g. zai-org/GLM-5.2,
                    anthropic/claude-sonnet-4.5 (openrouter),
                    claude-sonnet-4-5 (anthropic)
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Repo root (where this package and the skills/ directory live).
REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"

load_dotenv(REPO_ROOT / ".env")

LLM_PROVIDERS = ("openai", "openrouter", "anthropic")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai").strip().lower() or "openai"
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")

# Langfuse tracing is optional: it activates only when both keys are present.
# The langfuse SDK reads LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY /
# LANGFUSE_BASE_URL from the environment itself (populated by load_dotenv above).
LANGFUSE_ENABLED = bool(
    os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")
)

# Coverage goal the orchestrator aims for unless the user overrides it.
DEFAULT_COVERAGE_TARGET = 80.0


def _int_env(name: str, default: int) -> int:
    """Read a positive int from the environment, falling back on junk values."""
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def _float_env(name: str, default: float) -> float:
    """Read a non-negative float from the environment, junk values excepted."""
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value >= 0 else default


# Sampling temperature. Defaults to 0 (greedy decoding) so a run is
# reproducible: with LangChain's own default of 0.7, the same prompt yields a
# different test file every time and no measurement can be repeated. This also
# matches the setup used by the CITYWALK evaluation, which the bench/ harness
# reproduces.
LLM_TEMPERATURE = _float_env("LLM_TEMPERATURE", 0.0)

# Per-request timeout. Long enough that a slow completion is waited out rather
# than retried: a retry costs another full prompt and skews the token totals a
# benchmark run reports.
LLM_TIMEOUT_SECONDS = _float_env("LLM_TIMEOUT_SECONDS", 600.0)


# LangGraph counts every node execution as one super-step, and one agent turn
# costs three of them (model -> TodoListMiddleware.after_model -> tools), so a
# limit of N allows only about N/3 tool calls. 200 (the LangGraph default) is
# ~66 tool calls, which the analyze -> solve -> generate -> fix -> measure loop
# exhausts on any non-trivial project. Every subagent inherits this same limit
# for its own run, and a subagent that exhausts it aborts the whole session.
DEFAULT_RECURSION_LIMIT = _int_env("AGENT_RECURSION_LIMIT", 600)

# Subprocess limits for build/test/coverage commands.
COMMAND_TIMEOUT_SECONDS = 300
MAX_OUTPUT_CHARS = 20000


def require_llm_config() -> None:
    """Fail fast with a clear message when .env is missing or incomplete."""
    if LLM_PROVIDER not in LLM_PROVIDERS:
        raise RuntimeError(
            f"Unknown LLM_PROVIDER={LLM_PROVIDER!r} — use one of "
            f"{', '.join(LLM_PROVIDERS)}."
        )
    required = [("LLM_API_KEY", LLM_API_KEY), ("LLM_MODEL", LLM_MODEL)]
    if LLM_PROVIDER == "openai":
        required.insert(0, ("LLM_BASE_URL", LLM_BASE_URL))
    missing = [name for name, value in required if not value]
    if missing:
        raise RuntimeError(
            f"Missing {', '.join(missing)} — set them in {REPO_ROOT / '.env'} "
            "(see .env.example)."
        )
