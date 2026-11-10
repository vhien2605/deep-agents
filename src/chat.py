"""Conversational front end: a chat agent that finds projects on disk and
hands them to the unit-test deep agent.

The unit-test agent is bound to one project root at construction time, so it
cannot serve a chat where the user names the project in plain language
("viết test cho project calculator"). This agent sits in front of it: it
locates the project with `find_projects`, confirms the choice in conversation,
then runs the full analyze -> generate -> fix -> measure loop through
`generate_tests`. Progress of that inner run is forwarded as custom stream
events so a UI can show it live.
"""

import os
import re
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessageChunk
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer

from src.agent import create_cpp_unittest_agent
from src.config import DEFAULT_COVERAGE_TARGET, DEFAULT_RECURSION_LIMIT, SKILLS_DIR
from src.llm import build_chat_model

# Marker file -> language, checked in this order (a Java project may also ship
# a Makefile, so the more specific build files win).
PROJECT_MARKERS = (
    ("pom.xml", "java"),
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("Makefile", "cpp"),
)
SKIP_DIRS = {
    "node_modules", "__pycache__", "venv", "build", "dist", "target",
    "site-packages", "runs", "data",
}
MAX_SEARCH_DEPTH = 4
MAX_RESULTS = 30


def expand_skill_command(message: str) -> str:
    """Turn "/<skill> rest" into a request that makes the chat agent hand that
    skill to the unit-test agent. Messages without a known skill pass through."""
    match = re.match(r"/([a-z0-9][a-z0-9-]*)(?:\s+(.*))?$", message, re.DOTALL)
    # The name pattern also keeps "/../x" from escaping SKILLS_DIR.
    if not match or not (SKILLS_DIR / match[1] / "SKILL.md").is_file():
        return message
    name, rest = match[1], match[2] or ""
    request = rest.strip() or "Generate unit tests for the project in this workspace."
    return (
        f"[The user invoked the `{name}` skill.] When you call generate_tests, start "
        f"`instructions` with: \"Follow the {name} skill — read its SKILL.md before "
        f"anything else.\"\n\nRequest: {request}"
    )


CHAT_PROMPT = """You are a unit-test assistant the user talks to in chat. You can
generate unit tests for C++ (Makefile), Python (pyproject.toml/setup.py) and
Java (pom.xml) projects on this machine.

Workspace searched for projects: {workspace}
This is the folder open in the user's editor: "this project" / "project này"
means the project at the workspace root (or the only project inside it).

How to work:
- When the user asks to test a project, call find_projects with a name hint
  taken from their message (empty hint lists everything). If they give a path,
  use it directly.
- Exactly one good match: tell the user which project and language you picked,
  then call generate_tests. Several matches: list them and ask which one.
  No match: say so and ask for the path.
- Pass any extra wishes (only some files, a test framework, a coverage target)
  to generate_tests through `instructions` / `coverage_target`.
- generate_tests runs a long build-and-test loop that writes files into the
  project. After it returns, summarise the result for the user: test files
  written, coverage reached, real bugs found.
- Answer in the user's language. Be concise.
"""


def _detect_language(path: Path) -> str | None:
    for marker, language in PROJECT_MARKERS:
        if (path / marker).is_file():
            return language
    return None


def find_project_dirs(workspace: Path, hint: str = "") -> list[dict]:
    """Walk `workspace` (bounded depth) for directories holding a build file.

    Matches whose directory name contains `hint` rank first, then matches on
    the rest of the path; with an empty hint every project is returned.
    """
    hint = hint.strip().lower()
    found: list[tuple[int, dict]] = []
    for dirpath, dirnames, _ in os.walk(workspace):
        current = Path(dirpath)
        depth = len(current.relative_to(workspace).parts)
        dirnames[:] = [] if depth >= MAX_SEARCH_DEPTH else sorted(
            d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS
        )
        language = _detect_language(current)
        if language is None:
            continue
        if not hint:
            rank = 0
        elif hint in current.name.lower():
            rank = 0
        elif hint in str(current.relative_to(workspace)).lower():
            rank = 1
        else:
            continue
        found.append((rank, {"path": str(current), "language": language}))
    found.sort(key=lambda item: (item[0], item[1]["path"]))
    return [entry for _, entry in found[:MAX_RESULTS]]


def _summarise_update(update: dict) -> list[str]:
    """Turn one inner-agent graph update into short progress lines."""
    lines = []
    for payload in update.values():
        if not isinstance(payload, dict):
            continue
        for message in payload.get("messages", []) or []:
            if getattr(message, "type", "") != "ai":
                continue
            for call in getattr(message, "tool_calls", []) or []:
                args = call.get("args", {})
                detail = args.get("subagent_type") or args.get("file_path") or args.get("command") or ""
                lines.append(f"{call['name']} {str(detail)[:120]}".rstrip())
    return lines


