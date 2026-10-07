"""Symbolic execution over a function's AST: path conditions -> Z3 specs.

Given one function in a C++/Python/Java file this module

1. reads its parameters and maps each to a Z3 sort,
2. finds every branch (if / else-if / else / while / for / ternary),
3. computes each branch's *path condition* — the conjunction of the enclosing
   guards, with the right polarity — and translates it from source syntax into
   Z3 Python syntax,
4. emits a ready-to-solve spec per branch outcome.

Everything it cannot faithfully translate (calls, field access, subscripts,
locals that are not parameters) is reported rather than guessed, so a spec is
only marked solvable when its constraints are expressed purely in terms of the
function's inputs.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

from tree_sitter import Node

from src.config import SKILLS_DIR
from src.tools.analysis import (
    LANGUAGES,
    _lang_for,
    _node_text,
    _parse,
    _qualified_name,
    _walk,
)

# --------------------------------------------------------------------------- #
# Parameter sorts
# --------------------------------------------------------------------------- #

# Fixed-width source types must become BitVecs, not Ints: a branch guarded by
# overflow or by an unsigned comparison is only reachable under BitVec semantics.
CPP_SORTS = {
    "bool": "Bool",
    "char": "BitVec8", "signed char": "BitVec8", "unsigned char": "BitVec8",
    "int8_t": "BitVec8", "uint8_t": "BitVec8",
    "short": "BitVec16", "unsigned short": "BitVec16",
    "int16_t": "BitVec16", "uint16_t": "BitVec16",
    "int": "BitVec32", "unsigned": "BitVec32", "unsigned int": "BitVec32",
    "int32_t": "BitVec32", "uint32_t": "BitVec32",
    "long": "BitVec64", "unsigned long": "BitVec64", "long long": "BitVec64",
    "unsigned long long": "BitVec64", "int64_t": "BitVec64",
    "uint64_t": "BitVec64", "size_t": "BitVec64", "ssize_t": "BitVec64",
    "float": "Real", "double": "Real", "long double": "Real",
    "std::string": "String", "string": "String", "char*": "String",
    "const char*": "String", "std::string_view": "String",
}

JAVA_SORTS = {
    "boolean": "Bool",
    "byte": "BitVec8", "short": "BitVec16", "char": "BitVec16",
    "int": "BitVec32", "long": "BitVec64",
    "float": "Real", "double": "Real",
    "String": "String", "Integer": "BitVec32", "Long": "BitVec64",
    "Boolean": "Bool", "Double": "Real", "Float": "Real",
    "int[]": "Array(Int,BitVec32)", "long[]": "Array(Int,BitVec64)",
    "double[]": "Array(Int,Real)", "boolean[]": "Array(Int,Bool)",
    "String[]": "Array(Int,String)",
}

# Python ints are arbitrary precision, so Int (not BitVec) is the faithful sort.
PY_SORTS = {
    "int": "Int", "float": "Real", "bool": "Bool", "str": "String",
    "list[int]": "Array(Int,Int)", "list[float]": "Array(Int,Real)",
    "list[str]": "Array(Int,String)", "list[bool]": "Array(Int,Bool)",
    "List[int]": "Array(Int,Int)", "List[float]": "Array(Int,Real)",
    "List[str]": "Array(Int,String)",
    "Sequence[int]": "Array(Int,Int)", "tuple[int, ...]": "Array(Int,Int)",
}


def _normalize_cpp_type(text: str) -> str:
    """Strip qualifiers/references so `const std::string&` matches `std::string`."""
    t = re.sub(r"\b(const|constexpr|volatile|static|inline|register)\b", " ", text)
    t = t.replace("&", " ").strip()
    t = re.sub(r"\s+", " ", t)
    if t.endswith("*"):
        t = t[:-1].strip() + "*"
    return t


def _sort_for(type_text: str | None, lang: str) -> tuple[str | None, str | None]:
    """Map a source type to a Z3 sort. Returns `(sort, note_if_unmapped)`."""
    if not type_text:
        return None, "no declared type; infer the sort from how the parameter is used"
    raw = type_text.strip()
    if lang == "cpp":
        key = _normalize_cpp_type(raw)
        table = CPP_SORTS
    elif lang == "java":
        key = re.sub(r"\s+", "", raw)
        table = JAVA_SORTS
    else:
        key = re.sub(r"\s+", " ", raw).replace("| None", "").strip()
        key = key.removeprefix("Optional[").removesuffix("]") if key.startswith("Optional[") else key
        table = PY_SORTS
    sort = table.get(key)
    if sort is None:
        return None, f"type {raw!r} has no direct Z3 sort; model the property the branch tests (e.g. size, is_null) as Int/Bool"
    return sort, None


# --------------------------------------------------------------------------- #
# Parameter extraction
# --------------------------------------------------------------------------- #

_PARAM_LIST_TYPES = {
    "cpp": "parameter_list",
    "python": "parameters",
    "java": "formal_parameters",
}


def _cpp_param_name(declarator: Node | None) -> str | None:
    """Descend pointer/reference/array declarators down to the identifier."""
    node = declarator
    while node is not None:
        if node.type == "identifier":
            return _node_text(node)
        inner = node.child_by_field_name("declarator")
        if inner is None:
            for child in node.children:
                if child.type == "identifier":
                    return _node_text(child)
            return None
        node = inner
    return None


def extract_parameters(fn_node: Node, lang: str) -> list[dict[str, Any]]:
    """List the function's parameters with a suggested Z3 sort for each."""
    params: list[dict[str, Any]] = []
    plist = None
    for node in _walk(fn_node):
        if node.type == _PARAM_LIST_TYPES[lang]:
            plist = node
            break
    if plist is None:
        return params

    for child in plist.children:
        name: str | None = None
        type_text: str | None = None

        if lang == "cpp" and child.type in ("parameter_declaration", "optional_parameter_declaration"):
            type_node = child.child_by_field_name("type")
            type_text = _node_text(type_node) if type_node is not None else None
            declarator = child.child_by_field_name("declarator")
            if declarator is not None and declarator.type in ("pointer_declarator", "abstract_pointer_declarator"):
                type_text = f"{type_text}*"
            name = _cpp_param_name(declarator)
        elif lang == "java" and child.type in ("formal_parameter", "spread_parameter"):
            type_node = child.child_by_field_name("type")
            name_node = child.child_by_field_name("name")
            type_text = _node_text(type_node) if type_node is not None else None
            name = _node_text(name_node) if name_node is not None else None
        elif lang == "python":
            if child.type == "identifier":
                name = _node_text(child)
            elif child.type in ("typed_parameter", "typed_default_parameter", "default_parameter"):
                type_node = child.child_by_field_name("type")
                type_text = _node_text(type_node) if type_node is not None else None
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    name = _node_text(name_node)
                else:  # typed_parameter keeps the name as an unnamed identifier child
                    for sub in child.children:
                        if sub.type == "identifier":
                            name = _node_text(sub)
                            break

        if not name or (lang == "python" and name in ("self", "cls")):
            continue
        sort, note = _sort_for(type_text, lang)
        entry: dict[str, Any] = {"name": name, "declared_type": type_text, "sort": sort}
        if note:
            entry["note"] = note
        params.append(entry)
    return params


