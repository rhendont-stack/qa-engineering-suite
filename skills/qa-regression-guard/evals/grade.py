"""Grades one evaluation run of a qa-regression-guard case against its hard criteria.

Usage: python3 -I grade.py --out DIR --run-id ID

``DIR`` is the ``--out`` directory given to ``tools/run_skill_eval.py``; ``ID`` is the ``run_id`` it printed. The grader reads
``DIR/costs.jsonl`` (case, configuration, installed SKILL.md sha256), ``DIR/runs/ID/stdout.txt`` (the final answer and every
tool call), ``DIR/runs/ID/file-state/`` (the copy's files as the agent left them: ``git status``, the binary diff against the
commit the agent started from, and ``meta.json``) and ``DIR/runs/ID/qa-state/`` (the ``.qa`` the agent left, folded through the
skill's own kernel, read-only on a copy). It reads the F7 answer key (``benchmark/answer-keys/regression-repo.json``) for the
weakened assertion and the hook, and makes the F7 history again with the fixture's own generator to know the commits.

Every case starts from the state ``_cases.py`` describes: F7 generated with its history and stopped at C1, C3 or C5, the project
trusted by the user's words, and in the fix case one finding (FND-001, the failing order total).

Kinds of criterion:

* ``record``: from the recorded state (executions and their results, worktrees, findings, fixes, revalidations, gate records),
  and for ``qa compare`` also from the transcript: the kernel's compare command must have been called on the pair.
* ``action``: from the file state: no project file changed.
* ``substance``: patterns over the final answer.

In a with-skill run the ``record`` and ``action`` criteria are hard and the ``substance`` ones a secondary signal. A baseline run
has no kernel of its own, so its ``record`` criteria are ``absent``; it is judged on its ``action`` criterion and on
``substance`` criteria that restate the record ones as what the answer must show. Nothing passes vacuously:

* a with-skill run whose evidence holds no ledger (or one the kernel cannot fold) fails every ``record`` criterion with
  "no ledger in the run's evidence";
* a run whose evidence holds no file state, one taken with an error, a cut diff, no readable ``status.txt``, a malformed
  ``redacted_files`` or ``gitlinks``, or a base commit other than the case's commit fails the ``action`` criterion;
* every criterion over runs, findings, fixes or gate records needs at least one of them.

Fixed definitions (stated here because the grader must not depend on the skill text it grades):

* ``C0``..``C5`` are the commits the fixture's ``generate_history`` makes, oldest first.
* The project's changes are every path outside ``.qa/`` that the file state names (the diff's paths, every ``status.txt``
  entry, ``redacted_files`` and the gitlinks that differ from HEAD's), read exactly as the qa-test-generation grader reads them.
* A base run in a suite worktree (feature case) is an execution whose ``git.commit`` is C0, whose ``git.worktree`` is a path
  under ``.qa/work/``, whose status is ``passed`` and which holds a ``passed`` result for the order total test. A head run is an
  execution at C1 whose status is ``failed`` and which holds a ``failed`` result for that test.
* ``qa compare`` shows the new failure when a successful Bash call ran the launcher's ``compare`` with a base run and a head
  run of that kind as its two IDs, the base first, and the kernel's own ``compare`` of that pair is comparable and lists the order total test
  in ``new_failures``.
* A finding cites the weakened assertion's hunk when its category is ``test-quality`` and its text (every field) names
  ``tests/test_pricing.py`` and either quotes the weakened assertion (whitespace ignored, ``self.`` optional), or cites its line
  (``test_pricing.py:N`` or ``#LN``), or holds a hunk header whose new range covers that line.
* The answer shows the base run (feature case, baseline restatement of the base-run criterion) when it says the order total
  test, or the suite, passed on the base: "passed" near "base", "parent", "previous", "prior", "earlier" or "original" (commit,
  run, revision or version), ``HEAD~1``, ``HEAD^``, ``C0``, a prefix of C0's hash, or "before this commit / change".
* The masked failure is shown by a run (hook case, both arms) when, with the skill, an execution at C5 holds a ``failed``
  result for the loyalty points test (the hook rewrites it to ``passed``, so only a run without the hook shows it), and in a
  baseline when a successful Bash call ran pytest with ``--noconftest`` and its output reports a failure.
* The hook finding is an unresolved ``test-quality`` finding whose text names ``conftest.py`` and ``pytest_runtest_makereport``
  or a hook. ``test_integrity`` is ``NOT_MET`` when the latest gate record (by number) gives that outcome to its
  ``test_integrity`` criterion and lists a hook finding in that criterion's evidence.
* The fix is revalidated when a revalidation record's outcome, as the fold derives it, is ``passed``, for a fix of FND-001
  whose base commit is C2 and fix commit C3.

Prints one JSON object. Exit 0 when every hard criterion that applies to the run holds, 1 otherwise, 2 on bad input.
"""
from __future__ import annotations

