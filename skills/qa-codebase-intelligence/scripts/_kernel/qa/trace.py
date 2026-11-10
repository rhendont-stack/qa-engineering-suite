"""Typed trace links, the read-only scan that maps tests to test cases, and the traceability gap matrix (§9.2).

Links are ``trace.linked`` events ``{source, relation, target, via}`` and nothing else: no record holds a list of what it
is linked to. ``link`` records one, after checking that its relation joins the kinds of record it may join and that both
ends exist (the state's reducer does not judge ends). The pairs recorded here:

    verified_by   AC  -> TC          mitigated_by  RSK -> TC
    automated_by  TC  -> test id     executed_in   TC  -> EXE
    red_check     TC  -> EXE         violates      FND -> AC | REQ

``addressed_by`` (FND -> FIX) and ``validated_by`` (FIX -> RVL) belong to the fix workflow and are not accepted here.

A test id is the one results record, ``classname::name`` (``tests.test_discount::test_cap``, or
``tests.test_discount.TestCap::test_cap`` for a class). ``normalise_test_id`` also takes the pytest path form
(``tests/test_discount.py::test_cap``) and rewrites it to that form.

A ``red_check`` is the proof that a test can fail (§7.6): the execution must hold a ``failed`` result with
``failure_kind: assertion`` for a test the test case is ``automated_by``. An error, such as a missing import, is refused.

``scan`` maps tests to test cases from ``TC-nnn`` tags (in the names of recorded results, and in test source under a
root) and from ``.qa/trace/test-map.json``. It reads text and parses Python with ``ast``; it never imports or runs a
project file, never follows a symlink, and never leaves the root. ``matrix`` is a pure function of the state.
"""
from __future__ import annotations

import ast
import json
import os
import re
import stat
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from . import gitinfo, ledger, records, schema, state
from .errors import QaError
from .workspace import Workspace, write_atomic

# QaError codes raised here: invalid_link, unknown_record, red_check_refused, trace_map_invalid, scan_root_invalid, target_required,
# target_invalid, target_unknown, target_ambiguous, target_unavailable;
# plus ledger.append's.

VIA = ("manual", "tag", "map")
# relation: (source prefixes, target prefixes); a target prefix "" means a test id.
RELATIONS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "verified_by": (("AC-",), ("TC-",)),
    "mitigated_by": (("RSK-",), ("TC-",)),
    "automated_by": (("TC-",), ("",)),
    "executed_in": (("TC-",), ("EXE-",)),
    "red_check": (("TC-",), ("EXE-",)),
    "violates": (("FND-",), ("AC-", "REQ-")),
}
_STATE_KEY = {"AC-": "criteria", "RSK-": "risks", "TC-": "testcases", "EXE-": "executions", "FND-": "findings", "REQ-": "requirements"}

# The kinds of gap the matrix reports; the fix workflow adds its own.
GAP_KINDS = ("req_without_ac", "ac_without_tc", "tc_not_automated", "tc_never_executed", "tc_last_blocked_or_inconclusive",
             "req_only_characterization_or_assumed", "tc_unproven", "fnd_without_fix")
_OPEN_FINDINGS = ("open", "reopened", "fix_submitted")
_NEEDS_FIX = ("open", "reopened")

_TAG = re.compile(r"(?<![A-Za-z0-9])TC[-_]?([0-9]{3,})(?![0-9])")
_FULL_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_MAX_FILE = 1024 * 1024
_MAX_FILES = 20000
_PYTHON_TEST_FILE = re.compile(r"^(test_.*|.*_test)\.py$")
_OTHER_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".rb", ".cs")
_SKIPPED_DIRS = ("node_modules", "__pycache__")
_MAP_PATH = ("trace", "test-map.json")
_MATRIX_PATH = ("trace", "matrix.json")


def _tag_id(digits: str) -> str:
    return ledger.format_human_id("TC", int(digits))


