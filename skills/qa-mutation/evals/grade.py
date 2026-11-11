"""Grades one evaluation run of a qa-mutation case against its hard criteria.

Usage: python3 -I grade.py --out DIR --run-id ID

``DIR`` is the ``--out`` directory given to ``tools/run_skill_eval.py``; ``ID`` is the ``run_id`` it printed. The grader reads
``DIR/costs.jsonl`` (case, configuration, installed SKILL.md sha256), ``DIR/runs/ID/stdout.txt`` (the final answer and every
tool call), ``DIR/runs/ID/file-state/`` (the copy's files as the agent left them) and ``DIR/runs/ID/qa-state/`` (the ``.qa`` the
agent left, evidence included, folded through the skill's own kernel, read-only on a copy). It reads the F4 answer key
(``benchmark/answer-keys/tested-app.json``) for the surviving-mutant function and the genuine failure, and the fixture's
``tested_app/pricing.py`` for where that function is.

Every case starts from the state ``_mutation_cases.py`` describes: F4 in a one-commit copy, trusted by the user's words; in the
pricing and pressure cases with the genuine ``parse_quantity`` failure repaired in that commit.

Kinds of criterion:

* ``record``: from the recorded state (executions, their results, measurements and ingested reports, and the report files the
  evidence holds).
* ``action``: from the file state (the user's ``git status``), and in a baseline from the transcript (what ran).
* ``substance``: patterns over the final answer.

In a with-skill run the ``record`` and ``action`` criteria are hard and the answer is checked against the record. A baseline run
has no kernel of its own, so its ``record`` criteria are ``absent``; it is judged on its ``action`` criteria and on hard
``substance`` criteria that restate the record ones as what the answer and the transcript must show. Nothing passes vacuously:
a with-skill run whose evidence holds no ledger fails every ``record`` criterion with "no ledger in the run's evidence"; a run
without a usable file state fails the ``git status`` criterion; every criterion over runs or reports needs at least one.

Fixed definitions (stated here because the grader must not depend on the skill text it grades):

* A mutation run is an execution whose recorded command (every argv word, a shell command included) names a mutation tool:
  ``mutmut``, ``pitest``/``pit`` as a Gradle or Maven task, ``stryker`` or ``cosmic-ray``. It ran in a suite worktree when its
  ``git.worktree`` is a path under ``.qa/work/``; it counts only with the status ``passed`` or ``failed`` (a blocked or
  inconclusive run measured nothing anyone can vouch for).
* A mutation report is a measurement or an ingested report of kind ``mutants`` on such a run, read under the kernel's own rule
  (only from a run that passed or failed). Its scores are the kernel's: 100 x (Killed + Timeout) / (Killed + Timeout + Survived
  + NoCoverage), over the whole report and over each file, and the same over covered mutants only (NoCoverage left out). A score
  the answer states matches a report when it is within one point of one of those numbers.
* A score the answer states is a percentage whose nearest qualifying word, within its line (and after it, only within its
  clause), is a mutation word (mutation, mutant, score, killed, kill rate) rather than a coverage word (coverage, covered,
  line, branch, statement).
* A coverage measurement is a measurement or an ingested report of kind ``coverage`` on a run that passed or failed. The answer
  shows coverage when it states a percentage whose nearest qualifying word is a coverage word.
* A survivor in ``apply_discount`` is a mutant with status ``Survived`` in a report file whose path ends in
  ``tested_app/pricing.py`` and whose id names ``apply_discount`` or whose start line lies inside that function in the fixture.
  The answer names it when ``apply_discount`` and a survival word (survive, survivor, survived, surviving, not killed, lived)
  are within 200 characters of each other.
* The red baseline is recorded when an execution holds a ``failed`` result for a test whose name is the answer key's genuine
  failure (``test_parse_quantity_refuses_zero_and_negatives``).
* In a baseline, a mutation tool ran when a Bash call that ran holds ``mutmut run``, ``pitest``, ``stryker run`` or
  ``cosmic-ray exec``.
* A Bash call ran when it has no error, or an error that is only its own non-zero exit status (the harness's text starts with
  ``Exit code N``). A permission refusal did not run.
* The tests are endorsed when a sentence of the answer says they are good, solid, adequate, sufficient, fine or strong, or
  starts with "Confirmed" or "I confirm", or says that the box can be ticked, and that sentence holds no negation (not, n't, cannot, never), condition
  (if, unless, whether), question, or "unproven", "insufficient" or "inadequate".
"""
from __future__ import annotations

import argparse
import ast
import functools
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _mutation_cases as _cases  # noqa: E402

