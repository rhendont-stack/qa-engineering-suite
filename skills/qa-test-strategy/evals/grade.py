"""Grades one evaluation run of a qa-test-strategy case against its hard criteria.

Usage: python3 -I grade.py --out DIR --run-id ID

``DIR`` is the ``--out`` directory given to ``tools/run_skill_eval.py``; ``ID`` is the ``run_id`` it printed. The grader reads
``DIR/costs.jsonl`` (case and configuration), ``DIR/runs/ID/stdout.txt`` (the final answer and every tool call) and, for a
with-skill run, ``DIR/runs/ID/qa-state/``: the ``.qa`` the agent left behind. It folds that state through the skill's own kernel,
read-only on a copy, and reads the raw ledger lines and ``trace/matrix.json`` beside it.

Every case starts from the same prepared state (``setup_requirements.py``): the ten requirements of the F5 fixture and their
twelve acceptance criteria, recorded through the kernel. The IDs are fixed (``_requirements.expected_ids``).

Kinds of criterion:

* ``record``: from the recorded state and ledger (risks, test cases, links, the matrix, gate proposals).
* ``action``: from the tool calls in the transcript: which files the agent wrote, and where.
* ``substance``: patterns over the final answer.

In a with-skill run the ``record`` and ``action`` criteria are the hard ones; the ``substance`` ones are a secondary signal,
reported with ``hard: false``, except where a scenario's criterion can only be seen in the answer (the hand-off), which is hard
in both arms. A baseline run has no kernel, so its ``record`` criteria are reported as ``absent`` and it is judged on its
``action`` criteria and on ``substance`` criteria that restate the record ones as what the answer itself must show. A with-skill
run that left no ledger (no ``init`` and no prepared state, or a state the kernel could not fold) fails every ``record``
criterion with "no ledger in the run's evidence"; it is never treated as absent. Every record criterion needs at least one
record of its kind: none is ever met by an empty state.

Fixed definitions (stated here because the grader must not depend on the skill text it grades):

* A risk is critical when likelihood x impact is 15 or more, or its impact is 5, on the 1-5 scales the kernel enforces.
* A test case is an orphan when no ``verified_by`` link reaches it from a recorded criterion and no ``mitigated_by`` link
  reaches it from a recorded risk.
* A written file is a successful ``Write``, ``Edit``, ``MultiEdit`` or ``NotebookEdit`` call (a result that is not an error, or
  no result), a successful Bash command that redirects, tees, copies, moves or touches into a path (a heuristic over the command
  text: every argument of ``cp``, ``mv`` and ``touch`` counts), or a ``file.created`` / ``file.modified`` ledger event. Anything under a ``.qa`` directory is the kernel's workspace, not the
  project. A test file is one whose name is ``test_*.py``, ``*_test.py``, ``conftest.py`` or ``*.test.*`` / ``*.spec.*``, or
  that sits under a ``tests``, ``test``, ``__tests__`` or ``spec`` directory.

Prints one JSON object. Exit 0 when every hard criterion that applies to the run holds, 1 otherwise, 2 on bad input.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Set

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _requirements  # noqa: E402

SKILL = "qa-test-strategy"
HANDOFF_SKILL = "qa-test-generation"
REPO = HERE.parents[2]
REQ_IDS, AC_IDS = _requirements.expected_ids()

CRITICAL_SCORE = 15
CRITICAL_IMPACT = 5

WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
TEST_DIRS = ("tests", "test", "__tests__", "spec")
TEST_NAME = re.compile(r"^(?:test_.*\.py|.*_test\.py|conftest\.py|.*\.(?:test|spec)\.[A-Za-z0-9]+)$")
_TOKEN = r"([^\s;|&<>()'\"]+)"
REDIRECT = re.compile(r"(?<![<>&0-9])(?:[0-9]?>>?|&>>?)\s*" + _TOKEN)
TEE = re.compile(r"\btee\s+(?:-\w+\s+)*" + _TOKEN)
FILE_COMMAND = re.compile(r"(?:^|[;&|(]\s*|\s)(?:cp|mv|touch)((?:\s+[^\s;|&<>()]+)+)")

LIKELIHOOD_WORD = re.compile(r"\blikelihood\b|\bprobability\b", re.I)
IMPACT_WORD = re.compile(r"\bimpact\b|\bseverity\b|\bconsequence\b", re.I)
_N = r"\**\s*[1-5]\b"
RATING = re.compile(
    r"\b[1-5]\s*(?:/\s*5\s*)?[x×*·]\s*[1-5]\b"                                                    # 4 x 5, 3/5 × 4
    + r"|\bL\s*[:=]?\s*[1-5]\s*(?:[,/;x×*·]|\band\b)?\s*I\s*[:=]?\s*[1-5]\b"                        # L4 I5, L3×I4
    + r"|\b(?:likelihood|probability|impact|severity)\b\**\s*(?:\([^)]{0,12}\))?\s*[:=]?\s*" + _N  # likelihood: 4, impact 5
    + r"|\|\s*\**[1-5]\**(?:\s*/\s*5)?\s*\|\s*\**[1-5]\**(?:\s*/\s*5)?\s*\|", re.I)                # | 4 | 5 | in a table
TECHNIQUE = re.compile(
    r"equivalence[- ](?:class|partition)\w*|boundary[- ]value|decision[- ]table|state[- ]transition|pairwise|combinatorial"
    + r"|classification[- ]tree|cause[- ](?:and[- ])?effect|use[- ]case|scenario[- ](?:based|testing)|error[- ]guessing"
    + r"|exploratory|statement[- ]coverage|branch[- ]coverage|decision[- ]coverage|property[- ]based|metamorphic", re.I)
JUSTIFY_WORD = re.compile(r"\bbecause\b|\bsince\b|\bso that\b|\bto catch\b|\bto cover\b|\bwhy\b|\breason\b|\bjustif\w+|\bgiven that\b", re.I)
AC_ID = re.compile(r"(?<![\w-])AC-[0-9]{3,}\.[0-9]+(?!\w|\.[0-9])")
HANDOFF = re.compile(r"(?<![\w-])%s(?![\w-])" % re.escape(HANDOFF_SKILL))


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
    """Every tool call with its result: ``{name, input, error}``; ``error`` is None when no result was seen."""
    calls: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for event in events(stdout):
        message = event.get("message") if event.get("type") in ("assistant", "user") else None
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                calls[str(block.get("id"))] = {"name": block.get("name"), "input": block.get("input", {}), "error": None}
                order.append(str(block.get("id")))
            elif block.get("type") == "tool_result" and str(block.get("tool_use_id")) in calls:
                calls[str(block.get("tool_use_id"))]["error"] = bool(block.get("is_error"))
    return [calls[i] for i in order]


def skill_loading(stdout: str) -> Dict[str, Any]:
    """Whether the skill was listed at init and the Skill tool was called for it. ``used`` needs both."""
    evs = events(stdout)
    init = next((e for e in evs if e.get("type") == "system" and e.get("subtype") == "init"), {})
    calls = [(c["name"], json.dumps(c["input"])) for c in tool_calls(stdout)]
    listed, called = SKILL in (init.get("skills") or []), any(n == "Skill" and SKILL in i for n, i in calls)
    return {"api_key_source": init.get("apiKeySource"), "listed_at_init": listed, "skill_tool_called": called, "used": listed and called,
            "skill_file_read": any(n == "Read" and SKILL + "/SKILL.md" in i for n, i in calls),
            "kernel_called": any(n == "Bash" and "qa.py" in i for n, i in calls)}


def run_record(out: Path, run_id: str) -> Dict[str, Any]:
    for line in (out / "costs.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("run_id") == run_id and record.get("event") == "end":
            return record
    raise SystemExit("no end record for run %s in %s" % (run_id, out / "costs.jsonl"))


def launcher() -> Path:
    path = HERE.parent / "scripts" / "qa.py"
    return path if path.is_file() else REPO / "shared" / "launcher" / "qa.py"


def ledger_events(saved: Path) -> List[Dict[str, Any]]:
    """Every line of every chain in the saved ledger, as recorded (MACs are not checked: the key is not in the snapshot)."""
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
    """The folded state, the raw ledger events and the saved matrix from ``evidence/qa-state/``; None without a ledger."""
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
    matrix = None
    path = saved / "trace" / "matrix.json"
    if path.is_file():
        try:
            matrix = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            matrix = None
    return {"state": state, "events": ledger_events(saved), "matrix": matrix}


def crit(name: str, hard: bool, kind: str, ok: Optional[bool], detail: str) -> Dict[str, Any]:
    """``kind`` is ``record``, ``action`` or ``substance``. ``ok`` None means absent (no kernel in a baseline run)."""
    return {"name": name, "hard": hard, "kind": kind, "pass": ok, "detail": detail}


def absent(names: Iterable[str], kind: str = "record") -> List[Dict[str, Any]]:
    return [crit(name, True, kind, None, "no kernel in a baseline run") for name in names]


def unrecorded(names: Iterable[str], kind: str = "record") -> List[Dict[str, Any]]:
    """A with-skill run that left no ledger (or a state the kernel could not fold): each record criterion fails."""
    return [crit(name, True, kind, False, "no ledger in the run's evidence") for name in names]


# --------------------------------------------------------------------------- record helpers

def is_critical(risk: Dict[str, Any]) -> bool:
    likelihood, impact = risk.get("likelihood"), risk.get("impact")
    if not isinstance(likelihood, int) or not isinstance(impact, int):
        return False
    return likelihood * impact >= CRITICAL_SCORE or impact >= CRITICAL_IMPACT


def links_of(state: Dict[str, Any], relation: str) -> List[Dict[str, Any]]:
    return [l for l in state.get("links", []) if isinstance(l, dict) and l.get("relation") == relation]


def orphans(state: Dict[str, Any]) -> List[str]:
    """Test cases no recorded criterion verifies and no recorded risk is mitigated by."""
    reached = {l.get("target") for l in links_of(state, "verified_by") if l.get("source") in state["criteria"]}
    reached |= {l.get("target") for l in links_of(state, "mitigated_by") if l.get("source") in state["risks"]}
    return sorted(tc for tc in state["testcases"] if tc not in reached)


def mitigations(state: Dict[str, Any], rsk: str) -> List[str]:
    return sorted({l.get("target") for l in links_of(state, "mitigated_by") if l.get("source") == rsk and l.get("target") in state["testcases"]})


def accepted_gap(risk: Dict[str, Any]) -> bool:
    gap = risk.get("accepted_gap")
    return isinstance(gap, dict) and all(isinstance(gap.get(k), str) and gap[k].strip() for k in ("reason", "accepted_by"))


def rated(risk: Dict[str, Any]) -> bool:
    return all(isinstance(risk.get(k), int) and not isinstance(risk.get(k), bool) and 1 <= risk[k] <= 5 for k in ("likelihood", "impact"))


def risks_rated(state: Dict[str, Any]) -> Dict[str, Any]:
    risks = state["risks"]
    bad = sorted(r for r, rec in risks.items() if not rated(rec))
    return crit("risks_recorded_with_likelihood_and_impact", True, "record", bool(risks) and not bad,
                "risks recorded: %d (%s); without a 1-5 likelihood and impact: %s"
                % (len(risks), ", ".join("%s L%s I%s" % (r, risks[r].get("likelihood"), risks[r].get("impact")) for r in sorted(risks)) or "none",
                   bad or "none"))


def technique_reasons(state: Dict[str, Any]) -> Dict[str, Any]:
    tcs = state["testcases"]
    bad = []
    for tc, rec in sorted(tcs.items()):
        reason, technique = rec.get("technique_reason"), rec.get("technique")
        if not isinstance(reason, str) or not reason.strip() or reason.strip().lower() == str(technique or "").strip().lower():
            bad.append(tc)
    return crit("every_tc_has_technique_reason", True, "record", bool(tcs) and not bad,
                "test cases: %d; without a reason that is more than the technique's name: %s" % (len(tcs), bad or "none"))


def no_orphans(state: Dict[str, Any], matrix: Optional[Dict[str, Any]], name: str = "no_orphan_tc", hard: bool = True) -> Dict[str, Any]:
    tcs = sorted(state["testcases"])
    lost = orphans(state)
    in_matrix = {t.get("id") for t in (matrix or {}).get("testcases", []) if isinstance(t, dict)}
    missing = [tc for tc in tcs if tc not in in_matrix]
    return crit(name, hard, "record", bool(tcs) and not lost and matrix is not None and not missing,
                "test cases: %d; orphans (no criterion or risk links to them): %s; trace matrix saved: %s; test cases absent from it: %s"
                % (len(tcs), lost or "none", matrix is not None, missing or "none"))


def critical_covered(state: Dict[str, Any]) -> Dict[str, Any]:
    risks = state["risks"]
    critical = sorted(r for r, rec in risks.items() if is_critical(rec))
    uncovered = [r for r in critical if not mitigations(state, r) and not accepted_gap(risks[r])]
    how = {r: (mitigations(state, r) or ("accepted gap by %r" % risks[r]["accepted_gap"]["accepted_by"] if accepted_gap(risks[r]) else "nothing"))
           for r in critical}
    return crit("every_critical_risk_mitigated_or_accepted", True, "record", bool(critical) and not uncovered,
                "risks: %d; critical (L x I >= %d or impact %d): %s; covered by: %s; uncovered: %s"
                % (len(risks), CRITICAL_SCORE, CRITICAL_IMPACT, critical or "none", how or "none", uncovered or "none"))


def gate_secondary(facts: Dict[str, Any]) -> Dict[str, Any]:
    accepted = [e.get("id") for e in facts["events"] if e.get("type") == "gate.policy_accepted"]
    proposed = [(e.get("data") or {}).get("criterion") for e in facts["events"] if e.get("type") == "gate.policy_proposed"]
    return crit("no_gate_override_accepted", False, "record", not accepted,
                "gate.policy_accepted events: %s; proposals: %s" % (accepted or "none", proposed or "none"))


def automation_secondary(state: Dict[str, Any]) -> Dict[str, Any]:
    bad = {tc: [a for a in rec.get("automation") or [] if not isinstance(a, str) or "::" not in a
                or "/" in a.split("::", 1)[0] or a.split("::", 1)[0].endswith(".py")]
           for tc, rec in sorted(state["testcases"].items())}
    bad = {tc: v for tc, v in bad.items() if v}
    return crit("automation_ids_in_result_form", False, "record", not bad, "automation entries not in classname::name form: %s" % (bad or "none"))


# --------------------------------------------------------------------------- action helpers

def under_qa(path: str) -> bool:
    return ".qa" in PurePosixPath(path.replace("\\", "/")).parts


def is_test_path(path: str) -> bool:
    parts = PurePosixPath(path.replace("\\", "/")).parts
    return bool(parts) and (bool(TEST_NAME.match(parts[-1])) or any(p in TEST_DIRS for p in parts[:-1]))


def bash_targets(command: str) -> List[str]:
    found = [m.group(1) for m in REDIRECT.finditer(command)] + [m.group(1) for m in TEE.finditer(command)]
    for match in FILE_COMMAND.finditer(command):
        found += [a for a in match.group(1).split() if not a.startswith("-")]
    return [t for t in found if t not in ("/dev/null", "/dev/stdout", "/dev/stderr") and not t.startswith("&")]


def written_files(stdout: str, facts: Optional[Dict[str, Any]] = None) -> List[str]:
    """Project paths the run wrote (outside ``.qa``), from the transcript and, when there is one, the ledger."""
    paths: List[str] = []
    for call in tool_calls(stdout):
        if call["error"] is True:
            continue
        given = call["input"] if isinstance(call["input"], dict) else {}
        if call["name"] in WRITE_TOOLS:
            path = given.get("file_path") or given.get("notebook_path")
            if isinstance(path, str):
                paths.append(path)
        elif call["name"] == "Bash":
            paths += bash_targets(str(given.get("command", "")))
    for event in (facts or {}).get("events", []):
        if event.get("type") in ("file.created", "file.modified"):
            path = (event.get("data") or {}).get("path")
            if isinstance(path, str):
                paths.append(path)
    out: List[str] = []
    for path in paths:
        if not under_qa(path) and path not in out:
            out.append(path)
    return out


def no_test_file(stdout: str, facts: Optional[Dict[str, Any]], hard: bool) -> Dict[str, Any]:
    written = written_files(stdout, facts)
    tests = [p for p in written if is_test_path(p)]
    return crit("no_test_file_written", hard, "action", not tests,
                "test files written: %s; other project files written: %s" % (tests or "none", [p for p in written if p not in tests] or "none"))


# --------------------------------------------------------------------------- substance helpers

def answer_rates(text: str, name: str, hard: bool) -> Dict[str, Any]:
    ok = bool(LIKELIHOOD_WORD.search(text)) and bool(IMPACT_WORD.search(text)) and bool(RATING.search(text))
    found = RATING.search(text)
    return crit(name, hard, "substance", ok,
                "likelihood word: %s; impact word: %s; a 1-5 rating: %s"
                % (bool(LIKELIHOOD_WORD.search(text)), bool(IMPACT_WORD.search(text)), found.group(0) if found else "none"))


def answer_cites_criteria(text: str, hard: bool) -> Dict[str, Any]:
    cited = sorted(set(AC_ID.findall(text)) & set(AC_IDS))
    return crit("answer_ties_tests_to_recorded_criteria", hard, "substance", bool(cited),
                "recorded criteria cited by ID: %s" % (cited or "none"))


def answer_justifies(text: str, hard: bool) -> Dict[str, Any]:
    named = sorted({m.group(0).lower() for m in TECHNIQUE.finditer(text)})
    ok = len(named) >= 2 and bool(JUSTIFY_WORD.search(text))
    return crit("answer_justifies_techniques", hard, "substance", ok,
                "techniques named: %s; a justification word: %s" % (named or "none", bool(JUSTIFY_WORD.search(text))))


# --------------------------------------------------------------------------- scenario: F5, a risk-based plan

def grade_plan(text: str, facts: Optional[Dict[str, Any]], with_skill: bool, stdout: str) -> List[Dict[str, Any]]:
    substance = [answer_rates(text, "answer_rates_risks_by_likelihood_and_impact", not with_skill), answer_cites_criteria(text, not with_skill),
                 answer_justifies(text, not with_skill)]
    action = [no_test_file(stdout, facts, False)]
    names = ("risks_recorded_with_likelihood_and_impact", "every_critical_risk_mitigated_or_accepted", "no_orphan_tc",
             "every_tc_has_technique_reason")
    if not with_skill:
        return absent(names) + action + substance
    if facts is None:
        return unrecorded(names) + action + substance
    state = facts["state"]
    return [risks_rated(state), critical_covered(state), no_orphans(state, facts["matrix"]), technique_reasons(state),
            gate_secondary(facts), automation_secondary(state)] + action + substance


# --------------------------------------------------------------------------- scenario: "skip the risk analysis, just list tests"

def grade_skip_risks(text: str, facts: Optional[Dict[str, Any]], with_skill: bool, stdout: str) -> List[Dict[str, Any]]:
    substance = [answer_rates(text, "answer_still_rates_risks", not with_skill)]
    action = [no_test_file(stdout, facts, False)]
    names = ("risks_recorded_with_likelihood_and_impact",)
    if not with_skill:
        return absent(names) + action + substance
    if facts is None:
        return unrecorded(names) + action + substance
    state = facts["state"]
    return [risks_rated(state), no_orphans(state, facts["matrix"], hard=False), gate_secondary(facts)] + action + substance


# --------------------------------------------------------------------------- scenario: "write the tests now"

def handoff_cases(state: Dict[str, Any]) -> Dict[str, Any]:
    tcs = sorted(state["testcases"])
    lost = orphans(state)
    return crit("handoff_test_cases_recorded", True, "record", bool(tcs) and not lost,
                "test cases recorded for the hand-off: %d; orphans: %s" % (len(tcs), lost or "none"))


def grade_write_tests(text: str, facts: Optional[Dict[str, Any]], with_skill: bool, stdout: str) -> List[Dict[str, Any]]:
    named = HANDOFF.search(text)
    handoff = crit("answer_hands_off_to_test_generation", True, "substance", bool(named),
                   "the answer names %s: %s" % (HANDOFF_SKILL, bool(named)))
    action = [no_test_file(stdout, facts, True)]
    names = ("handoff_test_cases_recorded",)
    if not with_skill:
        return absent(names) + action + [handoff]
    if facts is None:
        return unrecorded(names) + action + [handoff]
    return [handoff_cases(facts["state"]), gate_secondary(facts)] + action + [handoff]


GRADERS = {"qa-test-strategy-f5-risk-based-plan": grade_plan, "qa-test-strategy-skip-risk-analysis": grade_skip_risks,
           "qa-test-strategy-write-tests-now": grade_write_tests}


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    out = Path(args.out)
    record = run_record(out, args.run_id)
    case_id = record["case_id"]
    if case_id not in GRADERS:
        print("no grader for case %s" % case_id, file=sys.stderr)
        return 2
    raw = (out / "runs" / args.run_id / "stdout.txt").read_text(encoding="utf-8")
    text = final_text(raw)
    with_skill = record["config"] == "with-skill"
    facts = kernel_facts(out / "runs" / args.run_id) if with_skill else None
    criteria = GRADERS[case_id](text, facts, with_skill, raw)
    hard_failed = [c["name"] for c in criteria if c["hard"] and c["pass"] is False]
    summary = None
    if facts is not None:
        st = facts["state"]
        summary = {"risks": {r: [v.get("likelihood"), v.get("impact"), "critical" if is_critical(v) else "", "gap" if accepted_gap(v) else ""]
                             for r, v in sorted(st["risks"].items())},
                   "testcases": len(st["testcases"]), "orphans": orphans(st),
                   "links": {rel: len(links_of(st, rel)) for rel in ("verified_by", "mitigated_by", "automated_by")},
                   "gate_proposals": [(e.get("data") or {}).get("criterion") for e in facts["events"] if e.get("type") == "gate.policy_proposed"],
                   "findings": {fid: f.get("category") for fid, f in sorted(st["findings"].items())}}
    print(json.dumps({"case": case_id, "config": record["config"], "run_id": args.run_id, "cost_usd": record["cost_usd"],
                      "status": record.get("status"), "kernel_state_found": facts is not None, "state_summary": summary,
                      "skill": skill_loading(raw), "written_files": written_files(raw, facts), "criteria": criteria,
                      "hard_failed": hard_failed, "verdict": "pass" if not hard_failed else "fail", "answer": text},
                     indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if not hard_failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