def tags_in(text: str) -> List[str]:
    """The test case IDs a text names, as ``TC-nnn``, in order of appearance and without repeats."""
    found: List[str] = []
    for match in _TAG.finditer(text):
        tag = _tag_id(match.group(1))
        if tag not in found:
            found.append(tag)
    return found


def _order(identifier: str) -> Tuple[int, int, str]:
    try:
        return records._order(identifier)
    except (ValueError, IndexError):
        return (0, 0, identifier)


# --------------------------------------------------------------------------- test ids

def normalise_test_id(text: object) -> Optional[str]:
    """The ``classname::name`` form of a test id, or ``None`` when the text cannot be one.

    ``tests.test_x::test_y`` is already canonical. A path form (``tests/test_x.py::TestA::test_y``) becomes
    ``tests.test_x.TestA::test_y``. A path that is absolute or climbs out (``..``) is not mapped.
    """
    if not isinstance(text, str) or "::" not in text:
        return None
    head, rest = text.split("::", 1)
    head = head.strip()
    if not head or not rest.strip():
        return None
    if "/" not in head and not head.endswith(".py") and "\\" not in head:
        return text if head and rest else None
    if "\\" in head or head.startswith("/") or ":" in head:
        return None
    parts = [p for p in head.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    name_part, bracket, params = rest.partition("[")
    pieces = name_part.split("::")
    if not all(pieces):
        return None
    classes, name = pieces[:-1], pieces[-1]
    return "%s::%s%s%s" % (".".join(parts + classes), name, bracket, params)


# --------------------------------------------------------------------------- links

def _exists(st: dict, identifier: str) -> bool:
    for prefix, key in _STATE_KEY.items():
        if identifier.startswith(prefix):
            return identifier in st[key]
    return False


def _automated(st: dict, tc: str) -> List[str]:
    return [l["target"] for l in st["links"] if l["source"] == tc and l["relation"] == "automated_by"]


def _has_assertion_failure(st: dict, tc: str, execution: str) -> bool:
    tests = set(_automated(st, tc))
    return any(r["execution"] == execution and r["status"] == "failed" and r.get("failure_kind") == "assertion"
               and r["test_id"] in tests for r in st["results"].values())


def _checked(st: dict, source: str, relation: str, target: str, via: str) -> Tuple[str, str]:
    """``(source, target)`` as they will be recorded, or a QaError saying why the link is refused."""
    if relation not in RELATIONS:
        raise QaError("invalid_link", "no such relation is recorded here", relation=relation, relations=sorted(RELATIONS))
    if via not in VIA:
        raise QaError("invalid_link", "via is manual, tag or map", via=via)
    sources, targets = RELATIONS[relation]
    if not isinstance(source, str) or not any(source.startswith(p) for p in sources):
        raise QaError("invalid_link", "%s starts from %s" % (relation, " or ".join(p + "..." for p in sources)), source=source)
    if relation == "automated_by":
        canonical = normalise_test_id(target)
        if canonical is None:
            raise QaError("invalid_link", "automated_by ends at a test id in the form classname::name", target=target)
        target = canonical
    elif not isinstance(target, str) or not any(target.startswith(p) for p in targets):
        raise QaError("invalid_link", "%s ends at %s" % (relation, " or ".join(p + "..." for p in targets)), target=target)
    for end in (source,) + (() if relation == "automated_by" else (target,)):
        if not _exists(st, end):
            raise QaError("unknown_record", "no such record; record it first", id=end)
    if relation == "red_check":
        if not _automated(st, source):
            raise QaError("red_check_refused", "the test case is not automated_by any test, so no test of it can be seen failing", id=source)
        if not _has_assertion_failure(st, source, target):
            raise QaError("red_check_refused",
                          "the execution holds no failed assertion for a test of this test case (an error is not a red check)",
                          id=source, execution=target)
    return source, target


def _linked(st: dict, source: str, relation: str, target: str) -> Optional[dict]:
    for link in st["links"]:
        if (link["source"], link["relation"], link["target"]) == (source, relation, target):
            return link
    return None


def link(ws: Workspace, actor: ledger.Actor, source: str, relation: str, target: str, *, via: str = "manual") -> dict:
    """Record one link. Returns ``{source, relation, target, event, created}``; a link already recorded is not recorded again."""
    with ws.lock():
        st = state.load(ws)
        source, target = _checked(st, source, relation, target, via)
        held = _linked(st, source, relation, target)
        if held is not None:
            return {"source": source, "relation": relation, "target": target, "event": held["event"], "created": False}
        event = ledger.append(ws, actor, "trace.linked", {"source": source, "relation": relation, "target": target, "via": via})
    return {"source": source, "relation": relation, "target": target, "event": event["id"], "created": True}


# --------------------------------------------------------------------------- scan

def _read_regular(path: str) -> Optional[bytes]:
    """The file's bytes, or None when it is a symlink, not a regular file, too large or unreadable."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE:
            return None
        with open(fd, "rb", closefd=False) as handle:
            return handle.read(_MAX_FILE + 1)
    except OSError:
        return None
    finally:
        os.close(fd)


def _walk(root: Path) -> Tuple[List[str], int, bool]:
    """Relative POSIX paths of the files worth reading under ``root``, how many symlinks were passed over, and whether the file cap cut the walk short."""
    files: List[str] = []
    symlinks = 0
    base = str(root)
    for current, dirs, names in os.walk(base, followlinks=False):
        kept = []
        for name in sorted(dirs):
            full = os.path.join(current, name)
            if os.path.islink(full):
                symlinks += 1
            elif not name.startswith(".") and name not in _SKIPPED_DIRS:
                kept.append(name)
        dirs[:] = kept
        for name in sorted(names):
            full = os.path.join(current, name)
            wanted = _PYTHON_TEST_FILE.match(name) or (name.endswith(_OTHER_SUFFIXES) and re.search(r"test|spec", name.lower()))
            if not wanted:
                continue
            if os.path.islink(full):
                symlinks += 1
                continue
            files.append(Path(os.path.relpath(full, base)).as_posix())
            if len(files) >= _MAX_FILES:
                return files, symlinks, True
    return files, symlinks, False


def _marker_tags(decorators: List[ast.expr]) -> List[str]:
    found: List[str] = []
    for decorator in decorators:
        if not isinstance(decorator, ast.Call):
            continue
        func = decorator.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
        if name != "qa":
            continue
        for arg in decorator.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found += [t for t in tags_in(arg.value) if t not in found]
    return found


def _is_test_class(node: ast.ClassDef) -> bool:
    bases = [b.attr if isinstance(b, ast.Attribute) else b.id if isinstance(b, ast.Name) else "" for b in node.bases]
    return node.name.startswith("Test") or "TestCase" in bases


def _python_tests(module: str, tree: ast.Module) -> List[Tuple[str, List[str], int]]:
    """``(test id, tags, line)`` for each test pytest would collect from the module, found by reading the syntax tree."""
    found: List[Tuple[str, List[str], int]] = []

    def visit(body: List[ast.stmt], classname: str, inherited: List[str]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                tags = [t for t in tags_in(node.name) + _marker_tags(node.decorator_list) + inherited]
                found.append(("%s::%s" % (classname, node.name), list(dict.fromkeys(tags)), node.lineno))
            elif isinstance(node, ast.ClassDef) and _is_test_class(node):
                visit(node.body, classname + "." + node.name, inherited + _marker_tags(node.decorator_list))

    visit(tree.body, module, [])
    return found


def _scan_source(root: Path) -> Tuple[Dict[str, Set[str]], List[dict], int, int, bool]:
    """``(tags, unmapped, files_read, symlinks, truncated)``: test ids by the test case their source tags, and what could not be placed."""
    tags: Dict[str, Set[str]] = {}
    unmapped: List[dict] = []
    files, symlinks, truncated = _walk(root)
    read = 0
    for rel in files:
        raw = _read_regular(os.path.join(str(root), rel))
        if raw is None or len(raw) > _MAX_FILE:
            unmapped.append({"file": rel, "reason": "unreadable, not a regular file, or larger than 1 MiB"})
            continue
        read += 1
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            unmapped.append({"file": rel, "reason": "not UTF-8 text"})
            continue
        if rel.endswith(".py"):
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError, RecursionError, MemoryError):
                if tags_in(text):
                    unmapped.append({"file": rel, "reason": "the file does not parse, so its tests cannot be named"})
                continue
            module = ".".join(rel[:-3].split("/"))
            for test_id, found, _line in _python_tests(module, tree):
                for tag in found:
                    tags.setdefault(tag, set()).add(test_id)
        else:
            for number, line in enumerate(text.splitlines(), 1):
                for tag in tags_in(line):
                    unmapped.append({"file": rel, "line": number, "tag": tag,
                                     "reason": "the test id of this language is not derived; map it in .qa/trace/test-map.json"})
    return tags, unmapped, read, symlinks, truncated


def _read_map(ws: Workspace) -> Tuple[Dict[str, Set[str]], List[dict]]:
    path = ws.path(*_MAP_PATH)
    shown = ".qa/" + "/".join(_MAP_PATH)
    try:
        os.lstat(str(path))
    except FileNotFoundError:
        return {}, []
    except OSError as exc:
        raise QaError("trace_map_invalid", "the test map cannot be read", path=shown, error=str(exc))
    raw = _read_regular(str(path))
    if raw is None:
        raise QaError("trace_map_invalid", "the test map is a symlink, not a regular file, or too large to read", path=shown)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise QaError("trace_map_invalid", "the test map is not JSON", path=shown, error=str(exc))
    problems = schema.errors(data, schema.load_schema("test-map.schema.json"), base="test-map.schema.json")
    if problems:
        raise QaError("trace_map_invalid", "the test map does not fit its schema", path=shown, errors=problems)
    mapped: Dict[str, Set[str]] = {}
    unmapped: List[dict] = []
    for tc in sorted(data, key=_order):
        for entry in data[tc]:
            canonical = normalise_test_id(entry)
            if canonical is None:
                unmapped.append({"file": shown, "testcase": tc, "entry": entry, "reason": "not a test id (classname::name or path::name)"})
            else:
                mapped.setdefault(tc, set()).add(canonical)
    return mapped, unmapped


def _result_tags(st: dict) -> Dict[str, Set[str]]:
    tags: Dict[str, Set[str]] = {}
    for test_id in st["tests"]:
        for tag in tags_in(test_id.rsplit("::", 1)[-1]):
            tags.setdefault(tag, set()).add(test_id)
    return tags


def scan(ws: Workspace, actor: ledger.Actor, root: Path) -> dict:
    """Link test cases to the tests that implement them, from tags and the test map. Nothing under ``root`` is written.

    Returns ``{linked, already_linked, conflicts, unmapped, unknown_testcases, files_read, symlinks_skipped, truncated}``
    (``truncated``: the file cap was hit and some test files were not read). A test case
    whose tags and map both name tests, and not the same tests, is under ``conflicts`` and linked by neither.
    """
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise QaError("scan_root_invalid", "the scan root is not a directory, or is a symlink", path=str(root))
    root = root.resolve()
    with ws.lock():
        st = state.load(ws)
        tagged = _result_tags(st)
        source_tags, unmapped, files_read, symlinks, truncated = _scan_source(root)
        for tc, tests in source_tags.items():
            tagged.setdefault(tc, set()).update(tests)
        mapped, map_unmapped = _read_map(ws)
        unmapped += map_unmapped
        conflicts: List[dict] = []
        unknown: List[str] = []
        linked: List[dict] = []
        already = 0
        for tc in sorted(set(tagged) | set(mapped), key=_order):
            if tc not in st["testcases"]:
                unknown.append(tc)
                continue
            by_tag, by_map = tagged.get(tc, set()), mapped.get(tc, set())
            if by_tag and by_map and by_tag != by_map:
                conflicts.append({"testcase": tc, "tags": sorted(by_tag), "map": sorted(by_map)})
                continue
            via = "tag" if by_tag else "map"
            for test_id in sorted(by_tag or by_map):
                if _linked(st, tc, "automated_by", test_id) is not None:
                    already += 1
                    continue
                event = ledger.append(ws, actor, "trace.linked", {"source": tc, "relation": "automated_by", "target": test_id, "via": via})
                linked.append({"source": tc, "target": test_id, "via": via, "event": event["id"]})
    return {"linked": linked, "already_linked": already, "conflicts": conflicts, "unmapped": unmapped,
            "unknown_testcases": unknown, "files_read": files_read, "symlinks_skipped": symlinks, "truncated": truncated}


# --------------------------------------------------------------------------- matrix

def resolve_target(ws: Workspace, st: dict, target: Optional[str]) -> str:
    """The full commit the matrix is about. An abbreviation (7 or more hex digits) is resolved once against the commits the
    state records: exactly one must match. No target means the workspace's current HEAD; commits are never combined."""
    if target is None:
        facts, limitation = gitinfo.git_info(ws.root)
        commit = facts.get("commit") if facts else None
        if not isinstance(commit, str) or not _FULL_COMMIT.match(commit):
            raise QaError("target_unavailable", "the current commit cannot be read; name one with --target", reason=limitation)
        return commit
    target = target.strip().lower()
    if _FULL_COMMIT.match(target):
        return target
    if not re.match(r"^[0-9a-f]{7,63}$", target):
        raise QaError("target_invalid", "the target is a full commit id, or an abbreviation of at least 7 hex digits", target=target)
    recorded = sorted({(e.get("git") or {}).get("commit") for e in st["executions"].values()} - {None})
    found = [c for c in recorded if _FULL_COMMIT.match(c) and c.startswith(target)]
    if len(found) == 1:
        return found[0]
    raise QaError("target_unknown" if not found else "target_ambiguous",
                  "the abbreviation names no recorded commit" if not found else "the abbreviation names several recorded commits",
                  target=target, candidates=found)


def _latest(st: dict, test_id: str, target: str) -> Optional[Tuple[dict, str]]:
    """The state's entry for the test's latest result on ``target`` (the full commit, matched exactly)."""
    best: Optional[Tuple[Tuple[str, str], dict]] = None
    for entry in st["tests"].get(test_id, {}).values():
        execution = st["executions"].get(entry["execution"], {})
        if (execution.get("git") or {}).get("commit") != target:
            continue
        key = (execution.get("started_at", ""), entry["execution"])
        if best is None or key > best[0]:
            best = (key, entry)
    return (best[1], target) if best else None


def _test_case_status(tests: List[dict]) -> Optional[str]:
    statuses = [t["status"] for t in tests]
    if not any(s is not None for s in statuses):
        return None
    if "failed" in statuses:
        return "failed"
    for blocked in ("blocked", "inconclusive"):
        if blocked in statuses:
            return blocked
    return "passed" if all(s == "passed" for s in statuses) else "not_run"


def _by_source(st: dict, relation: str) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for item in st["links"]:
        if item["relation"] == relation:
            out.setdefault(item["source"], []).append(item["target"])
    return out


def matrix(st: dict, target_commit: str) -> dict:
    """The traceability matrix over ``st`` for one full commit: no clock, no file, no write; the same state gives the same matrix."""
    if not isinstance(target_commit, str) or not _FULL_COMMIT.match(target_commit):
        raise QaError("target_required", "the matrix is about one full commit id; resolve_target gives it", target=target_commit)
    verified = _by_source(st, "verified_by")
    automated = _by_source(st, "automated_by")
    red = _by_source(st, "red_check")
    violates = _by_source(st, "violates")
    fixes = _by_source(st, "addressed_by")
    tcs = st["testcases"]
    gaps: List[dict] = []

    def gap(kind: str, identifier: str) -> dict:
        item = {"kind": kind, "id": identifier}
        gaps.append(item)
        return item

    cases: Dict[str, dict] = {}
    for tc in sorted(tcs, key=_order):
        tests = []
        for test_id in sorted(set(automated.get(tc, []))):
            found = _latest(st, test_id, target_commit)
            entry = found[0] if found else None
            tests.append({"test_id": test_id, "commit": target_commit if entry else None,
                          "status": entry["status"] if entry else None, "flaky": bool(entry and entry["flaky"]),
                          "result": entry["result"] if entry else None, "execution": entry["execution"] if entry else None})
        cases[tc] = {"id": tc, "oracle": tcs[tc]["oracle"], "status": _test_case_status(tests),
                     "red_check": bool(red.get(tc)), "tests": tests}
        if not tests:
            gap("tc_not_automated", tc)
        elif cases[tc]["status"] in (None, "not_run"):  # a skipped or missing test did not execute on the target
            gap("tc_never_executed", tc)
        elif cases[tc]["status"] in ("blocked", "inconclusive"):
            gap("tc_last_blocked_or_inconclusive", tc)
        if tests and not cases[tc]["red_check"]:
            gap("tc_unproven", tc)

    requirement_gaps: Dict[str, List[dict]] = {}
    rows: List[dict] = []
    criteria_of: Dict[str, List[str]] = {}
    for ac in sorted(st["criteria"], key=_order):
        criteria_of.setdefault(st["criteria"][ac]["requirement"], []).append(ac)
    for req in sorted(st["requirements"], key=_order):
        record = st["requirements"][req]
        mine: List[dict] = []
        criteria = []
        req_cases: List[str] = []
        for ac in criteria_of.get(req, []):
            verifying = [t for t in sorted(set(verified.get(ac, [])), key=_order) if t in tcs]
            criteria.append({"id": ac, "testcases": verifying})
            req_cases += [t for t in verifying if t not in req_cases]
            if not verifying:
                mine.append(gap("ac_without_tc", ac))
        if not criteria:
            mine.append(gap("req_without_ac", req))
        shaky = record["provenance"] == "assumed" and not record.get("confirmed")
        if shaky or (req_cases and all(tcs[t]["oracle"] == "characterization" for t in req_cases)):
            mine.append(gap("req_only_characterization_or_assumed", req))
        proven = bool(criteria) and all(
            any(tcs[t]["oracle"] != "characterization" and cases[t]["status"] == "passed" for t in c["testcases"]) for c in criteria)
        ends = {req} | set(criteria_of.get(req, []))
        findings = sorted({f for f, targets in violates.items() for t in targets if t in ends
                           and f in st["findings"] and st["findings"][f]["state"] in _OPEN_FINDINGS}, key=_order)
        rows.append({"id": req, "criteria": criteria, "testcases": req_cases, "proven": proven, "findings": findings, "gaps": mine})
        requirement_gaps[req] = mine
    for fnd in sorted(st["findings"], key=_order):
        if st["findings"][fnd]["state"] in _NEEDS_FIX and not fixes.get(fnd):
            gap("fnd_without_fix", fnd)

    order = {kind: n for n, kind in enumerate(GAP_KINDS)}
    gaps.sort(key=lambda g: (order[g["kind"]], _order(g["id"])))
    counts = {kind: sum(1 for g in gaps if g["kind"] == kind) for kind in GAP_KINDS}
    for row in rows:
        row["gaps"].sort(key=lambda g: (order[g["kind"]], _order(g["id"])))
    return {"target": target_commit, "requirements": rows, "testcases": [cases[t] for t in cases], "gaps": gaps, "counts": counts}


def write_matrix(ws: Workspace, st: dict, target_commit: str) -> Tuple[dict, Path]:
    """Compute the matrix, check it against its schema, and write ``.qa/trace/matrix.json`` (the same state, the same bytes)."""
    result = matrix(st, target_commit)
    problems = schema.errors(result, schema.load_schema("trace-matrix.schema.json"), base="trace-matrix.schema.json")
    if problems:
        raise QaError("matrix_invalid", "the matrix does not fit its schema", errors=problems)
    path = ws.path(*_MATRIX_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, (json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return result, path
