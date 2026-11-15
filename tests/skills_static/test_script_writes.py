"""Skill scripts write only through an exclusive-create helper.

An AST scan of ``skills/*/scripts/*.py`` (the vendored ``_kernel/`` copies and the ``qa.py`` launcher are not skill scripts).
A write call is ``open`` with a mode that holds ``w``, ``a``, ``x`` or ``+``, ``.write_text``, ``.write_bytes`` or ``os.open``
with ``O_CREAT``. It passes only inside a function whose name starts with ``write_`` and whose body uses ``os.O_EXCL``, or as a
call to the kernel's atomic write (``write_atomic``). Anything else is listed by path and line, or named in ``ALLOWED`` with its
reason.
"""
from __future__ import annotations

import ast
from typing import Dict, Iterator, List, Optional, Tuple

from helpers import REPO

SCRIPT_FILES = sorted(p for p in (REPO / "skills").glob("*/scripts/*.py") if p.name != "qa.py")

# (path relative to the repository, enclosing function) -> why the write is not through the exclusive helper.
ALLOWED: Dict[Tuple[str, str], str] = {
    ("skills/qa-codebase-intelligence/scripts/write_map.py", "_atomic"):
        "writes its own temporary file at a random name beside the map, then renames it over the map; milestone 3 script, untouched here",
    ("skills/qa-codebase-intelligence/scripts/write_map.py", "write"):
        "appends one line to its writes log through a no-follow open, an append-only file by design; milestone 3 script, untouched here",
    ("skills/qa-mutation/scripts/_mutation.py", "finish"):
        "writes the converted mutation report to the path the caller named; milestone 3 script, untouched here",
}


def functions_with_parents(tree: ast.AST) -> Iterator[Tuple[ast.AST, Optional[ast.AST]]]:
    """Every node with its nearest enclosing function (None at module level)."""
    def visit(node: ast.AST, function: Optional[ast.AST]) -> Iterator[Tuple[ast.AST, Optional[ast.AST]]]:
        yield node, function
        inner = node if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
        for child in ast.iter_child_nodes(node):
            yield from visit(child, inner)
    yield from visit(tree, None)


def mode_text(call: ast.Call, position: int) -> Optional[str]:
    node = call.args[position] if len(call.args) > position else None
    for keyword in call.keywords:
        if keyword.arg == "mode":
            node = keyword.value
    if node is None:
        return ""                                          # the default mode reads
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def names_in(node: ast.AST) -> List[str]:
    return [n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)] + [n.id for n in ast.walk(node) if isinstance(n, ast.Name)]


def is_write_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute):
        if func.attr in ("write_text", "write_bytes"):
            return True
        if func.attr == "open":
            if isinstance(func.value, ast.Name) and func.value.id == "os":
                return len(call.args) > 1 and "O_CREAT" in names_in(call.args[1])
            mode = mode_text(call, 0)                      # Path.open(mode)
            return mode is None or any(c in mode for c in "wax+")
        return False
    if isinstance(func, ast.Name) and func.id == "open":
        mode = mode_text(call, 1)
        return mode is None or any(c in mode for c in "wax+")   # a mode that is not a literal cannot be shown to read
    return False


def is_atomic_call(call: ast.Call) -> bool:
    func = call.func
    return (isinstance(func, ast.Attribute) and func.attr == "write_atomic") or (isinstance(func, ast.Name) and func.id == "write_atomic")


def offending_calls(path) -> List[Tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node, function in functions_with_parents(tree):
        if not isinstance(node, ast.Call) or not is_write_call(node) or is_atomic_call(node):
            continue
        name = function.name if function is not None else "<module>"
        exclusive = function is not None and name.startswith("write_") and "O_EXCL" in names_in(function)
        if not exclusive:
            found.append((node.lineno, name))
    return found


def test_skill_scripts_write_only_through_the_exclusive_helper():
    assert len(SCRIPT_FILES) > 20, "the scan found no skill scripts"
    offences = []
    for path in SCRIPT_FILES:
        rel = path.relative_to(REPO).as_posix()
        for line, function in offending_calls(path):
            if (rel, function) not in ALLOWED:
                offences.append("%s:%d (in %s)" % (rel, line, function))
    assert not offences, "writes outside a write_* function that uses os.O_EXCL, or the kernel's write_atomic:\n" + "\n".join(offences)


def test_every_allowed_write_still_exists():
    seen = {(p.relative_to(REPO).as_posix(), function) for p in SCRIPT_FILES for _, function in offending_calls(p)}
    assert set(ALLOWED) <= seen, sorted(set(ALLOWED) - seen)
    assert all(reason.strip() for reason in ALLOWED.values())


def test_the_scan_recognises_each_write_form(tmp_path):
    source = (
        "import os\n"
        "def helper(p, d):\n"
        "    open(p, 'w')\n"                                   # 3
        "    open(p, mode='a')\n"                              # 4
        "    p.write_text(d)\n"                                # 5
        "    p.write_bytes(d)\n"                               # 6
        "    os.open(p, os.O_WRONLY | os.O_CREAT)\n"           # 7
        "    p.open('r+')\n"                                   # 8
        "    open(p)\n    open(p, 'rb')\n    os.open(p, os.O_RDONLY)\n"
        "def write_new(p, d):\n"
        "    os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL)\n"
        "def write_loose(p, d):\n"
        "    os.open(p, os.O_WRONLY | os.O_CREAT)\n"           # 15: a write_ name without O_EXCL
        "def other(p, d):\n"
        "    workspace.write_atomic(p, d)\n"
    )
    script = tmp_path / "s.py"
    script.write_text(source, encoding="utf-8")
    assert offending_calls(script) == [(3, "helper"), (4, "helper"), (5, "helper"), (6, "helper"), (7, "helper"), (8, "helper"),
                                       (15, "write_loose")]