def _text(content) -> str:
    if isinstance(content, str):
        return content
    # No separator: in a streamed chunk, text blocks are consecutive slices of
    # one reply, and non-text blocks (tool_use) must not inject spaces.
    return "".join(block.get("text", "") for block in content if isinstance(block, dict))


def _final_text(messages: list) -> str:
    for message in reversed(messages):
        if getattr(message, "type", "") == "ai" and _text(message.content).strip():
            return _text(message.content).strip()
    return "(the agent finished without a final message)"


def make_chat_tools(workspace: Path, model: BaseChatModel, recursion_limit: int):
    @tool
    def find_projects(name_hint: str = "") -> list[dict] | str:
        """Find C++/Python/Java projects in the workspace. `name_hint` is part
        of the project's folder name or path (case-insensitive); leave it empty
        to list every project."""
        results = find_project_dirs(workspace, name_hint)
        return results or f"No project matching {name_hint!r} under {workspace}."

    @tool
    def generate_tests(
        project_path: str,
        coverage_target: float = DEFAULT_COVERAGE_TARGET,
        instructions: str = "",
    ) -> str:
        """Run the unit-test agent on one project: analyze, generate tests,
        build & fix, measure coverage, iterate until `coverage_target` (line
        coverage percent). Writes test files into the project. Long-running."""
        root = Path(project_path).expanduser()
        if not root.is_absolute():
            root = workspace / root
        root = root.resolve()
        if not root.is_dir():
            return f"ERROR: project not found: {root}"
        language = _detect_language(root) or "unknown"

        task = (
            f"Generate unit tests for this {language} project until line coverage "
            f"of the production sources reaches at least {coverage_target}%. "
            "Follow your workflow: analyze, generate, build & fix, measure, iterate."
        )
        if instructions.strip():
            task += f"\n\nAdditional instructions: {instructions.strip()}"

        write = get_stream_writer()
        write(f"Running unit-test agent on {root} ({language})")
        agent = create_cpp_unittest_agent(root, coverage_target=coverage_target, model=model)
        messages: list = []
        for mode, chunk in agent.stream(
            {"messages": [{"role": "user", "content": task}]},
            stream_mode=["updates", "values"],
            config={"recursion_limit": recursion_limit},
        ):
            if mode == "updates":
                for line in _summarise_update(chunk):
                    write(line)
            else:
                messages = chunk.get("messages", messages)
        return _final_text(messages)

    return [find_projects, generate_tests]


def create_chat_agent(
    workspace: str | Path,
    *,
    model: BaseChatModel | None = None,
    worker_model: BaseChatModel | None = None,
    recursion_limit: int = DEFAULT_RECURSION_LIMIT,
):
    """Build the chat agent. `model` talks to the user and streams tokens;
    `worker_model` drives the unit-test agent and keeps streaming off (see
    src/llm.py). Conversation memory lives in an in-process checkpointer; pass
    a distinct `thread_id` per conversation."""
    workspace = Path(workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise FileNotFoundError(f"workspace not found: {workspace}")
    if model is None:
        model = build_chat_model(streaming=True)
    if worker_model is None:
        worker_model = build_chat_model()
    return create_agent(
        model=model,
        tools=make_chat_tools(workspace, worker_model, recursion_limit),
        system_prompt=CHAT_PROMPT.format(workspace=workspace),
        checkpointer=InMemorySaver(),
    )


def stream_reply(agent, message: str, thread_id: str, recursion_limit: int = DEFAULT_RECURSION_LIMIT):
    """Run one chat turn. Yields ("text", delta) for each streamed token of the
    assistant's reply, and ("tool", label) for each tool call of the chat agent
    and each step of the unit-test agent it runs."""
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": recursion_limit}
    for mode, chunk in agent.stream(
        {"messages": [{"role": "user", "content": message}]},
        config=config,
        stream_mode=["messages", "updates", "custom"],
    ):
        if mode == "custom":
            yield "tool", str(chunk)
        elif mode == "messages":
            token, metadata = chunk
            # Only the chat model's own streamed tokens: the unit-test agent's
            # messages surface through the same callbacks but are not chunks.
            if isinstance(token, AIMessageChunk) and metadata.get("langgraph_node") == "model":
                delta = _text(token.content)
                if delta:
                    yield "text", delta
        else:
            for payload in chunk.values():
                if not isinstance(payload, dict):
                    continue
                for msg in payload.get("messages", []) or []:
                    for call in getattr(msg, "tool_calls", []) or []:
                        yield "tool", f"{call['name']}({str(call.get('args', {}))[:150]})"