# --------------------------------------------------------------------------- #
# Condition translation: source expression -> Z3 Python expression
# --------------------------------------------------------------------------- #

# Operators that keep their infix spelling in z3py.
_INFIX = {"==", "!=", "<", ">", "<=", ">=", "+", "-", "*", "/", "%", "&", "|", "^", "<<", ">>"}
_LOGICAL = {"&&": "And", "||": "Or", "and": "And", "or": "Or"}

_PAREN_WRAPPERS = {"condition_clause", "parenthesized_expression"}
_PY_COMPARE_OPS = {"<", ">", "<=", ">=", "==", "!="}

_LITERAL_TYPES = {
    "number_literal", "integer", "float", "decimal_integer_literal",
    "hex_integer_literal", "decimal_floating_point_literal", "octal_integer_literal",
    "binary_integer_literal", "character_literal", "char_literal",
}
_TRUE_TYPES = {"true", "True"}
_FALSE_TYPES = {"false", "False"}


_COMPARISONS = {"==", "!=", "<", ">", "<=", ">="}
_BITWISE = {"&", "|", "^", "<<", ">>"}
# z3's BitVec `<` is signed; these are the unsigned counterparts.
_UNSIGNED_COMPARE = {"<": "ULT", ">": "UGT", "<=": "ULE", ">=": "UGE"}

# Source-language calls with an exact Z3 counterpart. Anything outside this
# whitelist is reported as untranslatable instead of being approximated.
_PURE_CALLS_PY = {"len": "Length"}
_PURE_CALLS_CPP = {"strlen": "Length", "abs": "_abs", "fabs": "_abs", "size": "Length", "length": "Length"}
_PURE_CALLS_JAVA = {"length": "Length", "isEmpty": "_is_empty", "abs": "_abs", "equals": "_equals"}


