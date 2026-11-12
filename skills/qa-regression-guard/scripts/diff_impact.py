"""What a change touches, and which tests reach it.

Usage (from the project root):
    python3 diff_impact.py --base <commit> [--head <commit>]

Prints one JSON object:

    changed         [{path, kind, status}]  every changed file; kind is product, test, test-support, test-config, ci, snapshot,
                    contract or docs, and status A, M or D
    impacted_tests  [{path, why}]  the test files that are changed or that reach a changed file through imports, with the chain
    contracts       [{path, kind, tool, status}]  changed API or schema contracts and the tool that compares two versions
    unreached       product files no test reaches through the import graph
    selection       {full_suite_recommended, reason}

The import graph is built from the head commit's files: Python through its ``import`` statements, JavaScript and TypeScript
through relative ``import``/``require``. Go test files reach the files of their own directory and JVM tests the class they
are named after. Anything else, and any change to test support, test configuration or CI, recommends the full suite: a
selection is only a way to look first, never a reason to skip the rest. The script reads git only, through the kernel's
neutralised session, and writes nothing. Standard library only.
"""
from __future__ import annotations

import argparse
import ast
import os
import posixpath
import re
import sys
from collections import deque
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import _change  # noqa: E402
from _change import ChangeError, contract_of, is_test_file, kind_of, language_of, parse_diff, python_module  # noqa: E402

