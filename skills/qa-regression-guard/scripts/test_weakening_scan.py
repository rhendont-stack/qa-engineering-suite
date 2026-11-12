"""Scan a change for the ways a test suite is made to stop catching failures.

Usage (from the project root):
    python3 test_weakening_scan.py --base <commit> --head <commit>
    python3 test_weakening_scan.py --diff <file holding a unified diff>

Prints one JSON object: ``{"ok": true, "base", "head", "hits": [...], "counts": {rule: n}}``. Each hit is
``{rule, language, path, line, side, removed, added, hunk, detail}``: the rule, the file, the line (``side`` says on which
commit: ``head`` for a line the change adds, ``base`` for one it removes), the removed and added lines it rests on, and the
header of the hunk that holds them, so a finding can cite the hunk as evidence.

The rules (``references/weakening-patterns.md`` describes each, per language):

    loosened-assertion        an exact assertion replaced by a weaker one on the same value
    assertion-removed         a hunk removes more assertions than it adds, outside a removed test
    deleted-test              a test definition removed and not put back under the same or a new name
    skip-added                a skip, xfail, ``only``, ``@Disabled`` or ``t.Skip`` added
    tolerance-widened         an approximate comparison's tolerance made looser
    snapshot-rewritten        a stored snapshot changed or removed
    coverage-threshold-lowered  a coverage minimum lowered
    ci-failure-ignored        a CI step allowed to fail
    tests-excluded            the runner told to leave tests out
    runner-hook               a runner hook or reporter that can change what a run reports
    forced-exit               a test or test-support file that ends the process with success
    equality-overridden       an equality method that always answers true
    test-detection-in-product product code that checks whether it runs under a test
    special-cased-input       a product condition on a literal value that only an added test uses

A hit is a lead for a finding, never a verdict: whether the change is legitimate is the agent's judgement, recorded with the
hunk as evidence. The scan reads only; it runs nothing from the repository and writes nothing. Standard library only.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import _change  # noqa: E402
from _change import ChangeError, kind_of, language_of, parse_diff  # noqa: E402,F401

NUM = r"[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?"
COMMENT = re.compile(r"^\s*(?:#|//|/\*|\*|--\s)")

# --------------------------------------------------------------------------- assertions

ASSERTION = re.compile(r"^\s*(?:self\.)?assert\w*\b|\bexpect\s*\(|\bassert(?:_that|That|\.\w+)?\s*\(|\b(?:require|assert)\.\w+\s*\(|"
                       r"\bt\.(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\s*\(|\.should\b|\bpytest\.raises\s*\(")
WEAK_CALL = re.compile(r"\b(?:self\.)?(?:assertTrue|assertFalse|assertIsNotNone|assertNotNull|assertNotEmpty|assert_?[Tt]ruthy|"
                       r"assert\.(?:ok|True|False|NotNil|NotEmpty|NoError|Truthy))\s*\(|"
                       r"\.(?:toBeTruthy|toBeFalsy|toBeDefined|toBeUndefined)\s*\(\s*\)|\.not\.(?:toBeNull|toBeUndefined|toBeNaN)\s*\(\s*\)|"
                       r"\.toHaveBeenCalled\s*\(\s*\)|\.(?:isNotNull|isTrue|isFalse|isNotEmpty|isNotBlank)\s*\(\s*\)|\bassert\s*\(")
BARE_ASSERT = re.compile(r"^\s*assert\s+(.+?)\s*$")
COMPARISON = re.compile(r"==|!=|<=|>=|<|>|\bin\b|\bis\b|\bisinstance\s*\(")


def _args(text: str, open_at: int) -> List[str]:
    """The top-level arguments of the call whose ``(`` is at ``open_at``."""
    depth, parts, current, quote = 0, [], [], ""
    for ch in text[open_at:]:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"`":
            quote = ch
            current.append(ch)
            continue
        if ch in "([{":
            depth += 1
            if depth == 1:
                continue
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                parts.append("".join(current).strip())
                return [p for p in parts if p]
        elif ch == "," and depth == 1:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    return []


def is_assertion(text: str) -> bool:
    return not COMMENT.match(text) and bool(ASSERTION.search(text))


def is_weak(text: str) -> bool:
    if WEAK_CALL.search(text):
        return True
    bare = BARE_ASSERT.match(text)
    if bare:
        expr = re.split(r",\s*(?:f?[\"'])", bare.group(1))[0]
        return not COMPARISON.search(expr)
    return False


def subject(text: str) -> str:
    """What a weak assertion checks: the argument of ``expect(...)``/``assertThat(...)``, of the assertion call, or a bare
    ``assert``'s expression; whitespace removed."""
    for pattern in (r"\bexpect\s*\(", r"\bassert_?[Tt]hat\s*\("):
        match = re.search(pattern, text)
        if match:
            args = _args(text, match.end() - 1)
            return re.sub(r"\s+", "", args[0]) if args else ""
    match = WEAK_CALL.search(text)
    if match and text[match.end() - 1:match.end()] == "(":
        args = _args(text, match.end() - 1)
        if match.group(0).startswith("assert.") and len(args) > 1:
            args = args[1:]  # Go's testify: assert.True(t, value)
        return re.sub(r"\s+", "", args[0]) if args else ""
    bare = BARE_ASSERT.match(text)
    if bare:
        return re.sub(r"\s+", "", re.split(r",\s*(?:f?[\"'])", bare.group(1))[0])
    return ""


# --------------------------------------------------------------------------- test definitions

PY_DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+(test\w*)\s*\(")
JS_DEF = re.compile(r"^(\s*)(?:it|test|specify)(?:\.(?:only|skip|concurrent|each\([^)]*\)))?\s*\(\s*(['\"`])(.*?)\2")
GO_DEF = re.compile(r"^(\s*)func\s+(Test\w+)\s*\(")
JVM_DEF = re.compile(r"^(\s*)(?:(?:public|protected|private|internal|static|suspend)\s+)*(?:void\s+|fun\s+)(`[^`]+`|\w+)\s*\(")
RB_DEF = re.compile(r"^(\s*)it\s+(['\"])(.*?)\2")


def test_def(text: str, language: Optional[str], previous: str = "") -> Optional[Tuple[int, str]]:
    """``(indent, name)`` when ``text`` opens a test; ``previous`` is the line before it on the same side."""
    if language == "python":
        match = PY_DEF.match(text)
        return (len(match.group(1)), match.group(2)) if match else None
    if language in ("javascript", "typescript"):
        match = JS_DEF.match(text)
        return (len(match.group(1)), match.group(3)) if match else None
    if language == "go":
        match = GO_DEF.match(text)
        return (len(match.group(1)), match.group(2)) if match else None
    if language in ("java", "kotlin"):
        match = JVM_DEF.match(text)
        if match and (match.group(2).startswith("test") or re.search(r"@(?:Test|ParameterizedTest|RepeatedTest)\b", previous + " " + text)):
            return len(match.group(1)), match.group(2)
        return None
    if language == "ruby":
        match = RB_DEF.match(text)
        return (len(match.group(1)), match.group(3)) if match else None
    return None


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip())


def owned_by_defs(lines: List[Dict[str, Any]], kind: str, language: Optional[str]) -> Tuple[List[Tuple[Dict[str, Any], str]], Set[int]]:
    """The test definitions on the ``kind`` side of a hunk, and the indexes of the lines that belong to one of them (the line
    that opens it and the more indented lines that follow it on the same side)."""
    defs: List[Tuple[Dict[str, Any], str]] = []
    owned: Set[int] = set()
    current: Optional[int] = None
    previous = ""
    for index, line in enumerate(lines):
        if line["kind"] != kind:
            if line["kind"] == " ":
                current = None
            continue
        found = test_def(line["text"], language, previous)
        previous = line["text"]
        if found is not None:
            defs.append((line, found[1]))
            owned.add(index)
            current = found[0]
            continue
        if current is not None and (not line["text"].strip() or _indent(line["text"]) > current):
            owned.add(index)
        elif line["text"].strip():
            current = None
    return defs, owned


# --------------------------------------------------------------------------- line patterns

SKIP = {
    "python": r"@pytest\.mark\.(?:skip|skipif|xfail)\b|\bpytest\.(?:skip|xfail)\s*\(|@unittest\.(?:skip|skipIf|skipUnless|expectedFailure)\b|"
              r"\.skipTest\s*\(|raise\s+(?:unittest\.)?SkipTest\b",
    "javascript": r"\b(?:it|test|describe|context|suite)\.(?:skip|only|todo|fixme)\s*\(|\b(?:xit|xtest|xdescribe|xcontext|fit|fdescribe)\s*\(",
    "java": r"@(?:Disabled|Ignore)\b|\bassume(?:True|False|That)\s*\(",
    "go": r"\bt\.Skip(?:f|Now)?\s*\(",
    "ruby": r"^\s*(?:skip|pending|xit|xdescribe)\b",
    "csharp": r"\[\s*(?:Ignore|Fact\s*\(\s*Skip|Theory\s*\(\s*Skip)",
    "php": r"markTest(?:Skipped|Incomplete)\s*\(",
    "rust": r"#\[ignore\b",
}
SKIP["typescript"] = SKIP["javascript"]
SKIP["kotlin"] = SKIP["java"]

TOLERANCE = re.compile(r"\b(abs|rel|atol|rtol|delta|epsilon|tolerance|tol|places|decimal|precision)\s*[=:]\s*(" + NUM + ")|"
                       r"\.toBeCloseTo\s*\([^,()]+(?:\([^()]*\))?[^,()]*,\s*(" + NUM + r")\s*\)|\b(within|offset)\s*\(\s*(" + NUM + ")")
NARROWER_WHEN_LARGER = {"places", "decimal", "precision", "digits"}

COVERAGE = [("fail_under", re.compile(r"\bfail_under\s*[=:]\s*(" + NUM + ")")),
            ("cov-fail-under", re.compile(r"--cov-fail-under[= ](" + NUM + ")")),
            ("minimum_coverage", re.compile(r"\bminimum_coverage\b\s*[=:(]?\s*(" + NUM + ")")),
            ("minimum", re.compile(r"<minimum>\s*(" + NUM + r")\s*</minimum>|\bminimum\s*=\s*(" + NUM + ")")),
            ("lines", re.compile(r"\blines\s*[=:]\s*(" + NUM + ")")), ("branches", re.compile(r"\bbranches\s*[=:]\s*(" + NUM + ")")),
            ("functions", re.compile(r"\bfunctions\s*[=:]\s*(" + NUM + ")")),
            ("statements", re.compile(r"\bstatements\s*[=:]\s*(" + NUM + ")")),
            ("target", re.compile(r"\b(?:target|threshold)\s*:\s*(" + NUM + ")%?"))]
COVERAGE_CONTEXT = re.compile(r"coverage|fail_under|cov-fail-under|jacoco|codecov|nyc|c8|istanbul", re.I)

CI_IGNORED = re.compile(r"\bcontinue-on-error\s*:\s*true\b|\ballow_failure\s*:\s*true\b|\bsoft_fail\s*:\s*true\b|"
                        r"\|\|\s*(?:true|:|exit\s+0)\b|\|\|\s*:\s*$|^\s*set\s+\+e\b|\bignore_errors\s*:\s*true\b")
EXCLUDED = re.compile(r"--ignore(?:-glob)?[= ]\S|--deselect\b|\bcollect_ignore(?:_glob)?\b|\b(?:testPathIgnorePatterns|modulePathIgnorePatterns|"
                      r"testIgnore|excludedGroups|excludeTags)\b|-k\s*[\"']?\s*not\b|-m\s*[\"']?\s*not\b|\bnorecursedirs\b|"
                      r"<exclude>[^<]*Test|\bexclude\s*\(?\s*[\"'][^\"']*Test")
HOOK_PY = re.compile(r"\bdef\s+(pytest_(?:runtest_\w+|collection_modifyitems|report_teststatus|sessionfinish|terminal_summary|pyfunc_call|"
                     r"ignore_collect|collect_file|deselected|collectreport|make_collect_report|exception_interact))\s*\(")
HOOK_EDIT = re.compile(r"\.\s*outcome\s*=(?!=)|\.longrepr\s*=(?!=)|\bexitstatus\s*=(?!=)|\.force_(?:result|exception)\s*\(|\bwasxfail\b|"
                       r"\.(?:passed|failed)\s*=(?!=)|\.state\s*=\s*['\"]passed['\"]")
HOOK_JS = re.compile(r"\b(?:reporters|testResultsProcessor|customReporter)\s*:|\bprocess\.on\s*\(\s*['\"](?:exit|uncaughtException|"
                     r"unhandledRejection)['\"]")
HOOK_JVM = re.compile(r"\bimplements\b[^{]*\b(?:TestExecutionExceptionHandler|ITestListener|IRetryAnalyzer|IHookable|TestWatcher|"
                      r"InvocationInterceptor)\b")
EXIT = re.compile(r"\b(?:os\._exit\s*\([^)]*\)|(?:sys\.exit|process\.exit|System\.exit|os\.Exit|exit)\s*\(\s*(?:0|False)?\s*\)|"
                  r"Runtime\.getRuntime\(\)\.halt\s*\()")
EQUALITY = re.compile(r"\bdef\s+__eq__\s*\(|\bboolean\s+equals\s*\(\s*(?:final\s+)?Object\b|\bfun\s+equals\s*\(|"
                      r"\bbool\s+Equals\s*\(\s*object\b|\bequals\s*\([^)]*\)\s*\{")
ALWAYS_TRUE = re.compile(r"\breturn\s+(?:True|true)\b\s*;?")
DETECTS_TEST = re.compile(r"PYTEST_CURRENT_TEST|PYTEST_VERSION|[\"'](?:pytest|unittest)[\"']\s+in\s+sys\.modules|"
                          r"sys\.modules\.get\(\s*[\"']pytest|NODE_ENV\s*[!=]==?\s*[\"']test[\"']|[\"']test[\"']\s*[!=]==?\s*process\.env\.NODE_ENV|"
                          r"\bJEST_WORKER_ID\b|\bVITEST\b|__karma__|Rails\.env\.test\?|RAILS_ENV|\btesting\.Testing\s*\(\s*\)|"
                          r"APP_ENV[^\n]*testing")
LITERAL = r"-?\d+(?:\.\d+)?|\"[^\"\n]{1,80}\"|'[^'\n]{1,80}'"
CONDITION = re.compile(r"^\s*(?:if|elif|elsif|else\s+if|unless|when|case|}\s*else\s+if)\b|\bif\s*\(|\?\s*[^:]+:")
COMPARED = re.compile(r"(?:===?|\bis\b|\.equals\(|\beq\b)\s*(" + LITERAL + r")|(" + LITERAL + r")\s*(?:===?)|^\s*case\s+(" + LITERAL + r")\s*[:=]")
TRIVIAL = {"0", "1", "-1", "0.0", "1.0", '""', "''"}


def _hit(rule: str, path: str, line: Dict[str, Any], hunk: Dict[str, Any], detail: str, removed: Sequence[str] = (),
         added: Sequence[str] = ()) -> Dict[str, Any]:
    head = line["new"] is not None
    return {"rule": rule, "language": language_of(path) or "text", "path": path, "line": line["new"] if head else line["old"],
            "side": "head" if head else "base", "removed": [t.strip() for t in removed], "added": [t.strip() for t in added],
            "hunk": hunk["header"], "detail": detail}


# --------------------------------------------------------------------------- the rules

def _assertion_rules(path: str, language: Optional[str], hunk: Dict[str, Any]) -> List[Dict[str, Any]]:
    lines = hunk["lines"]
    _, removed_owned = owned_by_defs(lines, "-", language)
    _, added_owned = owned_by_defs(lines, "+", language)
    removed = [(i, l) for i, l in enumerate(lines) if l["kind"] == "-" and i not in removed_owned and is_assertion(l["text"])]
    added = [(i, l) for i, l in enumerate(lines) if l["kind"] == "+" and i not in added_owned and is_assertion(l["text"])]
    hits, used_removed, used_added = [], set(), set()
    for i, line in added:
        if not is_weak(line["text"]):
            continue
        what = subject(line["text"])
        if not what:
            continue
        for j, old in removed:
            if j in used_removed or is_weak(old["text"]) or what not in re.sub(r"\s+", "", old["text"]):
                continue
            used_removed.add(j)
            used_added.add(i)
            hits.append(_hit("loosened-assertion", path, line, hunk, "an exact assertion on %s replaced by a weaker check" % what,
                             [old["text"]], [line["text"]]))
            break
    left = [l for j, l in removed if j not in used_removed]
    right = [l for i, l in added if i not in used_added]
    if len(left) > len(right):
        kept = {re.sub(r"\s+", "", l["text"]) for l in right}
        gone = [l for l in left if re.sub(r"\s+", "", l["text"]) not in kept] or left
        hits.append(_hit("assertion-removed", path, gone[0], hunk, "%d assertion(s) removed, %d added in this hunk" % (len(left), len(right)),
                         [l["text"] for l in gone]))
    return hits


def _deleted_tests(path: str, language: Optional[str], entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    removed, added = [], set()
    for hunk in entry["hunks"]:
        for line, name in owned_by_defs(hunk["lines"], "-", language)[0]:
            removed.append((line, name, hunk))
        added.update(name for _line, name in owned_by_defs(hunk["lines"], "+", language)[0])
    gone = [item for item in removed if item[1] not in added]
    new_names = added - {name for _l, name, _h in removed}
    if len(gone) <= len(new_names):
        return []  # as many new names as removed ones: renamed, not deleted
    return [_hit("deleted-test", path, line, hunk, "test %s removed (%d test definition(s) removed, %d new)" % (name, len(gone), len(new_names)),
                 [line["text"]]) for line, name, hunk in gone]


def _tolerances(text: str) -> Dict[str, float]:
    found = {}
    for match in TOLERANCE.finditer(text):
        if match.group(1):
            found[match.group(1)] = float(match.group(2))
        elif match.group(3):
            found["digits"] = float(match.group(3))
        elif match.group(4):
            found[match.group(4)] = float(match.group(5))
    return found


def _tolerance_rule(path: str, hunk: Dict[str, Any]) -> List[Dict[str, Any]]:
    removed = [l for l in hunk["lines"] if l["kind"] == "-" and _tolerances(l["text"])]
    added = [l for l in hunk["lines"] if l["kind"] == "+" and _tolerances(l["text"])]
    hits = []
    for old, new in zip(removed, added):
        before, after = _tolerances(old["text"]), _tolerances(new["text"])
        for key in sorted(set(before) & set(after)):
            wider = after[key] < before[key] if key in NARROWER_WHEN_LARGER else after[key] > before[key]
            if wider:
                hits.append(_hit("tolerance-widened", path, new, hunk, "%s went from %g to %g" % (key, before[key], after[key]),
                                 [old["text"]], [new["text"]]))
    return hits


def _coverage_values(text: str) -> Dict[str, float]:
    found = {}
    for key, pattern in COVERAGE:
        match = pattern.search(text)
        if match:
            value = next(g for g in match.groups() if g is not None)
            found[key] = float(value)
    return found


def _coverage_rule(path: str, hunk: Dict[str, Any]) -> List[Dict[str, Any]]:
    context = hunk["header"] + "\n" + "\n".join(l["text"] for l in hunk["lines"])
    if not COVERAGE_CONTEXT.search(context) and not COVERAGE_CONTEXT.search(path):
        return []
    removed = [l for l in hunk["lines"] if l["kind"] == "-" and _coverage_values(l["text"])]
    added = [l for l in hunk["lines"] if l["kind"] == "+" and _coverage_values(l["text"])]
    hits = []
    for old, new in zip(removed, added):
        before, after = _coverage_values(old["text"]), _coverage_values(new["text"])
        for key in sorted(set(before) & set(after)):
            if after[key] < before[key]:
                hits.append(_hit("coverage-threshold-lowered", path, new, hunk, "%s went from %g to %g" % (key, before[key], after[key]),
                                 [old["text"]], [new["text"]]))
    return hits


def _equality_rule(path: str, hunk: Dict[str, Any]) -> List[Dict[str, Any]]:
    lines, hits = hunk["lines"], []
    for index, line in enumerate(lines):
        if line["kind"] != "+" or not EQUALITY.search(line["text"]):
            continue
        rest = line["text"][EQUALITY.search(line["text"]).end():]
        rest = re.sub(r"^[^{]*?\)\s*(?:->\s*\w+\s*|:\s*\w+\s*)?[:{]?", "", rest)  # the rest of the signature
        body = [rest] + [l["text"] for l in lines[index + 1:index + 4] if l["kind"] == "+"]
        first = next((t.strip().lstrip("{").strip() for t in body if t.strip().lstrip("{").strip() and not COMMENT.match(t)
                      and not t.strip().startswith("@")), "")
        if ALWAYS_TRUE.match(first):
            hits.append(_hit("equality-overridden", path, line, hunk, "the equality method is always true: every comparison with this type passes",
                             added=[line["text"]] + [l["text"] for l in lines[index + 1:index + 3] if l["kind"] == "+"]))
    return hits


def _line_rules(path: str, kind: str, language: Optional[str], hunk: Dict[str, Any]) -> List[Dict[str, Any]]:
    hits = []
    skip = SKIP.get(language or "")
    testish = kind in ("test", "test-support")
    for line in hunk["lines"]:
        if line["kind"] != "+":
            continue
        text = line["text"]
        if COMMENT.match(text) and kind not in ("ci", "test-config"):
            continue
        if testish and skip and re.search(skip, text):
            hits.append(_hit("skip-added", path, line, hunk, "a test is skipped, marked as expected to fail or focused", added=[text]))
        if kind in ("ci", "test-config") and CI_IGNORED.search(text):
            hits.append(_hit("ci-failure-ignored", path, line, hunk, "a step's failure no longer fails the pipeline", added=[text]))
        if kind in ("ci", "test-config", "test-support") and EXCLUDED.search(text):
            hits.append(_hit("tests-excluded", path, line, hunk, "the runner is told to leave tests out", added=[text]))
        if testish or kind == "test-config":
            if language == "python" and (HOOK_PY.search(text) or HOOK_EDIT.search(text)):
                hits.append(_hit("runner-hook", path, line, hunk, "a runner hook that can change which tests run or what they report",
                                 added=[text]))
            elif kind == "test-config" and HOOK_JS.search(text):
                hits.append(_hit("runner-hook", path, line, hunk, "a reporter or process hook that can change what a run reports",
                                 added=[text]))
            elif language in ("javascript", "typescript") and HOOK_JS.search(text):
                hits.append(_hit("runner-hook", path, line, hunk, "a process hook that can change what a run reports", added=[text]))
            elif language in ("java", "kotlin") and HOOK_JVM.search(text):
                hits.append(_hit("runner-hook", path, line, hunk, "a runner extension that can change what a run reports", added=[text]))
        if testish and EXIT.search(text):
            hits.append(_hit("forced-exit", path, line, hunk, "the process is ended with success from test code", added=[text]))
        if kind == "product" and language is not None and DETECTS_TEST.search(text):
            hits.append(_hit("test-detection-in-product", path, line, hunk, "product code checks whether it runs under a test", added=[text]))
    return hits


def _literals(text: str) -> List[str]:
    if not CONDITION.search(text):
        return []
    found = []
    for match in COMPARED.finditer(text):
        value = next(g for g in match.groups() if g is not None)
        if value not in TRIVIAL:
            found.append(value)
    return found


def _uses(text: str, literal: str) -> bool:
    if literal[:1] in ("'", '"'):
        return bool(re.search(r"[\"']%s[\"']" % re.escape(literal[1:-1]), text))
    return bool(re.search(r"(?<![\w.])%s(?![\w.])" % re.escape(literal), text))


def _special_cases(files: List[Dict[str, Any]], kinds: Dict[str, str]) -> List[Dict[str, Any]]:
    added_tests = [(entry["path"], l["text"]) for entry in files if kinds[entry["path"]] == "test"
                   for hunk in entry["hunks"] for l in hunk["lines"] if l["kind"] == "+"]
    if not added_tests:
        return []
    hits = []
    for entry in files:
        if kinds[entry["path"]] != "product" or language_of(entry["path"]) is None:
            continue
        for hunk in entry["hunks"]:
            for line in hunk["lines"]:
                if line["kind"] != "+" or COMMENT.match(line["text"]):
                    continue
                for literal in _literals(line["text"]):
                    tests = sorted({path for path, text in added_tests if _uses(text, literal)})
                    if tests:
                        hits.append(_hit("special-cased-input", entry["path"], line, hunk,
                                         "an added condition compares with %s, a value an added test in %s also uses: a special case "
                                         "for the test's input rather than a rule" % (literal, ", ".join(tests)), added=[line["text"]]))
    return hits


def scan_diff(text: str) -> List[Dict[str, Any]]:
    """Every hit in a unified diff, sorted by path, line and rule."""
    files = [entry for entry in parse_diff(text) if not entry["binary"]]
    kinds = {entry["path"]: kind_of(entry["path"]) for entry in files}
    hits: List[Dict[str, Any]] = []
    for entry in files:
        path, kind, language = entry["path"], kinds[entry["path"]], language_of(entry["path"])
        if kind == "snapshot" and entry["status"] in ("M", "D"):
            first = next((l for h in entry["hunks"] for l in h["lines"] if l["kind"] == "-"), None)
            if first is not None:
                hunk = next(h for h in entry["hunks"] if first in h["lines"])
                hits.append(_hit("snapshot-rewritten", path, first, hunk, "a stored snapshot was %s" %
                                 ("deleted" if entry["status"] == "D" else "rewritten"), [first["text"]]))
            continue
        if kind == "test":
            hits.extend(_deleted_tests(path, language, entry))
        for hunk in entry["hunks"]:
            if kind in ("test", "test-support"):
                hits.extend(_assertion_rules(path, language, hunk))
                hits.extend(_tolerance_rule(path, hunk))
            if kind in ("test-config", "ci", "test-support") or "coverage" in path.lower() or "codecov" in path.lower():
                hits.extend(_coverage_rule(path, hunk))
            if language is not None:
                hits.extend(_equality_rule(path, hunk))
            hits.extend(_line_rules(path, kind, language, hunk))
    hits.extend(_special_cases(files, kinds))
    unique = {}
    for hit in hits:
        unique.setdefault((hit["path"], hit["line"], hit["rule"], hit["side"], hit["detail"]), hit)
    return [unique[key] for key in sorted(unique)]


# --------------------------------------------------------------------------- command line

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Scan a change for weakened tests.")
    parser.add_argument("--base", help="the commit before the change")
    parser.add_argument("--head", default=None, help="the commit with the change (default HEAD)")
    parser.add_argument("--diff", help="a file holding a unified diff, instead of --base/--head")
    args = parser.parse_args(argv)
    try:
        if args.diff:
            if args.base or args.head:
                raise ChangeError("bad_arguments", "--diff is used alone")
            with open(args.diff, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            base = head = None
        else:
            if not args.base:
                raise ChangeError("bad_arguments", "give --base (and optionally --head), or --diff")
            repo = _change.Repo(os.getcwd())
            base, head = repo.resolve(args.base), repo.resolve(args.head or "HEAD")
            text = repo.diff(base, head)
    except ChangeError as error:
        return _change.refuse(error)
    except OSError as exc:
        return _change.refuse(ChangeError("unreadable_diff", "the diff file could not be read: %s" % exc.strerror))
    hits = scan_diff(text)
    counts: Dict[str, int] = {}
    for hit in hits:
        counts[hit["rule"]] = counts.get(hit["rule"], 0) + 1
    return _change.emit({"ok": True, "base": base, "head": head, "hits": hits, "counts": counts})


if __name__ == "__main__":
    sys.exit(main())
