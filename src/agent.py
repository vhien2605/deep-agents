"""Assemble the deep agent: model, backend, tools, skills, and subagents."""

from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.language_models.chat_models import BaseChatModel

from src import prompts
from src.config import DEFAULT_COVERAGE_TARGET, SKILLS_DIR
from src.llm import build_chat_model
from src.tools import (
    make_analysis_tools,
    make_build_tools,
    make_coverage_tools,
    make_symbolic_tools,
)


def create_cpp_unittest_agent(
    project_root: str | Path,
    *,
    coverage_target: float = DEFAULT_COVERAGE_TARGET,
    model: BaseChatModel | None = None,
    checkpointer=None,
    with_build_tools: bool = True,
):
    """Create a deep agent that generates unit tests for the C++ project at
    `project_root`. Returns a compiled LangGraph agent; invoke it with
    {"messages": [{"role": "user", "content": <task>}]}.

    The model defaults to the provider configured in .env
    (LLM_PROVIDER / LLM_BASE_URL / LLM_API_KEY / LLM_MODEL).

    `with_build_tools=False` withholds the compiler and the test-fixer
    subagent, leaving an agent that can read and analyse the project but not
    build it. Benchmarks that must bound the repair budget use this: an agent
    holding a compiler decides for itself how many times to retry, which makes
    "one repair round" unenforceable.
    """
    project_root = Path(project_root).resolve()
    if not project_root.is_dir():
        raise FileNotFoundError(f"project root not found: {project_root}")

    if model is None:
        model = build_chat_model()

    analysis_tools = make_analysis_tools(project_root)
    build_tools = make_build_tools(project_root)
    coverage_tools = make_coverage_tools(project_root)
    symbolic_tools = make_symbolic_tools(project_root)

    # Backend: real filesystem + shell rooted at the C++ project, so the agent's
    # built-in read/write/edit/glob/grep/execute tools operate on the project.
    backend = LocalShellBackend(root_dir=project_root, virtual_mode=False)

    fmt = {"project_root": project_root, "coverage_target": coverage_target}
    skill_sources = [str(SKILLS_DIR)] if SKILLS_DIR.is_dir() else None

    subagents: list[dict] = [
        {
            "name": "code-analyzer",
            "description": (
                "Maps the C++ codebase with tree-sitter: files, classes, functions, "
                "signatures, branch counts, and which units lack tests. Read-only."
            ),
            "system_prompt": prompts.ANALYZER_PROMPT.format(**fmt),
            "tools": analysis_tools,
        },
        {
            "name": "path-solver",
            "description": (
                "Derives concrete argument values that reach a specific branch by "
                "solving its path constraints with Z3, and proves which branches are "
                "unreachable. Read-only; use before writing tests for branchy code."
            ),
            "system_prompt": prompts.SOLVER_PROMPT.format(**fmt),
            "tools": analysis_tools + symbolic_tools,
        },
        {
            "name": "test-generator",
            "description": (
                "Writes new C++ unit-test files for a given batch of functions/"
                "classes, following the project's test framework and conventions."
            ),
            "system_prompt": prompts.GENERATOR_PROMPT.format(**fmt),
            "tools": analysis_tools + symbolic_tools,
        },
        {
            "name": "coverage-auditor",
            "description": (
                "Measures line coverage with gcov/lcov and reports uncovered "
                "functions and line ranges, filtered to production sources."
            ),
            "system_prompt": prompts.AUDITOR_PROMPT.format(**fmt),
            "tools": analysis_tools + build_tools + coverage_tools + symbolic_tools,
        },
    ]

    if with_build_tools:
        subagents.insert(3, {
            "name": "test-fixer",
            "description": (
                "Builds with make, runs the tests, and repairs compile errors or "
                "failing assertions in generated tests until the suite passes."
            ),
            "system_prompt": prompts.FIXER_PROMPT.format(**fmt),
            "tools": analysis_tools + build_tools,
        })
        top_level_tools = analysis_tools + build_tools + coverage_tools + symbolic_tools
    else:
        subagents = [s for s in subagents if s["name"] != "coverage-auditor"]
        top_level_tools = analysis_tools + symbolic_tools

    return create_deep_agent(
        model=model,
        tools=top_level_tools,
        system_prompt=prompts.ORCHESTRATOR_PROMPT.format(**fmt),
        subagents=subagents,
        skills=skill_sources,
        backend=backend,
        # deepagents >= 0.7 no longer bundles the todo list; the orchestrator
        # prompt plans the ANALYZE -> SOLVE -> GENERATE -> FIX -> MEASURE loop
        # with it, so restore write_todos explicitly.
        middleware=[TodoListMiddleware()],
        checkpointer=checkpointer,
    )
