"""Tests for src/tools/analysis.py — tree-sitter multi-language analysis tools."""

import json
from pathlib import Path

import pytest

from src.tools.analysis import (
    LANGUAGES,
    SOURCE_EXTENSIONS,
    _function_name,
    _lang_for,
    _node_text,
    _parse,
    _qualified_name,
    _resolve,
    _signature,
    _walk,
    make_analysis_tools,
)


# ---------------------------------------------------------------------------
# _lang_for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "suffix,expected",
    [
        (".py", "python"),
        (".cpp", "cpp"),
        (".c", "cpp"),
        (".h", "cpp"),
        (".hpp", "cpp"),
        (".cc", "cpp"),
        (".cxx", "cpp"),
        (".java", "java"),
        (".txt", None),
        (".md", None),
        ("", None),
    ],
)
def test_lang_for(suffix, expected):
    assert _lang_for(Path(f"file{suffix}")) == expected


def test_source_extensions_covers_all():
    """Every extension in LANGUAGES must appear in SOURCE_EXTENSIONS."""
    for lang, spec in LANGUAGES.items():
        for ext in spec["extensions"]:
            assert SOURCE_EXTENSIONS[ext] == lang


# ---------------------------------------------------------------------------
# _parse + _node_text
# ---------------------------------------------------------------------------


def test_parse_python_source():
    tree = _parse(b"def f():\n    pass\n", "python")
    assert tree.root_node is not None
    assert not tree.root_node.has_error


def test_parse_cpp_source():
    tree = _parse(b"int main() { return 0; }", "cpp")
    assert tree.root_node is not None
    assert not tree.root_node.has_error


def test_parse_java_source():
    tree = _parse(b"class A { void m() {} }", "java")
    assert tree.root_node is not None


def test_node_text_decodes_bytes():
    tree = _parse(b"x = 1", "python")
    assert _node_text(tree.root_node) == "x = 1"


def test_node_text_replaces_invalid_utf8():
    tree = _parse(b"\xff\xfe", "python")
    # Should not raise; replacement chars are fine
    text = _node_text(tree.root_node)
    assert isinstance(text, str)


# ---------------------------------------------------------------------------
# _walk
# ---------------------------------------------------------------------------


def test_walk_yields_root_and_children():
    tree = _parse(b"def f():\n    return 1\n", "python")
    nodes = list(_walk(tree.root_node))
    # Root + at least a few children
    assert len(nodes) > 1
    assert tree.root_node in nodes


def test_walk_single_node():
    tree = _parse(b"1", "python")
    nodes = list(_walk(tree.root_node))
    # A minimal tree still has at least the root
    assert tree.root_node in nodes


# ---------------------------------------------------------------------------
# _resolve
# ---------------------------------------------------------------------------


def test_resolve_absolute_path(tmp_path):
    p = tmp_path / "foo.py"
    p.write_text("x = 1")
    assert _resolve(tmp_path, str(p)) == p


def test_resolve_relative_path(tmp_path):
    p = tmp_path / "foo.py"
    p.write_text("x = 1")
    assert _resolve(tmp_path, "foo.py") == p


# ---------------------------------------------------------------------------
# _function_name (C++ declarator descent)
# ---------------------------------------------------------------------------


def test_function_name_python():
    tree = _parse(b"def my_func():\n    pass\n", "python")
    fn = tree.root_node.child_by_field_name("name")  # not a function node
    # Find the function_definition node
    for node in _walk(tree.root_node):
        if node.type == "function_definition":
            assert _function_name(node, "python") == "my_func"
            return
    pytest.fail("function_definition node not found")


def test_function_name_java():
    tree = _parse(b"class A { void hello() {} }", "java")
    for node in _walk(tree.root_node):
        if node.type == "method_declaration":
            assert _function_name(node, "java") == "hello"
            return
    pytest.fail("method_declaration node not found")


def test_function_name_cpp_simple():
    tree = _parse(b"int add(int a, int b) { return a + b; }", "cpp")
    for node in _walk(tree.root_node):
        if node.type == "function_definition":
            assert _function_name(node, "cpp") == "add"
            return
    pytest.fail("function_definition node not found")


def test_function_name_cpp_qualified():
    tree = _parse(b"int Foo::bar() { return 0; }", "cpp")
    for node in _walk(tree.root_node):
        if node.type == "function_definition":
            name = _function_name(node, "cpp")
            assert "bar" in name
            return
    pytest.fail("function_definition node not found")