class Expr:
    """A translated subexpression plus the Z3 type information needed to combine it.

    `kind` is one of `bv`, `int`, `real`, `bool`, `string`, or `None` for a
    literal that adapts to whatever it is combined with. `width`/`signed` only
    apply to `bv` and drive C/Java integer promotion.
    """

    __slots__ = ("text", "kind", "width", "signed")

    def __init__(self, text: str, kind: str | None = None, width: int | None = None, signed: bool = True) -> None:
        self.text, self.kind, self.width, self.signed = text, kind, width, signed


class Translation:
    """Accumulates what a condition used and what could not be translated."""

    def __init__(self, sorts: dict[str, str] | None = None, unsigned: set[str] | None = None) -> None:
        self.unsupported: list[str] = []
        self.warnings: list[str] = []
        self.symbols: set[str] = set()
        self.sorts = sorts or {}
        self.unsigned = unsigned or set()

    def fail(self, node: Node) -> Expr:
        text = _node_text(node)
        self.unsupported.append(text)
        return Expr(text)


_BITVEC_SORT = re.compile(r"^BitVec(\d+)$")


def _kind_of_sort(sort: str | None) -> tuple[str | None, int | None]:
    if not sort:
        return None, None
    bv = _BITVEC_SORT.match(sort)
    if bv:
        return "bv", int(bv.group(1))
    return {"Int": "int", "Real": "real", "Bool": "bool", "String": "string"}.get(sort), None


def _clean_literal(text: str) -> str:
    """Drop C/Java literal suffixes (`3L`, `10u`, `1.5f`) that Python cannot parse."""
    if text.lower().startswith(("0x", "0b")):
        return re.sub(r"[uUlL]+$", "", text)
    return re.sub(r"[uUlLfF]+$", "", text) or text


def _widen(expr: Expr, target: int) -> str:
    """Promote a narrower BitVec, sign- or zero-extending per its declared type."""
    op = "SignExt" if expr.signed else "ZeroExt"
    return f"{op}({target - expr.width}, {expr.text})"


def _unify(lhs: Expr, rhs: Expr, out: Translation, op: str) -> tuple[str, str, str | None, int | None, bool]:
    """Coerce two operands to a common Z3 type, mirroring C/Java promotion rules.

    Returns `(lhs_text, rhs_text, kind, width, signed)`.
    """
    left, right = lhs.text, rhs.text
    kinds = {lhs.kind, rhs.kind} - {None}

    if lhs.kind == "bv" and rhs.kind == "bv":
        if lhs.width != rhs.width:  # C/Java promote the narrower operand
            if lhs.width < rhs.width:
                left = _widen(lhs, rhs.width)
            else:
                right = _widen(rhs, lhs.width)
        width = max(lhs.width, rhs.width)
        if lhs.signed != rhs.signed and op in _COMPARISONS:
            out.warnings.append(
                f"`{lhs.text} {op} {rhs.text}` mixes signed and unsigned operands; "
                f"encoded with the source language's unsigned semantics, but the "
                f"implicit conversion is worth asserting in the test"
            )
        return left, right, "bv", width, lhs.signed and rhs.signed

    # A BitVec meeting a mathematical Int (typically a Length(...) result).
    if kinds == {"bv", "int"}:
        if lhs.kind == "bv":
            left = f"BV2Int({lhs.text}, is_signed={lhs.signed})"
        else:
            right = f"BV2Int({rhs.text}, is_signed={rhs.signed})"
        return left, right, "int", None, True

    if "real" in kinds and kinds - {"real"}:
        if lhs.kind == "bv":
            left = f"ToReal(BV2Int({lhs.text}, is_signed={lhs.signed}))"
        elif lhs.kind == "int":
            left = f"ToReal({lhs.text})"
        if rhs.kind == "bv":
            right = f"ToReal(BV2Int({rhs.text}, is_signed={rhs.signed}))"
        elif rhs.kind == "int":
            right = f"ToReal({rhs.text})"
        return left, right, "real", None, True

    kind = lhs.kind or rhs.kind
    width = lhs.width if lhs.kind == "bv" else (rhs.width if rhs.kind == "bv" else None)
    signed = lhs.signed if lhs.kind == "bv" else rhs.signed
    return left, right, kind, width, signed


