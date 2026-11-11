from __future__ import annotations

import ast
import subprocess
import sys

from helpers import KERNEL


def test_kernel_imports_with_stdlib_only():
    code = ("import sys, pkgutil, importlib; sys.path.insert(0, sys.argv[1]); import qa; "
            "[importlib.import_module(m.name) for m in pkgutil.walk_packages(qa.__path__, 'qa.')]")
    r = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(KERNEL)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _non_docstring_strings(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value


def test_no_kernel_string_cites_a_section_number():
    """K6: the kernel ships inside skills/, so an error message must not point at an unpublished section number."""
    cited = ["%s:%d" % (path.relative_to(KERNEL), line) for path in sorted((KERNEL / "qa").rglob("*.py"))
             for line, text in _non_docstring_strings(path) if "§" in text]
    assert cited == []
