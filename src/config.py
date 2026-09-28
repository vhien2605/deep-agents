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