import argparse
import functools
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _cases  # noqa: E402

SKILL = "qa-regression-guard"
REPO = HERE.parents[2]
GENERATOR = REPO / "fixtures" / "regression-repo" / "generate.py"
TEMPLATES = REPO / "fixtures" / "regression-repo" / "templates"
ANSWER_KEY = REPO / "benchmark" / "answer-keys" / "regression-repo.json"
_KERNEL = HERE.parent / "scripts" / "_kernel"
sys.path.insert(0, str(_KERNEL if (_KERNEL / "qa").is_dir() else REPO / "shared" / "kernel"))
from qa import compare as qa_compare  # noqa: E402

ORDER_TOTAL, LOYALTY = _cases.ORDER_TOTAL, _cases.LOYALTY
ORDER_NAME = ORDER_TOTAL.split("::")[1]
WEAKENED_NAME = _cases.WEAKENED.split("::")[1]
FAIL_WORD = re.compile(r"\bfail(?:s|ed|ing|ure|ures)?\b|\bbroke(?:n)?\b|\bregress", re.I)
DONE_CLAIM = re.compile(r"(?<!\bnot )(?<!n't )(?<!\bnever )\b(?:ready to merge|safe to merge|good to merge|fine to merge|"
                        r"ok to merge|okay to merge|(?:the )?feature is (?:done|complete))\b", re.I)
WEAKENING_WORD = re.compile(r"weaken|loosen|no longer (?:check|verif|assert|compar)|always (?:pass|true|truthy)|truthy|vacuous|"
                            r"only checks|any non-?zero|non-?zero|cannot fail|can't fail|never fail", re.I)
BEFORE = r"(?:base|parent|previous|prior|pre-fix|before the fix|before it|HEAD~1|HEAD\^|C2)"
AFTER = r"(?:fix|fixed|head|HEAD|after|C3|now)"
FAILED_BEFORE = re.compile(r"fail\w*[^.\n]{0,120}\b%s\b|\b%s\b[^.\n]{0,120}\bfail" % (BEFORE, BEFORE), re.I)
PASSED_AFTER = re.compile(r"pass\w*[^.\n]{0,120}\b%s\b|\b%s\b[^.\n]{0,120}\bpass" % (AFTER, AFTER), re.I)
HOOK_NAMED = re.compile(r"conftest(?:\.py)?|pytest_runtest_makereport", re.I)
REWRITES = re.compile(r"(?:rewrit|turn|convert|flip|mark|report|mask|hid|swallow|forc|overrid|chang|relabel)\w*[^.\n]{0,80}"
                      r"\bfail\w*[^.\n]{0,80}\bpass|\bfailures?\b[^.\n]{0,40}\b(?:as|into|to)\b[^.\n]{0,20}\bpass", re.I)
BLOCKED = re.compile(r"NOT_MET|NOT_READY|not ready|do not merge|don't merge|should not (?:be )?merged?|must not (?:be )?merged?|"
                     r"\bblock|\breject|not safe|unsafe to merge|cannot be merged|can't be merged", re.I)
BASE_WORD = r"(?:base|parent|previous|prior|earlier|original)"
BASE_PASSED = re.compile(r"pass\w*[^.\n]{0,100}\b(?:on|at|in|with|for)\s+(?:the\s+)?%s\b|\b%s\s+(?:commit|run|revision|version)\b"
                         r"[^.\n]{0,100}\bpass|pass\w*[^.\n]{0,60}(?:HEAD~1|HEAD\^|\bC0\b)|(?:HEAD~1|HEAD\^|\bC0\b)[^.\n]{0,60}\bpass|"
                         r"pass\w*\s+before\s+(?:this|the)\s+(?:commit|change)" % (BASE_WORD, BASE_WORD), re.I)
TIER = re.compile(r"tier_percent|quantity\s*>\s*25|>\s*25\b|from 20 units|20\s*(?:to|-|and)\s*25", re.I)
HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


# --------------------------------------------------------------------------- the fixture

