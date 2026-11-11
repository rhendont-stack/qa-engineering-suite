"""Static scan of test files for six smells that let a test pass without checking anything.

Usage: python3 test_smells.py [PATH ...]     (default: the current directory)

Rules (one hit per test and rule, at the line that shows it):

* ``assertion-free``: the test checks nothing: no assert statement, no assertion call (``assert*``, ``expect``, ``verify``,
  ``pytest.raises``/``warns``, ``assertThrows``, ``fail``).
* ``tautological``: an assertion that cannot fail: both sides the same expression (``assert f(x) == f(x)``,
  ``assertEqual(a, a)``, ``expect(a).toBe(a)``) or a constant (``assert True``, ``assertTrue(true)``).
* ``over-mocked``: the test replaces the very thing it then calls and checks (``monkeypatch.setattr(m, "f", ...)`` and then
  ``m.f(...)``), or uses four or more mocks.
* ``sleep-based``: a fixed wait (``time.sleep``, ``Thread.sleep``, ``setTimeout`` with a number, ``waitForTimeout``).
* ``catch-all``: a broad catch around the check that swallows its failure (``except Exception``/bare ``except`` without a
  re-raise, ``catch (Exception|Throwable)`` with an empty body).
* ``snapshot-only``: the only checks compare against a stored snapshot (``toMatchSnapshot``, ``snapshot.assert_match``).

Languages: Python (``test_*.py``, ``*_test.py``; read with ``ast``), JavaScript and TypeScript (``*.test.*``, ``*.spec.*``;
``it``/``test`` blocks), Java and Kotlin (``*Test.java``, ``*Tests.java``, ``*IT.java``, ``*Test.kt``; ``@Test`` methods).
``.git``, ``.qa``, ``.claude``, ``mutants``, ``node_modules``, ``build``, ``target`` and virtual environments are skipped.

Prints one JSON object: ``files_scanned``, ``tests``, ``smells`` (``rule``, ``path``, ``line``, ``test``, ``language``,
``evidence``) sorted by path and line, ``by_rule``. A hit is a lead for review, never a verdict: read the test before
recording it. Reads only; standard library only; deterministic. Exit 0, or 2 on a path that does not exist.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SKIP_DIRS = {".git", ".qa", ".claude", "mutants", "node_modules", "build", "target", "dist", "__pycache__", ".venv", "venv",
             ".tox", ".mypy_cache", ".pytest_cache", ".gradle", "site-packages"}
PY_TEST = re.compile(r"^(?:test_.*|.*_test)\.py$")
JS_TEST = re.compile(r"^.*\.(?:test|spec)\.[cm]?[jt]sx?$")
JVM_TEST = re.compile(r"^.*(?:Test|Tests|IT)\.(?:java|kt)$")
RULES = ("assertion-free", "tautological", "over-mocked", "sleep-based", "catch-all", "snapshot-only")
MOCK_LIMIT = 4

Hit = Dict[str, Any]


def _hit(rule: str, path: str, line: int, test: str, language: str, evidence: str) -> Hit:
    return {"rule": rule, "path": path, "line": line, "test": test, "language": language, "evidence": evidence.strip()[:160]}


# --------------------------------------------------------------------------- Python

def _dotted(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return base + "." + node.attr if base else None
    return None


def _call_name(node: ast.Call) -> str:
    return _dotted(node.func) or ""


def _is_check_call(name: str) -> bool:
    last = name.rpartition(".")[2]
    return (last.startswith(("assert", "expect", "verify")) or last in ("raises", "warns", "fail", "assert_match")
            or name.startswith("pytest.") and last in ("raises", "warns", "fail"))


def _is_snapshot_call(name: str) -> bool:
    return "snapshot" in name.lower() or name.rpartition(".")[2] in ("assert_match", "assert_match_snapshot")


def _same(a: ast.AST, b: ast.AST) -> bool:
    return ast.dump(a) == ast.dump(b)


def _constant_truth(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and bool(node.value)


def _source(lines: List[str], node: ast.AST) -> str:
    return lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""


def _py_patches(fn: ast.AST) -> List[Tuple[int, Optional[str]]]:
    """``(line, the dotted thing replaced or None)`` for each mock or patch the test sets up, decorators included."""
    found: List[Tuple[int, Optional[str]]] = []
    for deco in getattr(fn, "decorator_list", []):
        target = deco.func if isinstance(deco, ast.Call) else deco
        name = _dotted(target) or ""
        if name.endswith(("patch", "patch.object", "patch.dict", "patch.multiple")):
            what = None
            if isinstance(deco, ast.Call) and deco.args and isinstance(deco.args[0], ast.Constant) and isinstance(deco.args[0].value, str):
                what = deco.args[0].value
            found.append((deco.lineno, what))
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        last = name.rpartition(".")[2]
        if name.endswith("monkeypatch.setattr") or name.endswith("patch.object") or name in ("mocker.patch.object",):
            what = None
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                base = _dotted(node.args[0])
                what = "%s.%s" % (base, node.args[1].value) if base else None
            elif node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                what = node.args[0].value
            found.append((node.lineno, what))
        elif last in ("patch", "Mock", "MagicMock", "AsyncMock", "create_autospec") and name not in ("dict.patch",):
            what = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str) else None
            found.append((node.lineno, what))
    return found


def _py_test(fn: ast.AST, label: str, path: str, lines: List[str]) -> List[Hit]:
    hits: List[Hit] = []
    asserts = [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
    checks = [c for c in calls if _is_check_call(_call_name(c))]
    with_raises = [n for n in ast.walk(fn) if isinstance(n, (ast.With, ast.AsyncWith))
                   and any(isinstance(i.context_expr, ast.Call) and _is_check_call(_call_name(i.context_expr)) for i in n.items)]
    if not asserts and not checks and not with_raises:
        hits.append(_hit("assertion-free", path, fn.lineno, label, "python", _source(lines, fn)))
    for node in asserts:
        test = node.test
        if _constant_truth(test) or (isinstance(test, ast.Compare) and len(test.comparators) == 1
                                     and isinstance(test.ops[0], (ast.Eq, ast.Is, ast.GtE, ast.LtE))
                                     and _same(test.left, test.comparators[0])):
            hits.append(_hit("tautological", path, node.lineno, label, "python", _source(lines, node)))
            break
    else:
        for call in checks:
            last = _call_name(call).rpartition(".")[2]
            if last in ("assertEqual", "assertEquals", "assertIs", "assertListEqual", "assertDictEqual") and len(call.args) >= 2 \
                    and _same(call.args[0], call.args[1]):
                hits.append(_hit("tautological", path, call.lineno, label, "python", _source(lines, call)))
                break
            if last == "assertTrue" and call.args and _constant_truth(call.args[0]):
                hits.append(_hit("tautological", path, call.lineno, label, "python", _source(lines, call)))
                break
    patches = _py_patches(fn)
    called = {(_call_name(c), c.lineno) for c in calls}
    replaced_then_called = next(((line, what) for line, what in patches if what
                                 and any(name == what or name.endswith("." + what) or what.endswith("." + name) and "." in name
                                         for name, at in called if at > line)), None)
    if replaced_then_called:
        at = replaced_then_called[0]
        hits.append(_hit("over-mocked", path, at, label, "python",
                         "replaces %s, then calls it: %s" % (replaced_then_called[1], lines[at - 1] if 0 < at <= len(lines) else "")))
    elif len(patches) >= MOCK_LIMIT:
        hits.append(_hit("over-mocked", path, patches[0][0], label, "python", "%d mocks or patches" % len(patches)))
    sleeps = [c for c in calls if _call_name(c) in ("time.sleep", "sleep", "asyncio.sleep")]
    if sleeps:
        hits.append(_hit("sleep-based", path, sleeps[0].lineno, label, "python", _source(lines, sleeps[0])))
    swallowing = _swallowing_handler(fn)
    if swallowing is not None:
        hits.append(_hit("catch-all", path, swallowing.lineno, label, "python", _source(lines, swallowing)))
    snap = [c for c in checks if _is_snapshot_call(_call_name(c))]
    snap_asserts = [a for a in asserts if "snapshot" in ast.dump(a).lower()]
    if (snap or snap_asserts) and len(snap) + len(snap_asserts) == len(checks) + len(asserts) and not with_raises:
        first = (snap + snap_asserts)[0]
        hits.append(_hit("snapshot-only", path, fn.lineno, label, "python", _source(lines, first)))
    return hits


def _swallowing_handler(fn: ast.AST) -> Optional[ast.ExceptHandler]:
    """The first broad ``except`` in the test that does not raise again."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                broad = handler.type is None or _dotted(handler.type) in ("Exception", "BaseException", "AssertionError")
                if broad and not any(isinstance(n, ast.Raise) for n in ast.walk(handler)):
                    return handler
    return None