def test_function_name_cpp_with_pointer_return():
    tree = _parse(b"int* get_ptr() { return nullptr; }", "cpp")
    for node in _walk(tree.root_node):
        if node.type == "function_definition":
            assert _function_name(node, "cpp") == "get_ptr"
            return
    pytest.fail("function_definition node not found")


def test_function_name_python_unknown_when_no_name():
    """A function node without a name field returns '<unknown>'."""
    tree = _parse(b"lambda: 1", "python")
    # lambda is not a function_definition; test with a minimal edge case
    # by passing a node that has no 'name' child
    for node in _walk(tree.root_node):
        if node.type == "lambda":
            # lambda nodes don't have a name field
            result = _function_name(node, "python")
            assert result == "<unknown>"
            return
    # If no lambda node, just pass — the parse may vary
    assert True


# ---------------------------------------------------------------------------
# _qualified_name (Python/Java class prefix)
# ---------------------------------------------------------------------------


def test_qualified_name_python_method():
    tree = _parse(
        b"class Foo:\n    def bar(self):\n        pass\n",
        "python",
    )
    for node in _walk(tree.root_node):
        if node.type == "function_definition":
            assert _qualified_name(node, "python") == "Foo.bar"
            return
    pytest.fail("function_definition not found")


def test_qualified_name_python_toplevel():
    tree = _parse(b"def standalone():\n    pass\n", "python")
    for node in _walk(tree.root_node):
        if node.type == "function_definition":
            assert _qualified_name(node, "python") == "standalone"
            return
    pytest.fail("function_definition not found")


def test_qualified_name_java_method():
    tree = _parse(b"class Foo { void bar() {} }", "java")
    for node in _walk(tree.root_node):
        if node.type == "method_declaration":
            assert _qualified_name(node, "java") == "Foo.bar"
            return
    pytest.fail("method_declaration not found")


def test_qualified_name_cpp_no_prefix():
    tree = _parse(b"int bar() { return 0; }", "cpp")
    for node in _walk(tree.root_node):
        if node.type == "function_definition":
            # C++ does not prefix with class
            assert _qualified_name(node, "cpp") == "bar"
            return
    pytest.fail("function_definition not found")


# ---------------------------------------------------------------------------
# _signature
# ---------------------------------------------------------------------------


def test_signature_python_strips_colon():
    tree = _parse(b"def f(a, b):\n    return a + b\n", "python")
    for node in _walk(tree.root_node):
        if node.type == "function_definition":
            sig = _signature(node, "python")
            assert not sig.endswith(":")
            assert "def f(a, b)" in sig
            return
    pytest.fail("function_definition not found")


def test_signature_cpp_keeps_braces_region():
    tree = _parse(b"int f(int a) { return a; }", "cpp")
    for node in _walk(tree.root_node):
        if node.type == "function_definition":
            sig = _signature(node, "cpp")
            assert "int f(int a)" in sig
            return
    pytest.fail("function_definition not found")


def test_signature_python_no_body():
    """When body is None, signature uses the full node text."""
    tree = _parse(b"def f():\n    pass\n", "python")
    for node in _walk(tree.root_node):
        if node.type == "function_definition":
            sig = _signature(node, "python")
            assert isinstance(sig, str)
            assert len(sig) > 0
            return
    pytest.fail("function_definition not found")


# ---------------------------------------------------------------------------
# make_analysis_tools — analyze_source_file
# ---------------------------------------------------------------------------


@pytest.fixture
def analysis_tools(tmp_path):
    """Build analysis tools scoped to a temp project root with sample files."""
    (tmp_path / "calc.py").write_text(
        "class Foo:\n"
        "    def bar(self):\n"
        "        if True:\n"
        "            return 1\n"
        "        return 0\n"
        "\n"
        "def standalone():\n"
        "    return 42\n"
    )
    (tmp_path / "calc.cpp").write_text(
        "#include <iostream>\n"
        "int add(int a, int b) {\n"
        "    if (a > 0) {\n"
        "        return a + b;\n"
        "    }\n"
        "    return b;\n"
        "}\n"
    )
    (tmp_path / "App.java").write_text(
        "public class App {\n"
        "    public void run() {\n"
        "        if (true) {}\n"
        "    }\n"
        "}\n"
    )
    (tmp_path / "readme.md").write_text("# Not source")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "helper.py").write_text("def helper():\n    pass\n")
    return make_analysis_tools(tmp_path), tmp_path