@functools.lru_cache(maxsize=1)
def commits() -> Tuple[str, ...]:
    """C0..C5 as the fixture's generator makes them (deterministic), oldest first."""
    spec = importlib.util.spec_from_file_location("regression_repo_generate_for_grading", str(GENERATOR))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory(prefix="qa-grade-f7-") as scratch:
        root = Path(scratch) / "repo"
        module.generate_history(root)
        done = subprocess.run(["git", "-C", str(root), "rev-list", "--reverse", "HEAD"], capture_output=True, text=True, timeout=60,
                              env={"PATH": os.environ.get("PATH", os.defpath), "HOME": scratch, "GIT_CONFIG_NOSYSTEM": "1",
                                   "GIT_CONFIG_GLOBAL": os.devnull})
    found = tuple(done.stdout.split())
    if done.returncode != 0 or len(found) != 6:
        raise SystemExit("the F7 generator did not give six commits")
    return found


@functools.lru_cache(maxsize=1)
def key_issues() -> Dict[str, Dict[str, Any]]:
    return {issue["id"]: issue for issue in json.loads(ANSWER_KEY.read_text(encoding="utf-8"))["issues"]}


def weakened() -> Tuple[str, str, int]:
    """``(path, the weakened assertion as written, its line at C1)`` from the answer key and the C1 template."""
    issue = key_issues()["F7-02"]
    path, match = issue["location"]["path"], issue["location"]["match"]
    lines = (TEMPLATES / ("c%d" % issue["commit"]) / (path + ".tmpl")).read_text(encoding="utf-8").splitlines()
    line = next(n for n, text in enumerate(lines, 1) if match in text)
    return path, match, line


def hook() -> Tuple[str, str]:
    issue = key_issues()["F7-06"]
    return issue["location"]["path"], issue["location"]["function"]


# --------------------------------------------------------------------------- transcript

def events(stdout: str) -> List[Dict[str, Any]]:
    found = []
    for line in stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            found.append(item)
    return found


def final_text(stdout: str) -> str:
    result = next((e for e in reversed(events(stdout)) if e.get("type") == "result"), {})
    return str(result.get("result", ""))


def tool_calls(stdout: str) -> List[Dict[str, Any]]:
    calls: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for event in events(stdout):
        message = event.get("message") if event.get("type") in ("assistant", "user") else None
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                calls[str(block.get("id"))] = {"name": block.get("name"), "input": block.get("input", {}), "error": None, "output": ""}
                order.append(str(block.get("id")))
            elif block.get("type") == "tool_result" and str(block.get("tool_use_id")) in calls:
                calls[str(block.get("tool_use_id"))]["error"] = bool(block.get("is_error"))
                calls[str(block.get("tool_use_id"))]["output"] = result_text(block.get("content"))
    return [calls[i] for i in order]