def scan_python(path: str, text: str) -> Tuple[int, List[Hit]]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return 0, []
    lines = text.splitlines()
    hits: List[Hit] = []
    count = 0
    tests: List[Tuple[str, ast.AST]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            tests.append((node.name, node))
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                    tests.append(("%s::%s" % (node.name, item.name), item))
    for label, fn in tests:
        count += 1
        hits += _py_test(fn, label, path, lines)
    return count, hits


# --------------------------------------------------------------------------- JavaScript / TypeScript and JVM (text)

def _block(text: str, start: int) -> Tuple[int, int]:
    """The ``{...}`` block that opens at or after ``start``: (index of ``{``, index after the matching ``}``)."""
    open_at = text.find("{", start)
    if open_at < 0:
        return start, start
    depth, i, quote = 0, open_at, None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return open_at, i + 1
        i += 1
    return open_at, len(text)


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _args(text: str, open_paren: int) -> List[str]:
    """The top-level comma-separated arguments of the call whose ``(`` is at ``open_paren``."""
    depth, i, start, out, quote = 0, open_paren, open_paren + 1, [], None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                out.append(text[start:i])
                return [a.strip() for a in out]
        elif ch == "," and depth == 1:
            out.append(text[start:i])
            start = i + 1
        i += 1
    return [a.strip() for a in out]


JS_TEST_START = re.compile(r"(?m)^[ \t]*(?:it|test)(?:\.only)?\s*\(\s*([\"'`])(.*?)\1")
JS_CHECK = re.compile(r"\bexpect\s*\(|\bassert(?:\.\w+)?\s*\(|\bshould\b|\bverify\w*\s*\(")
JS_SNAPSHOT = re.compile(r"\.toMatch(?:Inline)?Snapshot\s*\(|\.toMatchFileSnapshot\s*\(")
JS_SLEEP = re.compile(r"setTimeout\s*\([^,]+,\s*\d+|\bsleep\s*\(\s*\d+|waitForTimeout\s*\(\s*\d+")
JS_MOCK = re.compile(r"\b(?:jest|vi)\.(?:mock|spyOn|fn|doMock)\s*\(|\bsinon\.(?:stub|mock|spy)\s*\(")
JS_CATCH = re.compile(r"catch\s*(?:\(\s*\w*\s*\))?\s*\{\s*\}")


def scan_js(path: str, text: str) -> Tuple[int, List[Hit]]:
    hits: List[Hit] = []
    count = 0
    for match in JS_TEST_START.finditer(text):
        count += 1
        name, line = match.group(2), _line_of(text, match.start())
        open_at, end = _block(text, match.end())
        body = text[open_at:end]
        checks = JS_CHECK.findall(body)
        snapshots = JS_SNAPSHOT.findall(body)
        if not checks:
            hits.append(_hit("assertion-free", path, line, name, "javascript", match.group(0)))
        for exp in re.finditer(r"\bexpect\s*\(", body):
            paren = exp.end() - 1
            args, close = _args(body, paren), _matching(body, paren)
            tail = re.match(r"\s*\.(?:toBe|toEqual|toStrictEqual)\s*\(", body[close:])
            if args and tail:
                other = _args(body, close + tail.end() - 1)
                if other and other[0] == args[0]:
                    hits.append(_hit("tautological", path, line + body.count("\n", 0, exp.start()), name, "javascript",
                                     body[exp.start():_matching(body, close + tail.end() - 1)]))
                    break
        sleep = JS_SLEEP.search(body)
        if sleep:
            hits.append(_hit("sleep-based", path, line + body.count("\n", 0, sleep.start()), name, "javascript", sleep.group(0)))
        catch = JS_CATCH.search(body)
        if catch:
            hits.append(_hit("catch-all", path, line + body.count("\n", 0, catch.start()), name, "javascript", catch.group(0)))
        if len(JS_MOCK.findall(body)) >= MOCK_LIMIT:
            hits.append(_hit("over-mocked", path, line, name, "javascript", "%d mocks or spies" % len(JS_MOCK.findall(body))))
        if snapshots and len(snapshots) == len(re.findall(r"\bexpect\s*\(", body)) and len(checks) == len(snapshots):
            hits.append(_hit("snapshot-only", path, line, name, "javascript", snapshots[0]))
    return count, hits


def _matching(text: str, open_paren: int) -> int:
    """The index after the ``)`` that closes the ``(`` at ``open_paren``."""
    depth, i, quote = 0, open_paren, None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


JVM_TEST_START = re.compile(r"@Test\b[^\n]*\n(?:\s*@[\w.]+(?:\([^)]*\))?\s*\n)*\s*(?:public\s+|protected\s+|private\s+)?"
                            r"(?:fun\s+|void\s+)`?([\w ]+?)`?\s*\(")
JVM_CHECK = re.compile(r"\b(?:assert\w*|verify\w*|fail|assertThat|expect\w*)\s*\(")
JVM_SLEEP = re.compile(r"\bThread\.sleep\s*\(|\bTimeUnit\.\w+\.sleep\s*\(|\bdelay\s*\(\s*\d+")
JVM_MOCK = re.compile(r"\bmock\s*\(|\bspy\s*\(|\bmockk\s*[(<]|\bwhen\s*\(|@Mock\b")
JVM_CATCH = re.compile(r"catch\s*\(\s*(?:final\s+)?(?:Exception|Throwable|RuntimeException|AssertionError|Error)\s+\w+\s*\)\s*\{\s*\}")


def scan_jvm(path: str, text: str) -> Tuple[int, List[Hit]]:
    hits: List[Hit] = []
    count = 0
    for match in JVM_TEST_START.finditer(text):
        count += 1
        name = match.group(1).strip()
        line = _line_of(text, match.start(1))
        open_at, end = _block(text, match.end())
        body = text[open_at:end]
        if not JVM_CHECK.search(body):
            hits.append(_hit("assertion-free", path, line, name, "java", name))
        for check in re.finditer(r"\b(assertEquals|assertSame|assertTrue)\s*\(", body):
            args = _args(body, check.end() - 1)
            if check.group(1) == "assertTrue" and args and args[0] == "true" or len(args) >= 2 and args[0] == args[1]:
                hits.append(_hit("tautological", path, line + body.count("\n", 0, check.start()), name, "java",
                                 body[check.start():_matching(body, check.end() - 1)]))
                break
        sleep = JVM_SLEEP.search(body)
        if sleep:
            hits.append(_hit("sleep-based", path, line + body.count("\n", 0, sleep.start()), name, "java", sleep.group(0)))
        catch = JVM_CATCH.search(body)
        if catch:
            hits.append(_hit("catch-all", path, line + body.count("\n", 0, catch.start()), name, "java", catch.group(0)))
        if len(JVM_MOCK.findall(body)) >= MOCK_LIMIT:
            hits.append(_hit("over-mocked", path, line, name, "java", "%d mocks or stubs" % len(JVM_MOCK.findall(body))))
    return count, hits


# --------------------------------------------------------------------------- walk

def test_files(paths: Iterable[str]) -> List[Tuple[str, str]]:
    """``(path as shown, language)`` for every test file under ``paths``, sorted."""
    found = set()
    for given in paths:
        p = Path(given)
        candidates = [p] if p.is_file() else []
        if p.is_dir():
            for here, dirs, names in os.walk(str(p)):
                dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".venv"))
                candidates += [Path(here) / n for n in names]
        for c in candidates:
            name = c.name
            language = "python" if PY_TEST.match(name) else "javascript" if JS_TEST.match(name) else "java" if JVM_TEST.match(name) else None
            if language:
                shown = os.path.normpath(str(c)).replace(os.sep, "/")
                found.add((shown[2:] if shown.startswith("./") else shown, language))
    return sorted(found)


def scan(paths: Iterable[str]) -> Dict[str, Any]:
    hits: List[Hit] = []
    tests = 0
    files = test_files(paths)
    for path, language in files:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        count, found = {"python": scan_python, "javascript": scan_js, "java": scan_jvm}[language](path, text)
        tests += count
        hits += found
    hits.sort(key=lambda h: (h["path"], h["line"], h["rule"], h["test"]))
    by_rule = {rule: sum(1 for h in hits if h["rule"] == rule) for rule in RULES}
    return {"files_scanned": len(files), "tests": tests, "smells": hits, "by_rule": by_rule,
            "note": "each hit is a lead: read the test, then record it as a finding or clear it with a reason"}


def main(argv: List[str]) -> int:
    paths = argv or ["."]
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        print(json.dumps({"ok": False, "error": {"code": "no_such_path", "paths": missing}}))
        return 2
    print(json.dumps(scan(paths), indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