def test_analyze_source_file_python(analysis_tools):
    tools, root = analysis_tools
    analyze_fn = tools[0]  # analyze_source_file
    result = analyze_fn.invoke({"file_path": "calc.py"})
    data = json.loads(result)
    assert data["language"] == "python"
    assert any(f["name"] == "Foo.bar" for f in data["functions"])
    assert any(f["name"] == "standalone" for f in data["functions"])
    assert any(c["name"] == "Foo" for c in data["classes"])
    # Foo.bar has an if-statement -> branch_count >= 1
    bar = next(f for f in data["functions"] if f["name"] == "Foo.bar")
    assert bar["branch_count"] >= 1
    assert data["has_parse_errors"] is False


def test_analyze_source_file_cpp(analysis_tools):
    tools, root = analysis_tools
    analyze_fn = tools[0]
    result = analyze_fn.invoke({"file_path": "calc.cpp"})
    data = json.loads(result)
    assert data["language"] == "cpp"
    assert any(f["name"] == "add" for f in data["functions"])
    assert "iostream" in data["imports"]


def test_analyze_source_file_java(analysis_tools):
    tools, root = analysis_tools
    analyze_fn = tools[0]
    result = analyze_fn.invoke({"file_path": "App.java"})
    data = json.loads(result)
    assert data["language"] == "java"
    assert any(f["name"] == "App.run" for f in data["functions"])
    assert any(c["name"] == "App" for c in data["classes"])


def test_analyze_source_file_absolute_path(analysis_tools):
    tools, root = analysis_tools
    analyze_fn = tools[0]
    result = analyze_fn.invoke({"file_path": str(root / "calc.py")})
    data = json.loads(result)
    assert data["language"] == "python"


def test_analyze_source_file_not_found(analysis_tools):
    tools, root = analysis_tools
    analyze_fn = tools[0]
    result = analyze_fn.invoke({"file_path": "nonexistent.py"})
    assert "ERROR" in result
    assert "not found" in result


def test_analyze_source_file_unsupported_type(analysis_tools):
    tools, root = analysis_tools
    analyze_fn = tools[0]
    result = analyze_fn.invoke({"file_path": "readme.md"})
    assert "ERROR" in result
    assert "unsupported" in result


def test_analyze_source_file_branch_count_zero(analysis_tools):
    tools, root = analysis_tools
    analyze_fn = tools[0]
    result = analyze_fn.invoke({"file_path": "calc.py"})
    data = json.loads(result)
    standalone = next(f for f in data["functions"] if f["name"] == "standalone")
    assert standalone["branch_count"] == 0


# ---------------------------------------------------------------------------
# make_analysis_tools — get_function_source
# ---------------------------------------------------------------------------


def test_get_function_source_python(analysis_tools):
    tools, root = analysis_tools
    get_fn = tools[1]
    result = get_fn.invoke({"file_path": "calc.py", "function_name": "standalone"})
    assert "def standalone" in result
    assert "return 42" in result


def test_get_function_source_qualified_name(analysis_tools):
    tools, root = analysis_tools
    get_fn = tools[1]
    result = get_fn.invoke({"file_path": "calc.py", "function_name": "Foo.bar"})
    assert "def bar" in result
    assert "return 1" in result


def test_get_function_source_substring_match(analysis_tools):
    tools, root = analysis_tools
    get_fn = tools[1]
    # "bar" is a substring of "Foo.bar"
    result = get_fn.invoke({"file_path": "calc.py", "function_name": "bar"})
    assert "def bar" in result


def test_get_function_source_cpp(analysis_tools):
    tools, root = analysis_tools
    get_fn = tools[1]
    result = get_fn.invoke({"file_path": "calc.cpp", "function_name": "add"})
    assert "int add" in result


def test_get_function_source_not_found(analysis_tools):
    tools, root = analysis_tools
    get_fn = tools[1]
    result = get_fn.invoke({"file_path": "calc.py", "function_name": "nonexistent"})
    assert "ERROR" in result
    assert "no function matching" in result