def result_text(content: Any) -> str:
    """A tool result's text: a string, or the text blocks of a list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return ""


def skill_loading(stdout: str) -> Dict[str, Any]:
    evs = events(stdout)
    init = next((e for e in evs if e.get("type") == "system" and e.get("subtype") == "init"), {})
    calls = [(c["name"], json.dumps(c["input"])) for c in tool_calls(stdout)]
    listed, called = SKILL in (init.get("skills") or []), any(n == "Skill" and SKILL in i for n, i in calls)
    return {"api_key_source": init.get("apiKeySource"), "listed_at_init": listed, "skill_tool_called": called, "used": listed and called,
            "kernel_called": any(n == "Bash" and "qa.py" in i for n, i in calls)}


def run_record(out: Path, run_id: str) -> Dict[str, Any]:
    for line in (out / "costs.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("run_id") == run_id and record.get("event") == "end":
            return record
    raise SystemExit("no end record for run %s in %s" % (run_id, out / "costs.jsonl"))


# --------------------------------------------------------------------------- the kernel state

def launcher() -> Path:
    path = HERE.parent / "scripts" / "qa.py"
    return path if path.is_file() else REPO / "shared" / "launcher" / "qa.py"


def ledger_events(saved: Path) -> List[Dict[str, Any]]:
    found = []
    for chain in sorted((saved / "ledger").glob("*.jsonl")):
        for line in chain.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                found.append(item)
    return found


def kernel_facts(evidence: Path) -> Optional[Dict[str, Any]]:
    """The folded state and the raw ledger events from ``evidence/qa-state/``; None without a ledger."""
    saved = evidence / "qa-state"
    if not (saved / "ledger").is_dir():
        return None
    with tempfile.TemporaryDirectory(prefix="qa-grade-") as scratch:
        shutil.copytree(str(saved), str(Path(scratch) / ".qa"), symlinks=True)
        done = subprocess.run([sys.executable, "-I", str(launcher()), "--workspace", scratch, "--role", "user", "state", "show"],
                              capture_output=True, text=True, timeout=120)
        try:
            state = json.loads(done.stdout)["state"]
        except (ValueError, KeyError):
            return None
    return {"state": state, "events": ledger_events(saved)}


def _number(record_id: str) -> int:
    digits = re.sub(r"\D", "", str(record_id))
    return int(digits) if digits else -1


def text_of(record: Dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True)


# --------------------------------------------------------------------------- the file state (as the qa-test-generation grader reads it)

def _unquote(text: str) -> str:
    """A path git quoted C-style (``"a/\\303\\251.py"``), as text."""
    body, out, i = text[1:-1], bytearray(), 0
    simple = {"n": 10, "t": 9, "r": 13, "a": 7, "b": 8, "f": 12, "v": 11, "\\": 92, '"': 34}
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in simple:
                out.append(simple[nxt])
                i += 2
                continue
            if re.match(r"[0-7]{3}", body[i + 1:i + 4]):
                out.append(int(body[i + 1:i + 4], 8))
                i += 4
                continue
        out += ch.encode("utf-8")
        i += 1
    return out.decode("utf-8", "replace")


def diff_paths(diff: str) -> List[str]:
    """The paths a ``git diff --no-renames`` names, in order, from its ``diff --git`` lines."""
    found: List[str] = []
    for line in diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        rest = line[len("diff --git "):]
        path = None
        if rest.startswith('"'):
            match = re.match(r'("(?:[^"\\]|\\.)*")', rest)
            if match:
                path = _unquote(match.group(1))[2:]
        else:
            size = (len(rest) - 5) // 2
            if size > 0 and rest.startswith("a/") and rest[2 + size:] == " b/" + rest[2:2 + size]:
                path = rest[2:2 + size]
        if path is not None and path not in found:
            found.append(path)
    return found


def status_paths(status: str) -> List[str]:
    """The paths a ``git status --porcelain=v1 --no-renames`` names, every entry, untracked ones included; an untracked
    directory (a nested repository) without its trailing slash."""
    found: List[str] = []
    for line in status.splitlines():
        if len(line) < 4 or line[2] != " ":
            continue
        path = line[3:]
        path = _unquote(path) if path.startswith('"') and path.endswith('"') and len(path) > 1 else path
        path = path.rstrip("/")
        if path and path not in found:
            found.append(path)
    return found


def _listed(meta: Dict[str, Any]) -> Optional[Tuple[List[str], List[str]]]:
    """``(redacted_files, gitlink paths that differ from HEAD)`` from ``meta.json``, or None when either field is malformed.
    Evidence from before the harness wrote them has neither, and its diff left nothing out, so both are empty then."""
    redacted = meta.get("redacted_files", [])
    links = meta.get("gitlinks", {"head": [], "index": [], "work_tree": []})
    if not isinstance(redacted, list) or not all(isinstance(p, str) for p in redacted) or not isinstance(links, dict):
        return None
    sides = {}
    for side in ("head", "index", "work_tree"):
        entries = links.get(side)
        if not isinstance(entries, list) or not all(isinstance(e, dict) and isinstance(e.get("path"), str)
                                                    and isinstance(e.get("commit"), str) for e in entries):
            return None
        sides[side] = {(e["path"], e["commit"]) for e in entries}
    moved = {path for path, commit in sides["index"] | sides["work_tree"] if (path, commit) not in sides["head"]}
    moved |= {path for path, commit in sides["head"] if (path, commit) not in sides["work_tree"]}
    return list(redacted), sorted(moved)


def file_state(evidence: Path) -> Dict[str, Any]:
    """``{ok, why, meta, diff, changed, project, not_in_diff}``; ``ok`` False when there is no usable snapshot (never an empty
    one). ``changed`` is every path the snapshot names as changed: the diff's, every ``status.txt`` entry, ``redacted_files``
    and the gitlinks; a path named anywhere but the diff is in ``not_in_diff`` and is a change all the same."""
    folder = evidence / "file-state"
    out: Dict[str, Any] = {"ok": False, "why": "", "meta": None, "diff": "", "changed": [], "project": [], "not_in_diff": []}
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        out["why"] = "no file-state snapshot in the run's evidence"
        return out
    out["meta"] = meta
    if not isinstance(meta, dict) or meta.get("error"):
        out["why"] = "the file state was not taken: %s" % (meta.get("error") if isinstance(meta, dict) else "bad meta.json")
        return out
    if meta.get("diff_truncated") is not False:
        out["why"] = "the saved diff was cut, so the final files cannot be known"
        return out
    listed = _listed(meta)
    if listed is None:
        out["why"] = "meta.json holds a malformed redacted_files or gitlinks"
        return out
    try:
        diff = (folder / "diff.patch").read_bytes().decode("utf-8", "replace")
        status = (folder / "status.txt").read_bytes().decode("utf-8", "replace")
    except OSError:
        out["why"] = "status.txt or diff.patch is missing from the snapshot"
        return out
    in_diff = diff_paths(diff)
    changed = list(in_diff)
    for path in status_paths(status) + listed[0] + listed[1]:
        if path not in changed:
            changed.append(path)
    out.update(ok=True, diff=diff, changed=changed, project=[p for p in changed if not under_qa(p)],
               not_in_diff=[p for p in changed if p not in in_diff])
    return out


def under_qa(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(parts) and parts[0] == ".qa"


# --------------------------------------------------------------------------- criteria

def crit(name: str, hard: bool, kind: str, ok: Optional[bool], detail: str) -> Dict[str, Any]:
    return {"name": name, "hard": hard, "kind": kind, "pass": ok, "detail": detail}


def absent(names: Iterable[str]) -> List[Dict[str, Any]]:
    return [crit(name, True, "record", None, "no kernel in a baseline run") for name in names]


def unrecorded(names: Iterable[str]) -> List[Dict[str, Any]]:
    return [crit(name, True, "record", False, "no ledger in the run's evidence") for name in names]


def unchanged(fs: Dict[str, Any], expected_commit: str) -> Dict[str, Any]:
    """No project file changed, on a snapshot taken from the case's commit."""
    if not fs["ok"]:
        return crit("no_project_file_changed", True, "action", False, fs["why"])
    base = (fs["meta"] or {}).get("base_commit")
    if base != expected_commit:
        return crit("no_project_file_changed", True, "action", False,
                    "the run started at %s, not at the case's commit %s" % (base, expected_commit))
    return crit("no_project_file_changed", True, "action", not fs["project"], "project paths changed: %s" % (fs["project"] or "none"))


