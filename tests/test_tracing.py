"""Tests for src/tracing.py — optional Langfuse tracing helpers."""

from contextlib import nullcontext

from src import tracing


# ---------------------------------------------------------------------------
# Disabled mode: every helper must be a harmless no-op
# ---------------------------------------------------------------------------


def test_callbacks_empty_when_disabled(monkeypatch):
    monkeypatch.setattr(tracing, "LANGFUSE_ENABLED", False)
    assert tracing.langchain_callbacks() == []


def test_trace_attributes_nullcontext_when_disabled(monkeypatch):
    monkeypatch.setattr(tracing, "LANGFUSE_ENABLED", False)
    ctx = tracing.trace_attributes(trace_name="t")
    assert isinstance(ctx, nullcontext)
    with ctx:  # must be enterable
        pass


def test_flush_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(tracing, "LANGFUSE_ENABLED", False)
    tracing.flush()  # must not raise


# ---------------------------------------------------------------------------
# Enabled mode: helpers return real Langfuse objects
# ---------------------------------------------------------------------------


def test_callbacks_when_enabled(monkeypatch):
    monkeypatch.setattr(tracing, "LANGFUSE_ENABLED", True)
    callbacks = tracing.langchain_callbacks()
    assert len(callbacks) == 1
    assert type(callbacks[0]).__name__ == "LangchainCallbackHandler"


def test_flush_delivers_buffered_traces_when_enabled(monkeypatch):
    flushed = []

    class _Client:
        def flush(self):
            flushed.append(True)

    monkeypatch.setattr(tracing, "LANGFUSE_ENABLED", True)
    monkeypatch.setattr("langfuse.get_client", lambda: _Client())
    tracing.flush()
    assert flushed == [True]


def test_trace_attributes_context_manager_when_enabled(monkeypatch):
    monkeypatch.setattr(tracing, "LANGFUSE_ENABLED", True)
    with tracing.trace_attributes(
        trace_name="test-trace",
        session_id="s1",
        tags=["test"],
        metadata={"k": "v"},
    ):
        pass  # entering and exiting must not raise
