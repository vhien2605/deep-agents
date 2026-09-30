"""Optional Langfuse tracing for the deep agent.

Tracing activates automatically when LANGFUSE_PUBLIC_KEY and
LANGFUSE_SECRET_KEY are set (see .env.example); otherwise every helper here is
a no-op and the agent runs untraced.
"""

from contextlib import nullcontext

# src.config must be imported before langfuse so load_dotenv() has populated
# the LANGFUSE_* variables the client reads at initialization.
from src.config import LANGFUSE_ENABLED


def langchain_callbacks() -> list:
    """Callbacks for a LangChain/LangGraph `config` dict; empty when disabled."""
    if not LANGFUSE_ENABLED:
        return []
    from langfuse.langchain import CallbackHandler

    return [CallbackHandler()]


def trace_attributes(
    *,
    trace_name: str,
    session_id: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, str] | None = None,
):
    """Context manager that attaches trace-level attributes to every span
    created inside it. Metadata values must be strings of at most 200 chars
    (SDK v4 requirement).
    """
    if not LANGFUSE_ENABLED:
        return nullcontext()
    from langfuse import propagate_attributes

    return propagate_attributes(
        trace_name=trace_name,
        session_id=session_id,
        tags=tags,
        metadata=metadata,
    )


def flush() -> None:
    """Deliver buffered traces; call before a short-lived process exits."""
    if LANGFUSE_ENABLED:
        from langfuse import get_client

        get_client().flush()