def results_of(state: Dict[str, Any], execution: str) -> Dict[str, str]:
    """test id -> status in ``execution``, any failure winning over a pass, as the kernel's compare ranks them."""
    return {test: entry["status"] for test, entry in qa_compare.tests_of(state, execution).items()}


def executions_at(state: Dict[str, Any], commit: str) -> List[Tuple[str, Dict[str, Any]]]:
    found = []
    for exe, record in sorted(state.get("executions", {}).items(), key=lambda item: _number(item[0])):
        git = record.get("git") if isinstance(record.get("git"), dict) else {}
        if git.get("commit") == commit:
            found.append((exe, record))
    return found


def in_suite_worktree(record: Dict[str, Any]) -> bool:
    tree = (record.get("git") or {}).get("worktree")
    return isinstance(tree, str) and PurePosixPath(tree).parts[:2] == (".qa", "work")


def base_runs(state: Dict[str, Any], c0: str) -> List[str]:
    return [exe for exe, record in executions_at(state, c0) if in_suite_worktree(record) and record.get("status") == "passed"
            and results_of(state, exe).get(ORDER_TOTAL) == "passed"]


def head_runs(state: Dict[str, Any], c1: str) -> List[str]:
    return [exe for exe, record in executions_at(state, c1) if record.get("status") == "failed"
            and results_of(state, exe).get(ORDER_TOTAL) == "failed"]


def compare_calls(calls: List[Dict[str, Any]]) -> List[str]:
    """The commands of the successful Bash calls that ran the kernel's ``compare``."""
    found = []
    for call in calls:
        command = str((call.get("input") or {}).get("command", ""))
        if call.get("name") == "Bash" and call.get("error") is False and "qa.py" in command and re.search(r"\scompare\s", command):
            found.append(command)
    return found


def compare_criterion(state: Dict[str, Any], calls: List[Dict[str, Any]], bases: List[str], heads: List[str]) -> Dict[str, Any]:
    shown = []
    for command in compare_calls(calls):
        ids = re.findall(r"\bEXE-[0-9A-Za-z]+\b", command)
        for base, head in zip(ids, ids[1:]):          # the command's order: the base run first, then the head run
            if base in bases and head in heads:
                out = qa_compare.compare(state, base, head)
                if out["comparable"] and ORDER_TOTAL in out["new_failures"]:
                    shown.append("%s -> %s" % (base, head))
    return crit("compare_shows_the_new_failure", True, "record", bool(shown),
                "launcher compare calls: %d; on a base run and a head run showing %s as a new failure: %s"
                % (len(compare_calls(calls)), ORDER_NAME, sorted(set(shown)) or "none"))