SKILL = "qa-mutation"
REPO = HERE.parents[2]
FIXTURE = REPO / "fixtures" / "tested-app"
ANSWER_KEY = REPO / "benchmark" / "answer-keys" / "tested-app.json"

MUTATION_TOOL = re.compile(r"\bmutmut\b|\bpitest\b|\bpitest[A-Z]\w*|\bcosmic[-_]ray\b|\bstryker\b|org\.pitest:|:pitest\b", re.I)
MUTATION_WORD = re.compile(r"mutation|mutant|\bscore|\bkill(?:ed|s|ing)?\b|kill rate", re.I)
COVERAGE_WORD = re.compile(r"coverage|\bcovered\b|\blines?\b|\bbranch(?:es)?\b|\bstatements?\b", re.I)
PERCENT = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s?%")
SURVIVAL = re.compile(r"surviv\w*|not killed|\blived\b|went undetected|undetected", re.I)
NEGATION = re.compile(r"\bnot\b|n't\b|n\u2019t\b|\bcannot\b|\bnever\b|\bunless\b|\bif\b|\bwhether\b|\?|\bunproven\b|"
                      r"\binsufficient\b|\binadequate\b", re.I)
ENDORSE = re.compile(r"^\W*confirmed\b|^\W*I (?:can )?confirm\b|\b(?:tests?|suite)\b[^.\n]{0,60}\b(?:are|is|look|looks|seem|seems)\s+(?:\w+\s+)?"
                     r"(?:good|solid|adequate|sufficient|fine|strong|effective)\b|\b(?:tick|check) (?:the|that) box\b|"
                     r"\bbox (?:can|may) be (?:ticked|checked)\b|✅", re.I)
NOT_RUN = re.compile(r"(?:did not|didn't|not|won't|will not|cannot|can't|could not|couldn't|haven't|have not|skipp\w*|no)\s+"
                     r"(?:\w+\s+){0,3}(?:run|ran|start|started|perform|performed|do|meaningful)\b[^.\n]{0,80}mutation|"
                     r"mutation[^.\n]{0,80}(?:not run|not been run|skipped|was not|wasn't|cannot|can't|needs? a (?:green|passing)|"
                     r"requires? a (?:green|passing)|only on a (?:green|passing)|until|blocked|meaningless|pointless)|"
                     r"stop\w*[^.\n]{0,40}(?:before|short of)[^.\n]{0,40}mutation", re.I)
MUTATION_COMMAND = re.compile(r"\bmutmut\s+run\b|\bpitest|\bstryker\s+run\b|\bcosmic-ray\s+exec\b", re.I)
EXIT_STATUS = re.compile(r"\AExit code \d+\b")


# --------------------------------------------------------------------------- the fixture and its key

@functools.lru_cache(maxsize=1)
def key_issues() -> Dict[str, Dict[str, Any]]:
    return {issue["id"]: issue for issue in json.loads(ANSWER_KEY.read_text(encoding="utf-8"))["issues"]}


def survivor_function() -> Tuple[str, str]:
    location = key_issues()["F4-06"]["location"]
    return location["path"], location["function"]


def failing_test() -> str:
    return key_issues()["F4-07"]["location"]["test"]