def _call_target(node: Node, lang: str) -> tuple[str | None, list[Node], Node | None]:
    """Extract `(callee_name, arguments, receiver)` from a call node."""
    if lang == "python":
        fn, args = node.child_by_field_name("function"), node.child_by_field_name("arguments")
        arg_nodes = [c for c in args.children if c.type not in ("(", ")", ",")] if args else []
        if fn is None:
            return None, arg_nodes, None
        if fn.type == "attribute":
            return _node_text(fn.child_by_field_name("attribute")), arg_nodes, fn.child_by_field_name("object")
        return _node_text(fn), arg_nodes, None
    if lang == "java":
        name, args = node.child_by_field_name("name"), node.child_by_field_name("arguments")
        arg_nodes = [c for c in args.children if c.type not in ("(", ")", ",")] if args else []
        return (_node_text(name) if name else None), arg_nodes, node.child_by_field_name("object")
    fn, args = node.child_by_field_name("function"), node.child_by_field_name("arguments")
    arg_nodes = [c for c in args.children if c.type not in ("(", ")", ",")] if args else []
    if fn is None:
        return None, arg_nodes, None
    if fn.type == "field_expression":
        return _node_text(fn.child_by_field_name("field")), arg_nodes, fn.child_by_field_name("argument")
    return _node_text(fn), arg_nodes, None


def _translate_call(node: Node, lang: str, out: Translation) -> Expr:
    """Translate a call, but only for the whitelist with exact Z3 semantics."""
    table = {"python": _PURE_CALLS_PY, "java": _PURE_CALLS_JAVA, "cpp": _PURE_CALLS_CPP}[lang]
    name, args, receiver = _call_target(node, lang)
    if name is None or name not in table:
        return out.fail(node)
    mapped = table[name]
    operands = ([receiver] if receiver is not None else []) + args

    if mapped == "Length":
        if len(operands) != 1:
            return out.fail(node)
        target = _translate(operands[0], lang, out)
        if target.kind not in ("string", None):
            return out.fail(node)
        return Expr(f"Length({target.text})", "int")
    if mapped == "_is_empty":
        if len(operands) != 1:
            return out.fail(node)
        return Expr(f"(Length({_translate(operands[0], lang, out).text}) == 0)", "bool")
    if mapped == "_equals":
        if len(operands) != 2:
            return out.fail(node)
        left, right = _translate(operands[0], lang, out), _translate(operands[1], lang, out)
        return Expr(f"({left.text} == {right.text})", "bool")
    if mapped == "_abs":
        if len(operands) != 1:
            return out.fail(node)
        inner = _translate(operands[0], lang, out)
        return Expr(f"If({inner.text} >= 0, {inner.text}, -{inner.text})", inner.kind, inner.width, inner.signed)
    return out.fail(node)


