"""Find the focal units: which files are production code, and what's in them.

Path heuristics alone misclassify real projects (re2 keeps its own tests in
`re2/testing/`, its helper library in `util/`, and stale object files in
`build_eval/`), so the build files are treated as the source of truth: whatever
CMake compiles into a non-test library is production code. Heuristics only fill
in when a project ships no parseable build file.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from src.tools.analysis import (
    LANGUAGES,
    _node_text,
    _parse,
    _qualified_name,
    _signature,
    _walk,
)

CPP_SOURCE_SUFFIXES = {".cc", ".cpp", ".cxx", ".c++"}

# Directory names that never hold production code under test.
EXCLUDED_DIR_PARTS = {
    "test", "tests", "testing", "unittest", "unittests", "fuzzing", "fuzz",
    "benchmark", "benchmarks", "benchlog", "third_party", "thirdparty",
    "vendor", "external", "extern", "doc", "docs", "examples", "sample",
    "samples", "perf", "performance", "obj", "cmake",
}
EXCLUDED_NAME_RE = re.compile(
    r"(^|[._-])(test|tests|benchmark|bench|fuzz|main)([._-]|$)", re.IGNORECASE
)
BUILD_DIR_RE = re.compile(r"^(build|_build|out|cmake-build)", re.IGNORECASE)

# CMake target names that indicate a test/benchmark artifact rather than the
# library under test. Deliberately narrow: "sample" and "demo" are excluded
# from this list because they are plausible names for the real library of a
# small project, and dropping its target would leave nothing to benchmark.
TEST_TARGET_RE = re.compile(r"test|bench|fuzz|mock", re.IGNORECASE)

# set() keywords that are modifiers, not values.
CMAKE_SET_MODIFIERS = {
    "CACHE", "STRING", "BOOL", "FILEPATH", "PATH", "INTERNAL", "FORCE",
    "PARENT_SCOPE",
}
CMAKE_LIBRARY_KEYWORDS = {
    "STATIC", "SHARED", "MODULE", "OBJECT", "INTERFACE", "IMPORTED", "ALIAS",
    "EXCLUDE_FROM_ALL",
}

# Boolean operators add a decision point in the standard cyclomatic count but
# are not statements, so tree-sitter reports them as binary expressions.
CPP_SHORT_CIRCUIT = {"&&", "||", "and", "or"}


@dataclass(frozen=True)
class FocalMethod:
    """One function/method the benchmark will ask the agent to test."""

    file: str           # relative to project root, POSIX separators
    name: str           # qualified where the grammar allows (Class::method)
    signature: str
    start_line: int     # 1-based, inclusive
    end_line: int
    complexity: int     # cyclomatic complexity
    branch_count: int

    @property
    def key(self) -> str:
        return f"{self.file}::{self.name}@{self.start_line}"


def _strip_cmake_comments(text: str) -> str:
    return re.sub(r"#[^\n]*", "", text)


def _collect_cmake_variables(text: str) -> dict[str, list[str]]:
    """Map `set(VAR a b c)` names to their token lists, ignoring cache/flags."""
    variables: dict[str, list[str]] = {}
    for match in re.finditer(r"\bset\s*\(([^)]*)\)", text, re.IGNORECASE | re.DOTALL):
        tokens = match.group(1).split()
        if len(tokens) < 2:
            continue
        name, values = tokens[0], tokens[1:]
        variables[name] = [v for v in values if v.upper() not in CMAKE_SET_MODIFIERS]
    return variables


def _expand(tokens: list[str], variables: dict[str, list[str]], depth: int = 0) -> list[str]:
    """Substitute ${VAR} references, one level of nesting at a time."""
    if depth > 4:
        return tokens
    out: list[str] = []
    changed = False
    for token in tokens:
        match = re.fullmatch(r"\$\{(\w+)\}", token)
        if match and match.group(1) in variables:
            out.extend(variables[match.group(1)])
            changed = True
        else:
            out.append(token)
    return _expand(out, variables, depth + 1) if changed else out


def _is_excluded_path(path: Path, project_root: Path) -> bool:
    """True for directories that hold tests, build output, or vendored code."""
    try:
        parts = path.resolve().relative_to(project_root.resolve()).parts
    except ValueError:
        return True
    for part in parts:
        lowered = part.lower()
        if lowered in EXCLUDED_DIR_PARTS or BUILD_DIR_RE.match(lowered):
            return True
        if part.startswith("."):
            return True
    return False


def sources_from_cmake(project_root: Path) -> list[Path]:
    """Sources compiled into non-test libraries, per the project's CMake files.

    Returns absolute paths that exist on disk; an empty list means CMake told
    us nothing usable and the caller should fall back to heuristics.
    """
    found: list[Path] = []
    for cmake_file in sorted(project_root.rglob("CMakeLists.txt")):
        if _is_excluded_path(cmake_file.parent, project_root):
            continue
        text = _strip_cmake_comments(cmake_file.read_text(errors="replace"))
        variables = _collect_cmake_variables(text)
        for match in re.finditer(
            r"\badd_library\s*\(([^)]*)\)", text, re.IGNORECASE | re.DOTALL
        ):
            tokens = _expand(match.group(1).split(), variables)
            if not tokens:
                continue
            target, rest = tokens[0], tokens[1:]
            if TEST_TARGET_RE.search(target):
                continue
            for token in rest:
                if token.upper() in CMAKE_LIBRARY_KEYWORDS:
                    continue
                if Path(token).suffix not in CPP_SOURCE_SUFFIXES:
                    continue
                candidate = (cmake_file.parent / token).resolve()
                if candidate.is_file():
                    found.append(candidate)
    # Preserve first-seen order while dropping duplicates.
    return list(dict.fromkeys(found))


def sources_by_heuristic(project_root: Path) -> list[Path]:
    """Fallback discovery: every C++ source outside test/build/vendor trees."""
    out = []
    for path in sorted(project_root.rglob("*")):
        if path.suffix not in CPP_SOURCE_SUFFIXES or not path.is_file():
            continue
        if _is_excluded_path(path.parent, project_root):
            continue
        if EXCLUDED_NAME_RE.search(path.stem):
            continue
        out.append(path.resolve())
    return out


def production_sources(project_root: Path) -> tuple[list[Path], str]:
    """Focal source files plus the strategy that produced them."""
    from_cmake = sources_from_cmake(project_root)
    if from_cmake:
        # CMake can still list a generated or vendored file; apply the path
        # filter on top so both strategies agree on what counts as production.
        kept = [p for p in from_cmake if not _is_excluded_path(p.parent, project_root)]
        if kept:
            return kept, "cmake"
    return sources_by_heuristic(project_root), "heuristic"


def _cyclomatic_complexity(node: Node, lang: str) -> tuple[int, int]:
    """(complexity, branch_count) for one function body.

    Complexity counts decision points + 1; `case` labels and short-circuit
    operators each add one, matching how lizard and most CC tools score C++.
    """
    branch_types = LANGUAGES[lang]["branch_types"]
    branches = 0
    short_circuit = 0
    for child in _walk(node):
        if child.type in branch_types:
            branches += 1
        elif child.type == "binary_expression":
            operator = child.child_by_field_name("operator")
            if operator is not None and operator.type in CPP_SHORT_CIRCUIT:
                short_circuit += 1
    return branches + short_circuit + 1, branches


def _has_body(node: Node) -> bool:
    return node.child_by_field_name("body") is not None


def focal_methods(project_root: Path, source: Path, *, min_lines: int = 1) -> list[FocalMethod]:
    """Every function *defined* in `source`, with its complexity."""
    lang = "cpp"
    tree = _parse(source.read_bytes(), lang)
    relative = source.resolve().relative_to(project_root.resolve()).as_posix()

    methods = []
    for node in _walk(tree.root_node):
        if node.type not in LANGUAGES[lang]["function_types"] or not _has_body(node):
            continue
        name = _qualified_name(node, lang)
        if name == "<unknown>":
            continue
        start, end = node.start_point[0] + 1, node.end_point[0] + 1
        if end - start + 1 < min_lines:
            continue
        complexity, branches = _cyclomatic_complexity(node, lang)
        methods.append(
            FocalMethod(
                file=relative,
                name=name,
                signature=_signature(node, lang),
                start_line=start,
                end_line=end,
                complexity=complexity,
                branch_count=branches,
            )
        )
    return methods


def discover(project_root: Path, *, min_lines: int = 1) -> tuple[list[FocalMethod], str]:
    """All focal methods in a project, in file order."""
    project_root = project_root.resolve()
    sources, strategy = production_sources(project_root)
    methods: list[FocalMethod] = []
    for source in sources:
        methods.extend(focal_methods(project_root, source, min_lines=min_lines))
    return methods, strategy


# ---------------------------------------------------------------------------
# Focal-set narrowing, for comparison with published benchmarks
#
# CITYWALK defines a focal method as one belonging to a focal class file
# (m_focal in M(sc_focal), §2.1-2.2.1), and its pipeline is built around the
# other method signatures "within the focal class". Free functions have no
# focal class, and a private helper is not reachable from a test, so neither
# enters its focal set. Reproducing that selection needs the header, not just
# the source file.


def _class_name_of(node: Node) -> str | None:
    name = node.child_by_field_name("name")
    return _node_text(name) if name is not None else None


def _declared_method_names(class_node: Node, default_public: bool) -> set[str]:
    """Public method names declared in one class body.

    Access defaults to private for `class` and public for `struct`, and each
    `public:` / `private:` / `protected:` label switches it for the members
    that follow — so the body is walked in order rather than searched.
    """
    body = class_node.child_by_field_name("body")
    if body is None:
        return set()
    public = default_public
    names: set[str] = set()
    for child in body.children:
        if child.type == "access_specifier":
            public = _node_text(child).strip().rstrip(":").strip() == "public"
            continue
        if not public or child.type not in {"field_declaration", "declaration",
                                            "function_definition"}:
            continue
        for node in _walk(child):
            if node.type != "function_declarator":
                continue
            declarator = node.child_by_field_name("declarator")
            if declarator is None:
                continue
            name = _node_text(declarator).strip()
            # Operators and destructors keep their punctuation; identifiers do
            # not, and only identifiers are matched against a definition.
            if name and (name[0].isalpha() or name[0] == "_"):
                names.add(name.split("::")[-1])
    return names


def public_method_index(project_root: Path) -> set[str]:
    """`Class::method` for every public method declared in a project header."""
    project_root = project_root.resolve()
    index: set[str] = set()
    for header in sorted(project_root.rglob("*.h")):
        if _is_excluded_path(header.parent, project_root):
            continue
        tree = _parse(header.read_bytes(), "cpp")
        for node in _walk(tree.root_node):
            if node.type not in {"class_specifier", "struct_specifier"}:
                continue
            class_name = _class_name_of(node)
            if not class_name:
                continue
            default_public = node.type == "struct_specifier"
            for method in _declared_method_names(node, default_public):
                index.add(f"{class_name}::{method}")
    return index


def class_access_index(project_root: Path) -> dict[str, str]:
    """`Class::method` -> "public" | "private" | "protected" for the project.

    Both headers and sources are scanned: a class declared inside a .cc file
    (re2 has several) still governs the access of the methods defined below it.
    """
    project_root = project_root.resolve()
    access: dict[str, str] = {}
    patterns = ["*.h", "*.hpp", *(f"*{s}" for s in CPP_SOURCE_SUFFIXES)]
    seen: set[Path] = set()
    for pattern in patterns:
        for path in sorted(project_root.rglob(pattern)):
            if path in seen or _is_excluded_path(path.parent, project_root):
                continue
            seen.add(path)
            tree = _parse(path.read_bytes(), "cpp")
            for node in _walk(tree.root_node):
                if node.type not in {"class_specifier", "struct_specifier"}:
                    continue
                class_name = _class_name_of(node)
                if not class_name:
                    continue
                default = "public" if node.type == "struct_specifier" else "private"
                for method, level in _member_access(node, default).items():
                    # First declaration wins; a later redeclaration of the same
                    # name in another class must not overwrite it.
                    access.setdefault(f"{class_name}::{method}", level)
    return access


def _member_access(class_node: Node, default: str) -> dict[str, str]:
    """Access level of each method name declared in one class body."""
    body = class_node.child_by_field_name("body")
    if body is None:
        return {}
    current = default
    levels: dict[str, str] = {}
    for child in body.children:
        if child.type == "access_specifier":
            current = _node_text(child).strip().rstrip(":").strip() or current
            continue
        if child.type not in {"field_declaration", "declaration", "function_definition"}:
            continue
        for node in _walk(child):
            if node.type != "function_declarator":
                continue
            declarator = node.child_by_field_name("declarator")
            if declarator is None:
                continue
            name = _node_text(declarator).strip().split("::")[-1]
            if name and (name[0].isalpha() or name[0] == "_"):
                levels.setdefault(name, current)
    return levels


# Per-project file exclusions taken verbatim from CITYWALK's evaluator.py:
#   if project_name == "re2" and (file.startswith("unicode")
#                                 or file.startswith("perl_groups")): continue
# Generated tables of Unicode data have no behaviour worth testing, and the
# authors skip them by name rather than by any general rule.
CITYWALK_FILE_EXCLUSIONS = {"re2": ("unicode", "perl_groups")}


def citywalk_excluded_file(project_name: str, file_path: str) -> bool:
    """Whether CITYWALK's evaluator would skip this source file outright."""
    prefixes = CITYWALK_FILE_EXCLUSIONS.get(project_name.split("-")[0], ())
    stem = Path(file_path).name
    return any(stem.startswith(prefix) for prefix in prefixes)


def is_citywalk_focal(name: str, access: dict[str, str]) -> bool:
    """Whether CITYWALK's parser would keep this definition as a focal method.

    Their rule (pre_parser.py) accepts FUNCTION_DECL, CXX_METHOD and
    FUNCTION_TEMPLATE and skips only PRIVATE and PROTECTED members. A free
    function has no access specifier at all, so it is kept — as is a method
    whose class could not be resolved.
    """
    if "::" not in name:
        return True
    class_name, method = name.rsplit("::", 1)
    class_name = class_name.split("::")[-1]
    return access.get(f"{class_name}::{method}", "public") not in {"private", "protected"}


def is_public_class_method(name: str, index: set[str]) -> bool:
    """Whether a definition's qualified name is a public method of a class.

    A free function has no `::` and is excluded outright. A qualified name is
    kept only when its own class declares it publicly, so a private helper of
    an internal class does not slip through on a name shared with some other
    class.
    """
    if "::" not in name:
        return False
    class_name, method = name.rsplit("::", 1)
    class_name = class_name.split("::")[-1]  # drop namespace qualifiers
    return f"{class_name}::{method}" in index