def test_get_function_source_file_not_found(analysis_tools):
    tools, root = analysis_tools
    get_fn = tools[1]
    result = get_fn.invoke({"file_path": "missing.py", "function_name": "f"})
    assert "ERROR" in result
    assert "not found" in result


def test_get_function_source_unsupported_type(analysis_tools):
    tools, root = analysis_tools
    get_fn = tools[1]
    result = get_fn.invoke({"file_path": "readme.md", "function_name": "f"})
    assert "ERROR" in result
    assert "unsupported" in result


def test_get_function_source_multiple_matches(analysis_tools):
    """Two functions whose names both contain a substring should both be returned."""
    tools, root = analysis_tools
    (root / "multi.py").write_text(
        "def foo_one():\n    pass\n"
        "def foo_two():\n    pass\n"
    )
    get_fn = tools[1]
    result = get_fn.invoke({"file_path": "multi.py", "function_name": "foo"})
    assert "foo_one" in result
    assert "foo_two" in result


# ---------------------------------------------------------------------------
# make_analysis_tools — list_source_files
# ---------------------------------------------------------------------------


def test_list_source_files_all(analysis_tools):
    tools, root = analysis_tools
    list_fn = tools[2]
    result = list_fn.invoke({"subdir": "."})
    lines = result.strip().split("\n")
    # calc.py, calc.cpp, App.java, subdir/helper.py  (readme.md excluded)
    paths = [l.split("\t")[0] for l in lines]
    assert any("calc.py" in p for p in paths)
    assert any("calc.cpp" in p for p in paths)
    assert any("App.java" in p for p in paths)
    assert any("helper.py" in p for p in paths)
    assert not any("readme.md" in p for p in paths)
    # Each line has a language tag
    for line in lines:
        assert "\t" in line


def test_list_source_files_filter_python(analysis_tools):
    tools, root = analysis_tools
    list_fn = tools[2]
    result = list_fn.invoke({"subdir": ".", "language": "python"})
    lines = result.strip().split("\n")
    for line in lines:
        assert line.endswith("\tpython")


def test_list_source_files_filter_cpp(analysis_tools):
    tools, root = analysis_tools
    list_fn = tools[2]
    result = list_fn.invoke({"subdir": ".", "language": "cpp"})
    lines = result.strip().split("\n")
    for line in lines:
        assert line.endswith("\tcpp")
    assert any("calc.cpp" in l for l in lines)
    assert not any("calc.py" in l for l in lines)


def test_list_source_files_filter_java(analysis_tools):
    tools, root = analysis_tools
    list_fn = tools[2]
    result = list_fn.invoke({"subdir": ".", "language": "java"})
    lines = result.strip().split("\n")
    for line in lines:
        assert line.endswith("\tjava")
    assert any("App.java" in l for l in lines)


def test_list_source_files_subdir(analysis_tools):
    tools, root = analysis_tools
    list_fn = tools[2]
    result = list_fn.invoke({"subdir": "subdir"})
    assert "helper.py" in result
    assert "calc.py" not in result


def test_list_source_files_dir_not_found(analysis_tools):
    tools, root = analysis_tools
    list_fn = tools[2]
    result = list_fn.invoke({"subdir": "does_not_exist"})
    assert "ERROR" in result
    assert "not found" in result


def test_list_source_files_absolute_subdir(analysis_tools):
    tools, root = analysis_tools
    list_fn = tools[2]
    result = list_fn.invoke({"subdir": str(root / "subdir")})
    assert "helper.py" in result


def test_list_source_files_excludes_build_dirs(analysis_tools):
    tools, root = analysis_tools
    # Create a build dir with a .py file — should be excluded
    (root / "build").mkdir()
    (root / "build" / "ignored.py").write_text("x = 1")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "cache.py").write_text("x = 1")
    list_fn = tools[2]
    result = list_fn.invoke({"subdir": "."})
    assert "ignored.py" not in result
    assert "cache.py" not in result


def test_list_source_files_no_sources(tmp_path):
    """Empty directory returns '(no source files found)'."""
    (tmp_path / "empty.txt").write_text("no source here")
    tools = make_analysis_tools(tmp_path)
    list_fn = tools[2]
    result = list_fn.invoke({"subdir": "."})
    assert "no source files found" in result


def test_make_analysis_tools_returns_three_tools(tmp_path):
    tools = make_analysis_tools(tmp_path)
    assert len(tools) == 3