def _translate(node: Node, lang: str, out: Translation) -> Expr:
    """Recursively render a condition subtree as a typed z3py expression."""
    kind = node.type

    if kind in _PAREN_WRAPPERS:
        inner = [c for c in node.children if c.type not in ("(", ")")]
        if not inner:
            return out.fail(node)
        sub = _translate(inner[0], lang, out)
        return Expr(f"({sub.text})", sub.kind, sub.width, sub.signed)

    if kind == "identifier":
        name = _node_text(node)
        out.symbols.add(name)
        expr_kind, width = _kind_of_sort(out.sorts.get(name))
        return Expr(name, expr_kind, width, name not in out.unsigned)

    if kind in _TRUE_TYPES:
        return Expr("True", "bool")
    if kind in _FALSE_TYPES:
        return Expr("False", "bool")
    if kind in _LITERAL_TYPES:
        return Expr(_clean_literal(_node_text(node)))
    if kind in ("string_literal", "string"):
        return Expr(f"StringVal({_node_text(node)})", "string")
    if kind in ("null_literal", "none", "nullptr"):
        return out.fail(node)

    if kind in ("call_expression", "call", "method_invocation"):
        return _translate_call(node, lang, out)

    # C++ / Java infix and logical operators.
    if kind == "binary_expression":
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        op_node = node.child_by_field_name("operator")
        op = _node_text(op_node) if op_node is not None else None
        if op is None:  # older grammars expose the operator as an unnamed child
            middle = [c for c in node.children if c is not left and c is not right]
            op = _node_text(middle[0]) if middle else None
        if left is None or right is None or op is None:
            return out.fail(node)
        lhs, rhs = _translate(left, lang, out), _translate(right, lang, out)
        if op in _LOGICAL:
            return Expr(f"{_LOGICAL[op]}({lhs.text}, {rhs.text})", "bool")
        if op not in _INFIX:
            return out.fail(node)
        ltext, rtext, res_kind, width, signed = _unify(lhs, rhs, out, op)
        if op == ">>" and res_kind == "bv" and not signed:
            return Expr(f"LShR({ltext}, {rtext})", "bv", width, signed)
        if op in _COMPARISONS:
            # In C/Java an unsigned operand makes the whole comparison unsigned,
            # while z3's `<` on BitVecs is signed — use the U-prefixed builtins.
            if res_kind == "bv" and not signed and op in _UNSIGNED_COMPARE:
                return Expr(f"{_UNSIGNED_COMPARE[op]}({ltext}, {rtext})", "bool")
            return Expr(f"({ltext} {op} {rtext})", "bool")
        if op in _BITWISE and res_kind != "bv":
            out.warnings.append(f"bitwise `{op}` applied to a non-BitVec operand")
        return Expr(f"({ltext} {op} {rtext})", res_kind, width, signed)

    if kind in ("unary_expression", "unary_operator"):
        operand = node.child_by_field_name("argument") or node.child_by_field_name("operand")
        op_node = node.child_by_field_name("operator")
        if operand is None:
            kids = list(node.children)
            operand = kids[-1] if kids else None
            op_node = kids[0] if len(kids) > 1 else op_node
        if operand is None or op_node is None:
            return out.fail(node)
        op = _node_text(op_node)
        inner = _translate(operand, lang, out)
        if op == "!":
            return Expr(f"Not({inner.text})", "bool")
        if op in ("-", "~", "+"):
            return Expr(f"({op}{inner.text})", inner.kind, inner.width, inner.signed)
        return out.fail(node)

    # Python-specific nodes.
    if kind == "boolean_operator":
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        op_node = node.child_by_field_name("operator")
        op = _node_text(op_node) if op_node is not None else None
        if left is None or right is None or op not in _LOGICAL:
            return out.fail(node)
        lhs, rhs = _translate(left, lang, out), _translate(right, lang, out)
        return Expr(f"{_LOGICAL[op]}({lhs.text}, {rhs.text})", "bool")

    if kind == "not_operator":
        argument = node.child_by_field_name("argument")
        if argument is None:
            return out.fail(node)
        return Expr(f"Not({_translate(argument, lang, out).text})", "bool")

    if kind == "comparison_operator":
        # Python allows chains (`0 < b < 1.5`); z3 does not, so expand to And(...).
        operands = [c for c in node.children if c.type not in _PY_COMPARE_OPS and c.type not in ("in", "not", "is")]
        ops = [_node_text(c) for c in node.children if c.type in _PY_COMPARE_OPS]
        if len(ops) != len(operands) - 1 or not ops:
            return out.fail(node)
        rendered = [_translate(o, lang, out) for o in operands]
        parts = []
        for i, op in enumerate(ops):
            ltext, rtext, _kind, _width, _signed = _unify(rendered[i], rendered[i + 1], out, op)
            parts.append(f"({ltext} {op} {rtext})")
        return Expr(parts[0] if len(parts) == 1 else f"And({', '.join(parts)})", "bool")

    if kind == "binary_operator":
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        op_node = node.child_by_field_name("operator")
        op = _node_text(op_node) if op_node is not None else None
        if left is None or right is None:
            return out.fail(node)
        if op == "//":  # z3 Int division truncates; equal to floor only for same-sign operands
            out.warnings.append(f"`{_node_text(node)}`: floor division approximated as truncating division")
            op = "/"
        if op not in _INFIX:
            return out.fail(node)
        lhs, rhs = _translate(left, lang, out), _translate(right, lang, out)
        ltext, rtext, res_kind, width, signed = _unify(lhs, rhs, out, op)
        return Expr(f"({ltext} {op} {rtext})", res_kind, width, signed)

    if kind == "conditional_expression":  # ternary used as a value
        return out.fail(node)

    return out.fail(node)


def translate_condition(
    node: Node,
    lang: str,
    sorts: dict[str, str] | None = None,
    unsigned: set[str] | None = None,
) -> dict[str, Any]:
    """Translate a condition node into z3py, reporting symbols, warnings, failures."""
    out = Translation(sorts, unsigned)
    expr = _translate(node, lang, out)
    return {
        "z3": expr.text,
        "symbols": sorted(out.symbols),
        "unsupported": out.unsupported,
        "warnings": out.warnings,
    }