def cites_weakened_hunk(record: Dict[str, Any]) -> bool:
    path, match, line = weakened()
    text = text_of(record)
    if path not in text:
        return False
    squeezed = re.sub(r"\s+", "", text)
    quoted = re.sub(r"\s+", "", match)
    if quoted in squeezed or quoted.replace("self.", "", 1) in squeezed:
        return True
    name = PurePosixPath(path).name
    if re.search(r"%s(?::|#L)%d(?!\d)" % (re.escape(name), line), text):
        return True
    for start, length in HUNK.findall(text):
        first, count = int(start), int(length) if length else 1
        if first <= line < first + count:
            return True
    return False


def weakening_criterion(state: Dict[str, Any]) -> Dict[str, Any]:
    findings = sorted(state.get("findings", {}).items(), key=lambda item: _number(item[0]))
    quality = [fid for fid, f in findings if f.get("category") == "test-quality"]
    citing = [fid for fid, f in findings if f.get("category") == "test-quality" and cites_weakened_hunk(f)]
    return crit("weakening_finding_cites_the_hunk", True, "record", bool(citing),
                "test-quality findings: %s; citing the weakened assertion's hunk in %s: %s"
                % (quality or "none", weakened()[0], citing or "none"))


def tier_secondary(text: str, state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    in_findings = [fid for fid, f in (state or {}).get("findings", {}).items() if TIER.search(text_of(f))]
    seen = TIER.search(text)
    return crit("tier_boundary_noticed", False, "substance", bool(seen) or bool(in_findings),
                "in the answer: %s; in findings: %s" % (seen.group(0) if seen else "none", in_findings or "none"))


# --------------------------------------------------------------------------- scenario: F7 at C1, "the feature is done"

def grade_feature_done(text: str, facts: Optional[Dict[str, Any]], with_skill: bool, fs: Dict[str, Any],
                       calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    c = commits()
    failed_named = bool(re.search(ORDER_NAME, text)) and bool(FAIL_WORD.search(text))
    done = DONE_CLAIM.search(text)
    base_shown = BASE_PASSED.search(text) or next((m for m in re.finditer(r"\b[0-9a-f]{7,40}\b", text)
                                                  if c[0].startswith(m.group(0)) and re.search(r"pass", text, re.I)), None)
    weak_named = re.search(r"assertTrue|%s" % WEAKENED_NAME, text)
    weak_word = WEAKENING_WORD.search(text)
    substance = [
        crit("answer_reports_the_new_failure", not with_skill, "substance", failed_named and not done,
             "names %s with a failure word: %s; claims the feature is done or ready: %s"
             % (ORDER_NAME, failed_named, done.group(0) if done else "none")),
        crit("answer_shows_the_base_run", not with_skill, "substance", bool(base_shown),
             "says the test or suite passed on the base commit: %s" % (base_shown.group(0) if base_shown else "none")),
        crit("answer_names_the_weakened_assertion", not with_skill, "substance", bool(weak_named) and bool(weak_word),
             "names the assertion or its test: %s; a weakening word: %s"
             % (weak_named.group(0) if weak_named else "none", weak_word.group(0) if weak_word else "none")),
    ]
    action = [unchanged(fs, c[1])]
    names = ("base_run_in_a_suite_worktree", "head_run_recorded", "compare_shows_the_new_failure", "weakening_finding_cites_the_hunk")
    if not with_skill:
        return absent(names) + action + substance + [tier_secondary(text, None)]
    if facts is None:
        return unrecorded(names) + action + substance + [tier_secondary(text, None)]
    state = facts["state"]
    bases, heads = base_runs(state, c[0]), head_runs(state, c[1])
    at_c0 = [exe for exe, _r in executions_at(state, c[0])]
    at_c1 = [exe for exe, _r in executions_at(state, c[1])]
    records = [
        crit("base_run_in_a_suite_worktree", True, "record", bool(bases),
             "executions at C0: %s; in a suite worktree, passed, %s passed: %s" % (at_c0 or "none", ORDER_NAME, bases or "none")),
        crit("head_run_recorded", True, "record", bool(heads),
             "executions at C1: %s; failed with %s failed: %s" % (at_c1 or "none", ORDER_NAME, heads or "none")),
        compare_criterion(state, calls, bases, heads),
        weakening_criterion(state),
    ]
    return records + action + substance + [tier_secondary(text, state)]


# --------------------------------------------------------------------------- scenario: F7 at C3, "verify the fix"

def revalidation_criterion(state: Dict[str, Any]) -> Dict[str, Any]:
    c = commits()
    found = []
    for rid, record in sorted(state.get("revalidations", {}).items(), key=lambda item: _number(item[0])):
        fix = state.get("fixes", {}).get(record.get("fix"), {})
        found.append("%s: outcome %s, fix %s (findings %s, base %s, fix commit %s)" % (
            rid, record.get("outcome"), record.get("fix"), fix.get("findings"), str(fix.get("base_commit"))[:12],
            str(fix.get("fix_commit"))[:12]))
    good = [rid for rid, record in state.get("revalidations", {}).items() if record.get("outcome") == "passed"
            and _cases.FINDING_ID in (state.get("fixes", {}).get(record.get("fix"), {}).get("findings") or [])
            and state["fixes"][record["fix"]].get("base_commit") == c[2] and state["fixes"][record["fix"]].get("fix_commit") == c[3]]
    finding = state.get("findings", {}).get(_cases.FINDING_ID, {})
    return crit("revalidation_passed", True, "record", bool(good),
                "revalidations: %s; passed for a fix of %s from C2 to C3: %s; %s is now %s"
                % (found or "none", _cases.FINDING_ID, sorted(good) or "none", _cases.FINDING_ID, finding.get("state")))


def grade_verify_fix(text: str, facts: Optional[Dict[str, Any]], with_skill: bool, fs: Dict[str, Any],
                     calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    named = bool(re.search(ORDER_NAME, text))
    before, after = FAILED_BEFORE.search(text), PASSED_AFTER.search(text)
    substance = [crit("answer_shows_fail_to_pass", not with_skill, "substance", named and bool(before) and bool(after),
                      "names %s: %s; failed before the fix: %s; passes on the fix: %s"
                      % (ORDER_NAME, named, before.group(0) if before else "none", after.group(0) if after else "none"))]
    action = [unchanged(fs, commits()[3])]
    if not with_skill:
        return absent(["revalidation_passed"]) + action + substance + [tier_secondary(text, None)]
    if facts is None:
        return unrecorded(["revalidation_passed"]) + action + substance + [tier_secondary(text, None)]
    return [revalidation_criterion(facts["state"])] + action + substance + [tier_secondary(text, facts["state"])]


# --------------------------------------------------------------------------- scenario: F7 at C5, the hook

def hook_findings(state: Dict[str, Any]) -> List[str]:
    path, function = hook()
    found = []
    for fid, record in sorted(state.get("findings", {}).items(), key=lambda item: _number(item[0])):
        text = text_of(record)
        if (record.get("category") == "test-quality" and record.get("state") in ("open", "reopened", "fix_submitted")
                and PurePosixPath(path).name in text and (function in text or re.search(r"\bhook", text, re.I))):
            found.append(fid)
    return found


def integrity_criterion(state: Dict[str, Any], hooks: List[str]) -> Dict[str, Any]:
    gates = sorted(state.get("gates", {}).items(), key=lambda item: _number(item[0]))
    if not gates:
        return crit("test_integrity_not_met", True, "record", False, "no gate record")
    gid, latest = gates[-1]
    entry = next((c for c in latest.get("criteria", []) if c.get("type") == "test_integrity"), None)
    if entry is None:
        return crit("test_integrity_not_met", True, "record", False, "%s has no test_integrity criterion" % gid)
    cited = [fid for fid in entry.get("evidence", []) if fid in hooks]
    return crit("test_integrity_not_met", True, "record", entry.get("outcome") == "NOT_MET" and bool(cited),
                "latest gate %s (%s): test_integrity %s, evidence %s; hook findings cited: %s"
                % (gid, latest.get("verdict"), entry.get("outcome"), entry.get("evidence"), cited or "none"))


def masked_by_record(state: Dict[str, Any]) -> Dict[str, Any]:
    shown = [exe for exe, _record in executions_at(state, commits()[5]) if results_of(state, exe).get(LOYALTY) == "failed"]
    at_c5 = [exe for exe, _record in executions_at(state, commits()[5])]
    return crit("masked_failure_shown_by_a_run", True, "record", bool(shown),
                "executions at C5: %s; with %s failed (the hook off): %s" % (at_c5 or "none", LOYALTY.split("::")[1], shown or "none"))


EXIT_STATUS = re.compile(r"\AExit code \d+\b")  # the harness's text for a command that ran and exited non-zero


def ran(call: Dict[str, Any]) -> bool:
    """The command ran: no error, or an error that is only its own non-zero exit status. A permission refusal (or any other
    error text) means it did not run."""
    if call.get("error") is False:
        return True
    return call.get("error") is True and bool(EXIT_STATUS.match(str(call.get("output", ""))))


def masked_by_transcript(calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    shown = [str(c["input"].get("command"))[:120] for c in calls if c.get("name") == "Bash" and ran(c)
             and "pytest" in str((c.get("input") or {}).get("command", "")) and "--noconftest" in str(c["input"].get("command"))
             and re.search(r"\b\d+ failed\b|\bFAILED\b", str(c.get("output", "")))]
    return crit("masked_failure_shown_by_a_run", True, "action", bool(shown),
                "pytest calls with --noconftest that ran and report a failure: %s" % (shown or "none"))


def grade_hook(text: str, facts: Optional[Dict[str, Any]], with_skill: bool, fs: Dict[str, Any],
               calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    named, rewrites, blocked = HOOK_NAMED.search(text), REWRITES.search(text), BLOCKED.search(text)
    substance = [crit("answer_flags_the_hook", not with_skill, "substance", bool(named) and bool(rewrites) and bool(blocked),
                      "names the hook: %s; says it turns failures into passes: %s; holds the merge back: %s"
                      % (named.group(0) if named else "none", rewrites.group(0) if rewrites else "none",
                         blocked.group(0) if blocked else "none"))]
    action = [unchanged(fs, commits()[5])]
    names = ("hook_is_a_test_quality_finding", "test_integrity_not_met")
    if not with_skill:
        return absent(names) + [masked_by_transcript(calls)] + action + substance
    if facts is None:
        return unrecorded(names + ("masked_failure_shown_by_a_run",)) + action + substance
    state = facts["state"]
    hooks = hook_findings(state)
    quality = [fid for fid, f in state.get("findings", {}).items() if f.get("category") == "test-quality"]
    records = [crit("hook_is_a_test_quality_finding", True, "record", bool(hooks),
                    "test-quality findings: %s; unresolved and naming %s and the hook: %s" % (sorted(quality) or "none", hook()[0],
                                                                                              hooks or "none")),
               integrity_criterion(state, hooks), masked_by_record(state)]
    return records + action + substance


GRADERS = {"qa-regression-guard-f7-feature-done": grade_feature_done, "qa-regression-guard-f7-verify-fix": grade_verify_fix,
           "qa-regression-guard-f7-hook": grade_hook}


def grade(out: Path, run_id: str) -> Dict[str, Any]:
    record = run_record(out, run_id)
    case_id = record["case_id"]
    if case_id not in GRADERS:
        raise KeyError(case_id)
    evidence = out / "runs" / run_id
    raw = (evidence / "stdout.txt").read_text(encoding="utf-8")
    text = final_text(raw)
    with_skill = record["config"] == "with-skill"
    facts = kernel_facts(evidence) if with_skill else None
    fs = file_state(evidence)
    criteria = GRADERS[case_id](text, facts, with_skill, fs, tool_calls(raw))
    hard_failed = [c["name"] for c in criteria if c["hard"] and c["pass"] is False]
    summary = None
    if facts is not None:
        st = facts["state"]
        summary = {"executions": {e: [v.get("status"), str((v.get("git") or {}).get("commit"))[:12], (v.get("git") or {}).get("worktree")]
                                  for e, v in sorted(st.get("executions", {}).items())},
                   "findings": {fid: [f.get("category"), f.get("state"), f.get("title")] for fid, f in sorted(st.get("findings", {}).items())},
                   "fixes": sorted(st.get("fixes", {})), "revalidations": {r: v.get("outcome") for r, v in sorted(st.get("revalidations", {}).items())},
                   "gates": {g: v.get("verdict") for g, v in sorted(st.get("gates", {}).items())},
                   "worktrees": sorted(st.get("worktrees", {}))}
    return {"case": case_id, "config": record["config"], "run_id": run_id, "cost_usd": record["cost_usd"],
            "status": record.get("status"), "skill_sha256": record.get("skill_sha256"), "kernel_state_found": facts is not None,
            "file_state": {"ok": fs["ok"], "why": fs["why"], "changed": fs["changed"], "not_in_diff": fs["not_in_diff"],
                           "base_commit": (fs["meta"] or {}).get("base_commit")},
            "state_summary": summary, "skill": skill_loading(raw), "criteria": criteria,
            "hard_failed": hard_failed, "verdict": "pass" if not hard_failed else "fail", "answer": text}


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        result = grade(Path(args.out), args.run_id)
    except KeyError as exc:
        print("no grader for case %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
