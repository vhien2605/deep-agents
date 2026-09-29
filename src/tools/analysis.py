"""Multi-language source analysis tools built on tree-sitter (C++, Python, Java)."""

import json
from pathlib import Path

import tree_sitter_cpp
import tree_sitter_java
import tree_sitter_python
from langchain_core.tools import tool
from tree_sitter import Language, Node, Parser

CPP_LANGUAGE = Language(tree_sitter_cpp.language())
PY_LANGUAGE = Language(tree_sitter_python.language())
JAVA_LANGUAGE = Language(tree_sitter_java.language())

# Per-language grammar description: node types for functions/classes/imports/
# branches, plus how to find a function's name and body.
LANGUAGES = {
    "cpp": {
        "language": CPP_LANGUAGE,
        "extensions": {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"},
        "function_types": {"function_definition"},
        "class_types": {"class_specifier", "struct_specifier"},
        "import_types": {"preproc_include"},
        "branch_types": {
            "if_statement", "for_statement", "while_statement", "do_statement",
            "switch_statement", "conditional_expression", "catch_clause",
            "case_statement",
        },
    },
    "python": {
        "language": PY_LANGUAGE,
        "extensions": {".py"},
        "function_types": {"function_definition"},
        "class_types": {"class_definition"},
        "import_types": {"import_statement", "import_from_statement"},
        "branch_types": {
            "if_statement", "for_statement", "while_statement",
            "conditional_expression", "except_clause", "case_clause",
            "boolean_operator", "with_statement",
        },
    },
    "java": {
        "language": JAVA_LANGUAGE,
        "extensions": {".java"},
        "function_types": {"method_declaration", "constructor_declaration"},
        "class_types": {
            "class_declaration", "interface_declaration", "enum_declaration",
            "record_declaration",
        },
        "import_types": {"import_declaration"},
        "branch_types": {
            "if_statement", "for_statement", "enhanced_for_statement",
            "while_statement", "do_statement", "switch_expression",
            "switch_block_statement_group", "ternary_expression", "catch_clause",
        },
    },
}

SOURCE_EXTENSIONS = {
    ext: lang for lang, spec in LANGUAGES.items() for ext in spec["extensions"]
}


def _lang_for(path: Path) -> str | None:
    return SOURCE_EXTENSIONS.get(path.suffix.lower())


def _parse(source: bytes, lang: str):
    return Parser(LANGUAGES[lang]["language"]).parse(source)


def _node_text(node: Node) -> str:
    return node.text.decode("utf8", errors="replace")


def _function_name(fn_node: Node, lang: str) -> str:
    if lang in ("python", "java"):
        name = fn_node.child_by_field_name("name")
        return _node_text(name) if name is not None else "<unknown>"
    # C++: descend through declarators to the function identifier.
    decl = fn_node.child_by_field_name("declarator")
    while decl is not None:
        if decl.type == "function_declarator":
            inner = decl.child_by_field_name("declarator")
            return _node_text(inner) if inner is not None else _node_text(decl)
        decl = decl.child_by_field_name("declarator")
    return "<unknown>"


def _qualified_name(fn_node: Node, lang: str) -> str:
    """Prefix Python/Java methods with their enclosing class (Class.method)."""
    name = _function_name(fn_node, lang)
    if lang in ("python", "java"):
        class_types = LANGUAGES[lang]["class_types"]
        parent = fn_node.parent
        while parent is not None:
            if parent.type in class_types:
                cls = parent.child_by_field_name("name")
                if cls is not None:
                    return f"{_node_text(cls)}.{name}"
            parent = parent.parent
    return name


def _signature(fn_node: Node, lang: str) -> str:
    """Signature = definition text before the body."""
    body = fn_node.child_by_field_name("body")
    end = body.start_byte if body is not None else fn_node.end_byte
    sig = fn_node.text[: end - fn_node.start_byte].decode("utf8", errors="replace").strip()
    return sig.rstrip(":").strip() if lang == "python" else sig


def _walk(node: Node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _resolve(project_root: Path, file_path: str) -> Path:
    p = Path(file_path)
    return p if p.is_absolute() else project_root / p


def make_analysis_tools(project_root: Path) -> list:
    """Build tree-sitter analysis tools scoped to `project_root`."""

    @tool
    def analyze_source_file(file_path: str) -> str:
        """Parse a source file (C/C++, Python, or Java) with tree-sitter and return its
        structure as JSON: language, imports/includes, classes, and function
        definitions with name, signature, line range, and a rough branch count
        (if/for/while/switch/except/ternary...). Python methods are reported as
        Class.method. Use this to decide which functions need tests. Paths are
        relative to the target project root.
        """
        path = _resolve(project_root, file_path)
        if not path.is_file():
            return f"ERROR: file not found: {path}"
        lang = _lang_for(path)
        if lang is None:
            return (
                f"ERROR: unsupported file type: {path.suffix} "
                f"(supported: {sorted(SOURCE_EXTENSIONS)})"
            )
        spec = LANGUAGES[lang]
        source = path.read_bytes()
        tree = _parse(source, lang)

        imports, classes, functions = [], [], []
        for node in _walk(tree.root_node):
            if node.type in spec["import_types"]:
                if lang == "cpp":
                    arg = node.child_by_field_name("path")
                    if arg is not None:
                        imports.append(_node_text(arg).strip('"<>'))
                else:
                    imports.append(_node_text(node))
            elif node.type in spec["class_types"]:
                name = node.child_by_field_name("name")
                classes.append({
                    "kind": node.type.replace("_specifier", "").replace("_definition", ""),
                    "name": _node_text(name) if name is not None else "<anonymous>",
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                })
            elif node.type in spec["function_types"]:
                branches = sum(1 for n in _walk(node) if n.type in spec["branch_types"])
                functions.append({
                    "name": _qualified_name(node, lang),
                    "signature": _signature(node, lang),
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "branch_count": branches,
                })

        return json.dumps(
            {
                "file": str(path),
                "language": lang,
                "imports": imports,
                "classes": classes,
                "functions": functions,
                "has_parse_errors": tree.root_node.has_error,
            },
            indent=2,
            ensure_ascii=False,
        )

    @tool
    def get_function_source(file_path: str, function_name: str) -> str:
        """Return the full source of one function definition in a C++, Python, or Java
        file. `function_name` matches the identifier reported by
        analyze_source_file (substring match on qualified names is allowed,
        e.g. "Calculator::add", "MyClass.method" (Python/Java), or just "add").
        """
        path = _resolve(project_root, file_path)
        if not path.is_file():
            return f"ERROR: file not found: {path}"
        lang = _lang_for(path)
        if lang is None:
            return f"ERROR: unsupported file type: {path.suffix}"
        tree = _parse(path.read_bytes(), lang)
        matches = []
        for node in _walk(tree.root_node):
            if node.type in LANGUAGES[lang]["function_types"]:
                name = _qualified_name(node, lang)
                if function_name == name or function_name in name:
                    matches.append(
                        f"# {path}:{node.start_point[0] + 1}-{node.end_point[0] + 1} ({name})\n"
                        + _node_text(node)
                    )
        if not matches:
            return f"ERROR: no function matching '{function_name}' in {path}"
        return "\n\n".join(matches)

    @tool
    def list_source_files(subdir: str = ".", language: str = "") -> str:
        """List source files under a subdirectory of the project root, excluding
        common build/vendor directories. `language` filters by "cpp", "python", or "java";
        empty lists all. Maven `target` and build dirs are excluded. Each output line is `<relative_path>\t<language>`.
        """
        base = _resolve(project_root, subdir)
        if not base.is_dir():
            return f"ERROR: directory not found: {base}"
        skip = {
            "build", ".git", "CMakeFiles", "third_party", "external",
            "node_modules", ".venv", "venv", "__pycache__", ".tox", "dist",
            "target", ".gradle", ".mvn",
        }
        out = []
        for p in sorted(base.rglob("*")):
            lang = _lang_for(p)
            if lang is None or (language and lang != language):
                continue
            if set(p.relative_to(base).parts[:-1]) & skip:
                continue
            out.append(f"{p.relative_to(project_root)}\t{lang}")
        return "\n".join(out) if out else "(no source files found)"

    return [analyze_source_file, get_function_source, list_source_files]