def negate(z3_text: str) -> str:
    return f"Not({z3_text})"


# --------------------------------------------------------------------------- #
# Branch discovery and path conditions
# --------------------------------------------------------------------------- #

_IF_TYPES = {"if_statement"}
_ELIF_TYPES = {"elif_clause"}
_LOOP_TYPES = {"while_statement", "for_statement", "do_statement", "enhanced_for_statement"}
_UNCONDITIONAL_LOOPS = {"do_statement", "enhanced_for_statement"}


def _condition_node(node: Node, lang: str) -> Node | None:
    """The boolean expression governing a branch node, unwrapped from parens."""
    cond = node.child_by_field_name("condition")
    if cond is None:
        return None
    if cond.type in _PAREN_WRAPPERS:
        inner = [c for c in cond.children if c.type not in ("(", ")")]
        return inner[0] if inner else None
    return cond


def _branch_nodes(fn_node: Node, lang: str) -> list[Node]:
    types = _IF_TYPES | _ELIF_TYPES | _LOOP_TYPES
    return [n for n in _walk(fn_node) if n.type in types and n is not fn_node]


def _guard_for(node: Node, lang: str, polarity: bool, ctx: dict[str, Any]) -> dict[str, Any] | None:
    """Build one path-condition entry for a branch node taken with `polarity`."""
    if node.type in _UNCONDITIONAL_LOOPS:
        if node.type == "do_statement":
            return None  # a do/while body always executes once; no guard needed
        return {
            "line": node.start_point[0] + 1,
            "source": _node_text(node).split("\n")[0].strip(),
            "z3": None,
            "polarity": polarity,
            "unsupported": ["for-each loop: constrain the iterable to be non-empty in the test"],
        }
    cond = _condition_node(node, lang)
    if cond is None:  # e.g. `for (;;)`
        return None
    tr = translate_condition(cond, lang, ctx["sorts"], ctx["unsigned"])
    return {
        "line": node.start_point[0] + 1,
        "source": _node_text(cond),
        "z3": tr["z3"] if polarity else negate(tr["z3"]),
        "polarity": polarity,
        "symbols": tr["symbols"],
        "unsupported": tr["unsupported"],
        "warnings": tr["warnings"],
    }