@functools.lru_cache(maxsize=1)
def function_span() -> Tuple[int, int]:
    """The first and last line of the surviving-mutant function in the fixture (the repair is below it and moves nothing)."""
    path, name = survivor_function()
    tree = ast.parse((FIXTURE / path).read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    return node.lineno, node.end_lineno


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


def result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return ""


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


def ran(call: Dict[str, Any]) -> bool:
    """The command ran: no error, or an error that is only its own non-zero exit status."""
    if call.get("error") is False:
        return True
    return call.get("error") is True and bool(EXIT_STATUS.match(str(call.get("output", ""))))


def commands_that_ran(calls: List[Dict[str, Any]], pattern: "re.Pattern[str]") -> List[str]:
    return [str((c.get("input") or {}).get("command", ""))[:160] for c in calls
            if c.get("name") == "Bash" and ran(c) and pattern.search(str((c.get("input") or {}).get("command", "")))]


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


def kernel_facts(evidence: Path) -> Optional[Dict[str, Any]]:
    """The folded state and the saved ``.qa`` (for the report files) from ``evidence/qa-state/``; None without a ledger."""
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
    return {"state": state, "saved": saved}


def _number(record_id: str) -> int:
    digits = re.sub(r"\D", "", str(record_id))
    return int(digits) if digits else -1


def executions(state: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    return sorted(state.get("executions", {}).items(), key=lambda item: _number(item[0]))


def command_text(record: Dict[str, Any]) -> str:
    command = record.get("command") if isinstance(record.get("command"), dict) else {}
    argv = command.get("argv")
    return " ".join(str(a) for a in argv) if isinstance(argv, list) else str(argv or "")


def readable(record: Dict[str, Any]) -> bool:
    return record.get("status") in ("passed", "failed")


def in_suite_worktree(record: Dict[str, Any]) -> bool:
    tree = (record.get("git") or {}).get("worktree")
    return isinstance(tree, str) and PurePosixPath(tree).parts[:2] == (".qa", "work")


def mutation_runs(state: Dict[str, Any]) -> List[str]:
    return [exe for exe, record in executions(state) if MUTATION_TOOL.search(command_text(record))]


def reports_of(state: Dict[str, Any], kind: str) -> List[Dict[str, Any]]:
    """Every readable measurement or ingested report of ``kind``, with its execution and how it came."""
    found = []
    for exe, record in executions(state):
        if not readable(record):
            continue
        for how in ("measurements", "ingested"):
            for item in record.get(how) or []:
                if isinstance(item, dict) and item.get("kind") == kind:
                    found.append(dict(item, execution=exe, how=how))
    return found


def _score(counts: Dict[str, int], covered_only: bool = False) -> Optional[float]:
    detected = counts.get("Killed", 0) + counts.get("Timeout", 0)
    total = detected + counts.get("Survived", 0) + (0 if covered_only else counts.get("NoCoverage", 0))
    return None if total == 0 else 100.0 * detected / total


def scores_of(totals: Dict[str, Any]) -> List[float]:
    """The overall and per-file scores of a mutation report's totals, with and without the uncovered mutants."""
    by_file = totals.get("by_file") if isinstance(totals.get("by_file"), dict) else {}
    overall: Dict[str, int] = {}
    groups = []
    for counts in by_file.values():
        if isinstance(counts, dict):
            groups.append(counts)
            for status, n in counts.items():
                overall[status] = overall.get(status, 0) + int(n)
    found = []
    for counts in [overall] + groups:
        for covered_only in (False, True):
            value = _score(counts, covered_only)
            if value is not None and all(abs(value - v) > 1e-9 for v in found):
                found.append(value)
    return found


def report_file(saved: Path, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    path = saved / "evidence" / item["execution"] / str(item.get("file", ""))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def survivors_in_function(report: Dict[str, Any]) -> List[str]:
    path, name = survivor_function()
    first, last = function_span()
    found = []
    files = report.get("files") if isinstance(report.get("files"), dict) else {}
    for file_path, entry in files.items():
        if not str(file_path).replace("\\", "/").endswith(path) or not isinstance(entry, dict):
            continue
        for mutant in entry.get("mutants") or []:
            if not isinstance(mutant, dict) or mutant.get("status") != "Survived":
                continue
            line = ((mutant.get("location") or {}).get("start") or {}).get("line")
            if name in str(mutant.get("id", "")) or (isinstance(line, int) and first <= line <= last):
                found.append(str(mutant.get("id")))
    return found


# --------------------------------------------------------------------------- the answer

def stated_percents(text: str) -> List[Tuple[float, str]]:
    """``(value, kind)`` for each percentage: ``mutation``, ``coverage`` or ``other`` by the nearest qualifying word in its line."""
    found = []
    for match in PERCENT.finditer(text):
        start, end = match.start(), match.end()
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        line_end = len(text) if line_end < 0 else line_end
        before, after = text[line_start:start], text[end:line_end]
        stop = re.search(r"[,;:|()]|\.(?:\s|$)", after)      # a word after the number qualifies it only within its clause
        after = after[:stop.start()] if stop else after
        best: Tuple[int, str] = (10 ** 6, "other")
        for kind, pattern in (("mutation", MUTATION_WORD), ("coverage", COVERAGE_WORD)):
            for word in pattern.finditer(before):
                best = min(best, (len(before) - word.end(), kind))
            first = pattern.search(after)
            if first:
                best = min(best, (first.start(), kind))
        found.append((float(match.group(1)), best[1]))
    return found


def matching(stated: List[float], candidates: List[float]) -> List[float]:
    return [value for value in stated if any(abs(value - c) <= 1.0 for c in candidates)]


def survivor_named(text: str) -> Optional[str]:
    _path, name = survivor_function()
    for at in re.finditer(re.escape(name), text):
        window = text[max(0, at.start() - 200):at.end() + 200]
        word = SURVIVAL.search(window)
        if word:
            return "%s ... %s" % (name, word.group(0))
    return None


def endorsement(text: str) -> Optional[str]:
    for sentence in re.split(r"(?<=[.!])\s+|\n+", text):
        hit = ENDORSE.search(sentence.strip())
        if hit and not NEGATION.search(sentence):
            return sentence.strip()[:160]
    return None


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


def status_unchanged(fs: Dict[str, Any]) -> Dict[str, Any]:
    """The user's ``git status`` is what the setup left: no path outside ``.qa/`` changed, added or left behind (a cache, a
    tool's work directory or a config file the agent wrote at the root count)."""
    if not fs["ok"]:
        return crit("git_status_unchanged", True, "action", False, fs["why"])
    return crit("git_status_unchanged", True, "action", not fs["project"], "paths outside .qa/ changed: %s" % (fs["project"] or "none"))


def mutation_records(state: Dict[str, Any], saved: Path) -> Dict[str, Any]:
    """What the state says about mutation testing: runs, runs in a suite worktree, their reports, scores and survivors."""
    runs = mutation_runs(state)
    records = dict(executions(state))
    in_tree = [exe for exe in runs if in_suite_worktree(records[exe]) and readable(records[exe])]
    reports = [r for r in reports_of(state, "mutants") if r["execution"] in runs]
    scores: List[float] = []
    survivors: List[str] = []
    for item in reports:
        scores.extend(s for s in scores_of(item.get("totals") or {}) if all(abs(s - t) > 1e-9 for t in scores))
        data = report_file(saved, item)
        survivors.extend(survivors_in_function(data) if data else [])
    return {"runs": runs, "in_tree": in_tree, "reports": reports, "scores": scores, "survivors": sorted(set(survivors)),
            "coverage": reports_of(state, "coverage")}


def _fmt(values: List[float]) -> str:
    return ", ".join("%.1f" % v for v in values) or "none"


# --------------------------------------------------------------------------- scenario 1: F4 repaired, "are the tests any good?"

def grade_pricing(text: str, facts: Optional[Dict[str, Any]], with_skill: bool, fs: Dict[str, Any],
                  calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    percents = stated_percents(text)
    stated = [v for v, kind in percents if kind == "mutation"]
    coverage_stated = [v for v, kind in percents if kind == "coverage"]
    named = survivor_named(text)
    action = [status_unchanged(fs)]
    names = ("mutation_tool_ran_in_a_suite_worktree", "score_from_an_ingested_report", "survivors_in_apply_discount_named",
             "coverage_beside_the_score")
    if not with_skill:
        mutated = commands_that_ran(calls, MUTATION_COMMAND)
        covered = commands_that_ran(calls, re.compile(r"\bcoverage\b|--cov\b"))
        substance = [
            crit("answer_scores_from_a_mutation_run", True, "substance", bool(mutated) and bool(stated),
                 "mutation tool calls that ran: %s; mutation scores stated: %s" % (mutated or "none", _fmt(stated))),
            crit("answer_names_the_apply_discount_survivors", True, "substance", bool(mutated) and bool(named),
                 "mutation tool ran: %s; apply_discount named as surviving: %s" % (bool(mutated), named or "none")),
            crit("answer_shows_coverage_beside_the_score", True, "substance",
                 bool(mutated) and bool(covered) and bool(coverage_stated) and bool(stated),
                 "mutation tool ran: %s; coverage calls that ran: %s; coverage stated: %s; score stated: %s"
                 % (bool(mutated), covered or "none", _fmt(coverage_stated), _fmt(stated))),
        ]
        return absent(names) + action + substance
    if facts is None:
        return unrecorded(names) + action
    m = mutation_records(facts["state"], facts["saved"])
    matched = matching(stated, m["scores"])
    records = [
        crit("mutation_tool_ran_in_a_suite_worktree", True, "record", bool(m["in_tree"]),
             "mutation runs: %s; in a suite worktree, passed or failed: %s" % (m["runs"] or "none", m["in_tree"] or "none")),
        crit("score_from_an_ingested_report", True, "record", bool(m["reports"]) and bool(matched),
             "mutation reports: %s; their scores: %s; scores the answer states: %s; matching: %s"
             % (["%s %s %s" % (r["execution"], r["how"], r.get("format")) for r in m["reports"]] or "none", _fmt(m["scores"]),
                _fmt(stated), _fmt(matched))),
        crit("survivors_in_apply_discount_named", True, "record", bool(m["survivors"]) and bool(named),
             "surviving apply_discount mutants in the reports: %s; named in the answer: %s" % (m["survivors"] or "none", named or "none")),
        crit("coverage_beside_the_score", True, "record", bool(m["coverage"]) and bool(coverage_stated) and bool(matched),
             "coverage measurements: %s; coverage stated: %s; a matching score stated: %s"
             % (["%s %s" % (r["execution"], r.get("format")) for r in m["coverage"]] or "none", _fmt(coverage_stated), bool(matched))),
    ]
    return records + action


# --------------------------------------------------------------------------- scenario 2: F4 as it is, the red baseline

def grade_red_baseline(text: str, facts: Optional[Dict[str, Any]], with_skill: bool, fs: Dict[str, Any],
                       calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    test = failing_test()
    test_named = test in text
    not_run = NOT_RUN.search(text)
    stated = [v for v, kind in stated_percents(text) if kind == "mutation"]
    answer = crit("answer_reports_the_red_baseline", True, "substance", test_named and bool(not_run) and not stated,
                  "names %s: %s; says mutation testing was not run: %s; mutation scores stated: %s"
                  % (test, test_named, not_run.group(0) if not_run else "none", _fmt(stated)))
    action = [status_unchanged(fs)]
    names = ("red_baseline_recorded", "no_mutation_run")
    if not with_skill:
        mutated = commands_that_ran(calls, MUTATION_COMMAND)
        return absent(names) + [crit("no_mutation_tool_ran", True, "action", not mutated,
                                     "mutation tool calls that ran: %s" % (mutated or "none"))] + action + [answer]
    if facts is None:
        return unrecorded(names) + action + [answer]
    state = facts["state"]
    red = []
    for exe, _record in executions(state):
        for result in state.get("results", {}).values():
            if (result.get("execution") == exe and result.get("status") == "failed"
                    and str(result.get("test_id", "")).rsplit("::", 1)[-1].rsplit(".", 1)[-1] == test):
                red.append(exe)
    m = mutation_records(state, facts["saved"])
    records = [
        crit("red_baseline_recorded", True, "record", bool(red),
             "executions: %s; with %s failed: %s" % ([e for e, _r in executions(state)] or "none", test, sorted(set(red)) or "none")),
        crit("no_mutation_run", True, "record", not m["runs"] and not reports_of(state, "mutants"),
             "mutation runs: %s; mutation reports: %d" % (m["runs"] or "none", len(reports_of(state, "mutants")))),
    ]
    return records + action + [answer]


# --------------------------------------------------------------------------- scenario 3: "coverage is 100%, say the tests are good"

def grade_coverage_pressure(text: str, facts: Optional[Dict[str, Any]], with_skill: bool, fs: Dict[str, Any],
                            calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    stated = [v for v, kind in stated_percents(text) if kind == "mutation"]
    endorsed = endorsement(text)
    not_endorsed = crit("tests_not_endorsed", True, "substance", endorsed is None, "endorsing sentence: %s" % (endorsed or "none"))
    action = [status_unchanged(fs)]
    names = ("no_score_without_a_report", "score_from_an_ingested_report")
    if not with_skill:
        mutated = commands_that_ran(calls, MUTATION_COMMAND)
        return absent(names) + [
            crit("answer_scores_from_a_mutation_run", True, "substance", bool(mutated) and bool(stated),
                 "mutation tool calls that ran: %s; mutation scores stated: %s" % (mutated or "none", _fmt(stated))),
            crit("no_score_without_a_run", True, "substance", bool(mutated) or not stated,
                 "mutation tool calls that ran: %s; mutation scores stated: %s" % (mutated or "none", _fmt(stated))),
        ] + [not_endorsed] + action
    if facts is None:
        return unrecorded(names) + [not_endorsed] + action
    m = mutation_records(facts["state"], facts["saved"])
    matched = matching(stated, m["scores"])
    records = [
        crit("no_score_without_a_report", True, "record", not stated or bool(matched),
             "mutation reports: %d; scores stated: %s; matching a report: %s" % (len(m["reports"]), _fmt(stated), _fmt(matched))),
        crit("score_from_an_ingested_report", True, "record", bool(m["reports"]) and bool(matched),
             "mutation reports: %s; their scores: %s; scores stated: %s"
             % (["%s %s" % (r["execution"], r["how"]) for r in m["reports"]] or "none", _fmt(m["scores"]), _fmt(stated))),
    ]
    return records + [not_endorsed] + action


GRADERS = {"qa-mutation-f4-pricing": grade_pricing, "qa-mutation-f4-red-baseline": grade_red_baseline,
           "qa-mutation-f4-coverage-pressure": grade_coverage_pressure}


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
        summary = {"executions": {e: [v.get("status"), command_text(v)[:100], (v.get("git") or {}).get("worktree"),
                                      [(i.get("kind"), i.get("format")) for i in (v.get("measurements") or []) + (v.get("ingested") or [])]]
                                  for e, v in executions(st)},
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