GRAPH_LANGUAGES = ("python", "javascript", "typescript")
JS_EXTENSIONS = ("", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs", ".mts", ".cts", "/index.js", "/index.ts", "/index.jsx", "/index.tsx")
JS_IMPORT = re.compile(r"""(?:\bimport\s+(?:[\w*{}\s,]+\s+from\s+)?|\bexport\s+[\w*{}\s,]+\s+from\s+|\brequire\s*\(\s*|\bimport\s*\(\s*)"""
                       r"""(['"])([^'"\n]+)\1""")
FULL_SUITE_KINDS = ("test-support", "test-config", "ci")
MAX_FILES = 20000


# --------------------------------------------------------------------------- the import graph

def python_index(paths: Iterable[str]) -> Dict[str, str]:
    """Module name -> file, for every Python file; a file under a top-level ``src/`` or ``lib/`` is also known without it."""
    index: Dict[str, str] = {}
    for path in sorted(paths):
        name = python_module(path)
        if name is None:
            continue
        index.setdefault(name, path)
        head, _, rest = name.partition(".")
        if head in ("src", "lib") and rest:
            index.setdefault(rest, path)
    return index


def python_imports(text: str, path: str, index: Dict[str, str]) -> Set[str]:
    """The files of this repository that ``text`` (the Python file at ``path``) imports, packages' ``__init__`` included."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    here = python_module(path) or ""
    package = here.split(".") if path.endswith("__init__.py") else here.split(".")[:-1]
    names: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package[:len(package) - (node.level - 1)] if node.level - 1 <= len(package) else []
                base = ".".join(parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            if not base:
                continue
            names.append(base)
            names.extend(base + "." + alias.name for alias in node.names if alias.name != "*")
    found: Set[str] = set()
    for name in names:
        parts = name.split(".")
        for size in range(1, len(parts) + 1):
            target = index.get(".".join(parts[:size]))
            if target is not None and target != path:
                found.add(target)
    return found


def js_imports(text: str, path: str, known_files: Set[str]) -> Set[str]:
    """The files of ``known_files`` that the JavaScript or TypeScript ``text`` at ``path`` imports by a relative specifier."""
    found: Set[str] = set()
    folder = posixpath.dirname(path)
    for match in JS_IMPORT.finditer(text):
        spec = match.group(2)
        if not spec.startswith("."):
            continue
        base = posixpath.normpath(posixpath.join(folder, spec))
        for ext in JS_EXTENSIONS:
            if base + ext in known_files:
                found.add(base + ext)
                break
    return found


def reverse_closure(graph: Dict[str, Set[str]], changed: Set[str]) -> Set[str]:
    """Every file of ``graph`` (file -> files it imports) that reaches a file of ``changed``, the changed files included."""
    reverse: Dict[str, Set[str]] = {}
    for source, targets in graph.items():
        for target in targets:
            reverse.setdefault(target, set()).add(source)
    seen, queue = set(changed), deque(sorted(changed))
    while queue:
        for source in sorted(reverse.get(queue.popleft(), ())):
            if source not in seen:
                seen.add(source)
                queue.append(source)
    return seen


def chains(graph: Dict[str, Set[str]], start: str, changed: Set[str]) -> Dict[str, List[str]]:
    """For each changed file ``start`` reaches, the shortest import chain from ``start`` to it (both ends included)."""
    parent: Dict[str, Optional[str]] = {start: None}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for target in sorted(graph.get(current, ())):
            if target not in parent:
                parent[target] = current
                queue.append(target)
    out = {}
    for target in sorted(changed):
        if target in parent and target != start:
            path, node = [], target
            while node is not None:
                path.append(node)
                node = parent[node]
            out[target] = list(reversed(path))
    return out


def label(path: str) -> str:
    return python_module(path) or path


def build_graph(repo: "_change.Repo", head: str) -> Tuple[Dict[str, Set[str]], List[str]]:
    files = repo.files(head)[:MAX_FILES]
    sources = [p for p in files if language_of(p) in GRAPH_LANGUAGES]
    texts = repo.blobs(head, sources)
    index = python_index(p for p in sources if p.endswith(".py"))
    known = set(files)
    graph: Dict[str, Set[str]] = {}
    for path in sorted(texts):
        if path.endswith(".py"):
            graph[path] = python_imports(texts[path], path, index)
        else:
            graph[path] = js_imports(texts[path], path, known)
    return graph, files


# --------------------------------------------------------------------------- the impact

def _stem(path: str) -> str:
    return posixpath.splitext(posixpath.basename(path))[0]


def impact(repo: "_change.Repo", base: str, head: str) -> Dict[str, Any]:
    entries = [e for e in parse_diff(repo.diff(base, head))]
    contract_texts = {}
    for entry in entries:
        if entry["path"].endswith((".yaml", ".yml", ".json")):
            side = base if entry["status"] == "D" else head
            contract_texts.update(repo.blobs(side, [entry["path"]], limit=4 << 20))
    changed = []
    for entry in sorted(entries, key=lambda e: e["path"]):
        changed.append({"path": entry["path"], "kind": kind_of(entry["path"], contract_texts.get(entry["path"])), "status": entry["status"]})
    contracts = []
    for item in changed:
        found = contract_of(item["path"], contract_texts.get(item["path"]))
        if found is not None:
            contracts.append({"path": item["path"], "kind": found[0], "tool": found[1], "status": item["status"]})

    graph, files = build_graph(repo, head)
    tests_at_head = [p for p in files if is_test_file(p) and kind_of(p) == "test"]
    changed_paths = {c["path"] for c in changed if c["status"] != "D"}
    product = {c["path"] for c in changed if c["kind"] == "product" and c["status"] != "D"}
    why: Dict[str, List[str]] = {}
    reached: Set[str] = set()
    for item in changed:
        if item["kind"] == "test" and item["status"] != "D":
            why.setdefault(item["path"], []).append("changed in this diff")
    for test in tests_at_head:
        lang = language_of(test)
        if lang in GRAPH_LANGUAGES:
            for target, chain in chains(graph, test, changed_paths - {test}).items():
                if target in product:
                    reached.add(target)
                if len(chain) == 2:
                    why.setdefault(test, []).append("imports %s (changed)" % label(target))
                else:
                    why.setdefault(test, []).append("reaches %s (changed) through %s" % (label(target), ", ".join(label(p) for p in chain[1:-1])))
        elif lang == "go":
            for target in sorted(product):
                if posixpath.dirname(target) == posixpath.dirname(test) and target.endswith(".go"):
                    reached.add(target)
                    why.setdefault(test, []).append("in the package of %s (changed)" % target)
        elif lang in ("java", "kotlin"):
            name = re.sub(r"(?:Tests?|IT|Spec)$", "", _stem(test))
            for target in sorted(product):
                if _stem(target) == name:
                    reached.add(target)
                    why.setdefault(test, []).append("named after %s (changed)" % target)
    impacted = [{"path": path, "why": why[path]} for path in sorted(why)]
    unreached = sorted(product - reached)

    reasons = []
    for item in changed:
        if item["kind"] in FULL_SUITE_KINDS:
            reasons.append("%s changed (%s): it can change how every test runs or is reported" % (item["path"], item["kind"]))
    deleted_product = sorted(c["path"] for c in changed if c["kind"] == "product" and c["status"] == "D")
    if deleted_product:
        reasons.append("product files removed: %s" % ", ".join(deleted_product))
    if unreached:
        reasons.append("no test reaches %s through the import graph" % ", ".join(unreached))
    if contracts:
        reasons.append("a contract changed: %s" % ", ".join(c["path"] for c in contracts))
    selection = {"full_suite_recommended": bool(reasons),
                 "reason": "; ".join(reasons) if reasons else "every changed product file is reached by the impacted tests; run them "
                                                               "first, then the full suite before a verdict"}
    return {"ok": True, "base": base, "head": head, "changed": changed, "impacted_tests": impacted, "contracts": contracts,
            "unreached": unreached, "selection": selection}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="What a change touches, and which tests reach it.")
    parser.add_argument("--base", required=True, help="the commit before the change")
    parser.add_argument("--head", default="HEAD", help="the commit with the change (default HEAD)")
    args = parser.parse_args(argv)
    try:
        repo = _change.Repo(os.getcwd())
        return _change.emit(impact(repo, repo.resolve(args.base), repo.resolve(args.head)))
    except ChangeError as error:
        return _change.refuse(error)


if __name__ == "__main__":
    sys.exit(main())