def path_condition(node: Node, fn_node: Node, lang: str, ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """Guards that must hold for control to reach `node`, outermost first."""
    chain: list[dict[str, Any]] = []
    current, parent = node, node.parent
    while parent is not None and current is not fn_node:
        if parent.type in _IF_TYPES | _ELIF_TYPES:
            consequence = parent.child_by_field_name("consequence")
            alternatives = parent.children_by_field_name("alternative")
            guard = None
            if consequence is not None and current == consequence:
                guard = _guard_for(parent, lang, True, ctx)
            elif any(current == alt for alt in alternatives):
                guard = _guard_for(parent, lang, False, ctx)
            if guard is not None:
                chain.append(guard)
        elif parent.type in _LOOP_TYPES:
            body = parent.child_by_field_name("body")
            if body is not None and current == body:
                guard = _guard_for(parent, lang, True, ctx)
                if guard is not None:
                    chain.append(guard)
        current, parent = parent, parent.parent
    chain.reverse()
    return chain


# --------------------------------------------------------------------------- #
# Spec building
# --------------------------------------------------------------------------- #

# Names that appear in translated conditions but are z3 constructs, not variables.
_Z3_NAMES = {"And", "Or", "Not", "If", "Implies", "True", "False", "StringVal", "Length", "Select"}


def _build_spec(
    params: list[dict[str, Any]],
    guards: list[dict[str, Any]],
    branch_guard: dict[str, Any],
    unsigned: set[str],
) -> dict[str, Any]:
    """Assemble a solver spec from the enclosing guards plus the branch itself."""
    variables = {p["name"]: p["sort"] for p in params if p["sort"]}
    constraints: list[dict[str, str]] = []
    blockers: list[str] = []
    symbols: set[str] = set()

    warnings: list[str] = []
    for guard in guards + [branch_guard]:
        if guard["z3"] is None:
            blockers.extend(guard.get("unsupported") or ["untranslatable guard"])
            continue
        if guard.get("unsupported"):
            blockers.extend(f"line {guard['line']}: cannot translate `{u}`" for u in guard["unsupported"])
        warnings.extend(guard.get("warnings") or [])
        symbols |= set(guard.get("symbols") or [])
        constraints.append({
            "name": f"line{guard['line']}_{'true' if guard['polarity'] else 'false'}",
            "expr": guard["z3"],
        })

    unknown = sorted(symbols - set(variables) - _Z3_NAMES)
    if unknown:
        blockers.append(
            f"symbols not bound to a parameter: {', '.join(unknown)} "
            "(a local or field — express it in terms of the inputs, or declare it in 'vars')"
        )
    untyped = [p["name"] for p in params if not p["sort"] and p["name"] in symbols]
    if untyped:
        blockers.append(f"parameters used in the condition but with no Z3 sort: {', '.join(untyped)}")

    built: dict[str, Any] = {
        "spec": {"vars": variables, "constraints": constraints},
        "solvable": not blockers,
        "blockers": blockers,
        "unsigned_vars": sorted(unsigned & set(variables)),
    }
    if warnings:
        built["warnings"] = sorted(set(warnings))
    return built


_UNSIGNED_CPP = re.compile(r"\b(unsigned|uint\d+_t|size_t)\b")


def _unsigned_params(params: list[dict[str, Any]], lang: str) -> set[str]:
    """Parameter names whose source type is unsigned (drives ZeroExt vs SignExt)."""
    out: set[str] = set()
    for param in params:
        declared = param.get("declared_type") or ""
        if lang == "cpp" and _UNSIGNED_CPP.search(declared):
            out.add(param["name"])
        elif lang == "java" and declared.strip() == "char":
            out.add(param["name"])
    return out


def analyze_function(source: bytes, lang: str, fn_node: Node) -> dict[str, Any]:
    """Full symbolic analysis of one function: params, branches, per-outcome specs."""
    params = extract_parameters(fn_node, lang)
    ctx = {
        "sorts": {p["name"]: p["sort"] for p in params if p["sort"]},
        "unsigned": _unsigned_params(params, lang),
    }
    branches: list[dict[str, Any]] = []

    for node in _branch_nodes(fn_node, lang):
        line = node.start_point[0] + 1
        guards = path_condition(node, fn_node, lang, ctx)
        entry: dict[str, Any] = {
            "id": f"{node.type}@{line}",
            "kind": node.type,
            "line": line,
            "path_condition": [
                {"line": g["line"], "z3": g["z3"], "source": g["source"]} for g in guards
            ],
            "outcomes": {},
        }
        for polarity, label in ((True, "true"), (False, "false")):
            guard = _guard_for(node, lang, polarity, ctx)
            if guard is None:
                continue
            entry["condition"] = guard["source"]
            entry["outcomes"][label] = _build_spec(params, guards, guard, ctx["unsigned"])
        branches.append(entry)

    notes = []
    if any(n.type in ("switch_statement", "switch_expression") for n in _walk(fn_node)):
        notes.append("switch/case statements are not decomposed; write those specs by hand")
    if any(n.type == "conditional_expression" for n in _walk(fn_node)):
        notes.append("ternary expressions are reported as untranslatable; split them manually")

    return {
        "function": _qualified_name(fn_node, lang),
        "language": lang,
        "start_line": fn_node.start_point[0] + 1,
        "end_line": fn_node.end_point[0] + 1,
        "parameters": params,
        "branches": branches,
        "notes": notes,
    }


def analyze_file_function(path: Path, function_name: str) -> dict[str, Any]:
    """Locate `function_name` in `path` and run the symbolic analysis on it."""
    lang = _lang_for(path)
    if lang is None:
        raise ValueError(f"unsupported file type: {path.suffix}")
    source = path.read_bytes()
    tree = _parse(source, lang)
    for node in _walk(tree.root_node):
        if node.type in LANGUAGES[lang]["function_types"]:
            name = _qualified_name(node, lang)
            if function_name == name or function_name in name:
                result = analyze_function(source, lang, node)
                result["file"] = str(path)
                return result
    raise ValueError(f"no function matching '{function_name}' in {path}")


# --------------------------------------------------------------------------- #
# Solving (delegates to the symbolic-execution skill's solver)
# --------------------------------------------------------------------------- #

_SOLVER_PATH = SKILLS_DIR / "symbolic-execution" / "solve_constraints.py"
_solver_module = None


def load_solver():
    """Import the skill's solver script so tool and skill share one implementation."""
    global _solver_module
    if _solver_module is None:
        if not _SOLVER_PATH.is_file():
            raise FileNotFoundError(f"solver script missing: {_SOLVER_PATH}")
        spec = importlib.util.spec_from_file_location("_symexec_solver", _SOLVER_PATH)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load solver from {_SOLVER_PATH}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _solver_module = module
    return _solver_module


def solve(spec: dict[str, Any]) -> dict[str, Any]:
    """Solve one spec dict, returning the solver's result dict."""
    solver = load_solver()
    result, _exit_code = solver.solve_spec(spec)
    return result


# Z3 is free to answer with any satisfying assignment, and for BitVecs that is
# usually a huge wraparound value that makes a terrible test fixture. Try first
# inside a small, human-readable range and only fall back to the unrestricted
# problem if that range genuinely has no solution.
_SMALL_BOUND = 1000
_SMALL_STRING_LENGTH = 8


def _readability_bounds(variables: dict[str, str], unsigned: set[str]) -> list[dict[str, str]]:
    """Extra constraints that keep a satisfying model small enough to read."""
    bounds: list[dict[str, str]] = []
    for name, sort in variables.items():
        kind, _width = _kind_of_sort(sort)
        if kind == "bv" and name in unsigned:
            expr = f"ULE({name}, {_SMALL_BOUND})"  # signed bounds would admit huge values
        elif kind in ("bv", "int", "real"):
            expr = f"And({name} >= -{_SMALL_BOUND}, {name} <= {_SMALL_BOUND})"
        elif kind == "string":
            expr = f"Length({name}) <= {_SMALL_STRING_LENGTH}"
        else:
            continue
        bounds.append({"name": f"_readable_{name}", "expr": expr})
    return bounds


def solve_outcome(
    spec: dict[str, Any],
    num_solutions: int = 1,
    prefer_small: bool = True,
    unsigned: set[str] | None = None,
) -> dict[str, Any]:
    """Solve one branch spec, preferring small inputs but never losing a solution.

    Returns the raw solver result with a `bounded` flag recording whether the
    readable-range attempt succeeded. An `unsat` here is a real proof of
    infeasibility: it always comes from the unrestricted problem.
    """
    base = {**spec, "num_solutions": num_solutions}
    if prefer_small:
        extra = _readability_bounds(spec["vars"], unsigned or set())
        bounded = {**base, "constraints": list(spec["constraints"]) + extra}
        result = solve(bounded)
        if result["status"] == "sat":
            result["bounded"] = True
            return result
    result = solve(base)
    result["bounded"] = False
    return result


def solve_all_branches(
    path: Path,
    function_name: str,
    num_solutions: int = 1,
    prefer_small: bool = True,
) -> dict[str, Any]:
    """Analyze a function and solve every solvable branch outcome.

    Returns the analysis with each outcome annotated with `status` and, when
    satisfiable, the concrete `inputs` that drive execution down that path.
    """
    analysis = analyze_file_function(path, function_name)
    counts = {"sat": 0, "unsat": 0, "unknown": 0, "skipped": 0, "error": 0}

    for branch in analysis["branches"]:
        for _label, outcome in branch["outcomes"].items():
            if not outcome["solvable"]:
                outcome["status"] = "skipped"
                counts["skipped"] += 1
                continue
            try:
                result = solve_outcome(
                    outcome["spec"], num_solutions, prefer_small, set(outcome["unsigned_vars"])
                )
            except Exception as exc:  # noqa: BLE001 - reported per branch, never fatal
                outcome["status"] = "error"
                outcome["error"] = f"{type(exc).__name__}: {exc}"
                counts["error"] += 1
                continue
            outcome["status"] = result["status"]
            counts[result["status"]] = counts.get(result["status"], 0) + 1
            if result["status"] == "sat":
                outcome["inputs"] = result["model"]
                if not result["bounded"]:
                    outcome.setdefault("warnings", []).append(
                        "no solution exists within the readable range; these inputs are extreme by necessity"
                    )
                if num_solutions > 1:
                    outcome["alternative_inputs"] = result["solutions"][1:]
            elif result["status"] == "unsat":
                outcome["unsat_core"] = result["unsat_core"]
                outcome["verdict"] = "path is infeasible — dead code, do not write a test for it"
            elif result["status"] == "unknown":
                outcome["reason"] = result.get("reason")

    analysis["summary"] = counts
    return analysis
