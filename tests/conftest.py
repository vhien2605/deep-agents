"""Shared test fixtures."""

import pytest

from src import tracing


@pytest.fixture(autouse=True)
def _disable_langfuse_tracing(monkeypatch):
    """Keep tests from sending real traces to Langfuse when .env has keys.

    Tests that exercise enabled-mode behavior re-enable it explicitly.
    """
    monkeypatch.setattr(tracing, "LANGFUSE_ENABLED", False)
