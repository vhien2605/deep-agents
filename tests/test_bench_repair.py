"""Tests for bench/repair.py — the single bounded LLM repair round."""

import pytest

from bench import repair

FIXED_FILE = """\
#include <gtest/gtest.h>
TEST(Suite, Case) { EXPECT_TRUE(true); }
"""


class FakeMessage:
    def __init__(self, content, usage=None, metadata=None):
        self.content = content
        self.usage_metadata = usage or {}
        self.response_metadata = metadata or {}


class FakeModel:
    """Records the prompt it was given and returns a scripted reply."""

    def __init__(self, reply, usage=None, metadata=None, raises=None):
        self.reply, self.usage, self.metadata, self.raises = reply, usage, metadata, raises
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        if self.raises:
            raise self.raises
        return FakeMessage(self.reply, self.usage, self.metadata)


# ---------------------------------------------------------------------------
# Extracting the file from a reply


def test_extract_code_unwraps_a_fenced_block():
    assert repair.extract_code(f"Here:\n```cpp\n{FIXED_FILE}```\nDone.") == FIXED_FILE.strip()


def test_extract_code_takes_the_longest_of_several_fences():
    text = "```cpp\nshort\n```\ntext\n```cpp\n" + FIXED_FILE + "```"
    assert repair.extract_code(text) == FIXED_FILE.strip()


def test_extract_code_accepts_a_bare_reply():
    assert repair.extract_code(FIXED_FILE) == FIXED_FILE.strip()


@pytest.mark.parametrize(
    "text, expected",
    [
        (FIXED_FILE, True),
        ("I cannot help with that request.", False),
        ("", False),
        ("TEST without braces", False),
    ],
)
def test_looks_like_test_file(text: str, expected: bool):
    assert repair.looks_like_test_file(text) is expected


# ---------------------------------------------------------------------------
# The repair round


def test_repair_once_returns_the_corrected_file():
    model = FakeModel(f"```cpp\n{FIXED_FILE}```")
    fixed, info = repair.repair_once(model, "broken", "error: boom")
    assert fixed == FIXED_FILE.strip()
    assert info["error"] == ""


def test_repair_once_sends_the_file_and_the_diagnostics():
    model = FakeModel(FIXED_FILE)
    repair.repair_once(model, "THE BROKEN FILE", "error: THE DIAGNOSTIC")
    prompt = model.calls[0][0]["content"]
    assert "THE BROKEN FILE" in prompt
    assert "THE DIAGNOSTIC" in prompt


def test_repair_once_calls_the_model_exactly_once():
    """The budget is one round; the module must never retry on its own."""
    model = FakeModel(FIXED_FILE)
    repair.repair_once(model, "broken", "error")
    assert len(model.calls) == 1


def test_repair_once_truncates_a_huge_error_log():
    model = FakeModel(FIXED_FILE)
    repair.repair_once(model, "broken", "x" * 50_000)
    assert len(model.calls[0][0]["content"]) < 20_000


def test_repair_once_rejects_a_reply_that_is_not_a_test_file():
    """A refusal must not be written over a file that at least parsed."""
    model = FakeModel("I'm sorry, I can't do that.")
    fixed, info = repair.repair_once(model, "broken", "error")
    assert fixed is None
    assert "did not contain a test file" in info["error"]


def test_repair_once_survives_a_model_error():
    model = FakeModel(FIXED_FILE, raises=RuntimeError("endpoint down"))
    fixed, info = repair.repair_once(model, "broken", "error")
    assert fixed is None
    assert "endpoint down" in info["error"]


def test_repair_once_reports_token_usage():
    model = FakeModel(FIXED_FILE, usage={"total_tokens": 512})
    _, info = repair.repair_once(model, "broken", "error")
    assert info["tokens"] == 512


def test_repair_once_sums_usage_when_no_total_is_given():
    model = FakeModel(FIXED_FILE, usage={"input_tokens": 300, "output_tokens": 90})
    _, info = repair.repair_once(model, "broken", "error")
    assert info["tokens"] == 390


def test_repair_once_records_the_model_that_answered():
    model = FakeModel(FIXED_FILE, metadata={"model_name": "served-model"})
    _, info = repair.repair_once(model, "broken", "error")
    assert info["model"] == "served-model"


def test_repair_once_handles_block_style_content():
    model = FakeModel([{"type": "text", "text": FIXED_FILE}])
    fixed, _ = repair.repair_once(model, "broken", "error")
    assert fixed == FIXED_FILE.strip()
