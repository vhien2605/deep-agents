"""One bounded LLM repair round for a test file that failed to compile.

CITYWALK fixes generated tests in three phases, of which only the last calls a
model: two rule-based passes, then *one* LLM round; a case still failing
compilation after that is dropped from the suite rather than repaired further
(Algorithm 1, lines 9-14). Reproducing that comparison means the repair budget
has to be enforced by the harness, not left to an agent that decides for itself
when to stop — so this module owns the round, and the caller owns the count.

Assertion failures are never repaired here. The paper repairs compilation
errors only; a test that compiles but fails at run time counts against EPR,
which is precisely what that metric is meant to capture.
"""

import re

from langchain_core.language_models.chat_models import BaseChatModel

# Mirrors the fixing prompt of the paper (Figure 6): the file, the diagnostics,
# and an instruction to return the corrected file — no examples, no dialogue.
REPAIR_PROMPT = """\
You are a highly skilled developer and expert in C++ and testing frameworks. \
I will provide the content of a test file along with the related compilation \
error messages. Please analyze the situation carefully, identify the causes of \
the errors, and modify the test file to fix the errors.

// Test File Content:
{test_file}

// Compilation Error Messages:
{error_message}

Return the complete corrected C++ test file and nothing else. Do not explain \
the changes. Do not wrap the file in prose. Keep every test case that can be \
made to compile; delete a case only when it cannot be fixed.
"""

# The model may still wrap its answer in a fence despite the instruction.
FENCE_RE = re.compile(r"```(?:cpp|c\+\+|cxx)?\s*\n(.*?)```", re.DOTALL)

# Enough of the compiler output to diagnose from, without burying the file.
MAX_ERROR_CHARS = 6000


def extract_code(response: str) -> str:
    """The C++ source from a model response, fenced or bare."""
    fenced = FENCE_RE.findall(response)
    if fenced:
        # Several fences: the longest is the file, the rest are fragments.
        return max(fenced, key=len).strip()
    return response.strip()


def looks_like_test_file(text: str) -> bool:
    """Guard against a refusal or an explanation being written to disk."""
    return "TEST" in text and "{" in text and "}" in text


def repair_once(
    model: BaseChatModel, test_source: str, error_output: str
) -> tuple[str | None, dict]:
    """Ask the model to fix a test file. Returns (fixed source or None, info).

    None means the round produced nothing usable — a refusal, an explanation,
    or an empty reply — and the caller should proceed as if no repair happened
    rather than overwrite a valid file with prose.
    """
    prompt = REPAIR_PROMPT.format(
        test_file=test_source,
        error_message=error_output[:MAX_ERROR_CHARS],
    )
    info: dict = {"tokens": 0, "model": "", "error": ""}
    try:
        response = model.invoke([{"role": "user", "content": prompt}])
    except Exception as exc:  # noqa: BLE001 — a failed repair is not a failed run
        info["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return None, info

    usage = getattr(response, "usage_metadata", None) or {}
    info["tokens"] = int(usage.get("total_tokens", 0)) or (
        int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
    )
    metadata = getattr(response, "response_metadata", None) or {}
    info["model"] = str(metadata.get("model_name") or metadata.get("model") or "")

    content = response.content
    if not isinstance(content, str):
        content = " ".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        )
    code = extract_code(content)
    if not looks_like_test_file(code):
        info["error"] = "response did not contain a test file"
        return None, info
    return code, info
