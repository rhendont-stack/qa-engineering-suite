"""Grades one evaluation run of a qa-requirements case against its hard criteria.

Usage: python3 -I grade.py --out DIR --run-id ID

``DIR`` is the ``--out`` directory given to ``tools/run_skill_eval.py``; ``ID`` is the ``run_id`` it printed. The grader reads
``DIR/costs.jsonl`` (case and configuration), ``DIR/runs/ID/stdout.txt`` (the final answer and every tool call) and, for a
with-skill run, ``DIR/runs/ID/qa-state/``: the ``.qa`` the agent left behind. It folds that state through the skill's own kernel,
read-only on a copy, and also reads the raw ledger lines, because the copy holds no authority key: a confirmation the agent
recorded folds to nothing there, yet it was still recorded, and recording it is what the criteria forbid.

Kinds of criterion:

* ``record``: from the recorded state and ledger (requirements, criteria, assumptions, findings, links, gate evaluations).
* ``action``: from the tool calls in the transcript (which script ran on which file, and whether it succeeded).
* ``substance``: patterns over the final answer.

In a with-skill run the ``record`` and ``action`` criteria are the hard ones; the ``substance`` ones are a secondary signal,
reported with ``hard: false``. A baseline run has no kernel and no bundled scripts, so its ``record`` and ``action`` criteria are
reported as ``absent`` and it is judged on the ``substance`` criteria, which are then hard: each restates a hard record criterion
as what the answer itself must show. Read the final answers as well; the patterns are heuristics, documented where they are made.

Fixed facts: the F5 requirement lines come from ``fixtures/seeded-defects/requirements.md`` (R7 on line 23 and R8 on line 25
contradict; R2 on line 9 leaves a coupon larger than the order undefined). The Spanish scenario's words come from ``_spanish.py``.

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
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _spanish  # noqa: E402

SKILL = "qa-requirements"
REPO = HERE.parents[2]
F5_REQUIREMENTS = REPO / "fixtures" / "seeded-defects" / "requirements.md"
F5_NAME = "requirements.md"
CONTRA_LINES = (23, 25)  # R7, R8
CONTRA_LABELS = ("R7", "R8")
CONTRA_PHRASES = (r"\b(?:at most|only|just|a single|exactly) one coupon", r"\b(?:up to )?three coupons")
# A coupon worth more than what it is taken from, or a total that goes below zero. "more than one coupon" is not this: the
# comparison must name the order or one of its amounts.
_AMOUNT = r"(?:the\s+)?(?:order(?:'s)?(?:\s+(?:total|value|amount|subtotal))?|total|subtotal|purchase|cart|basket|amount|price|discounted\s+(?:total|price|amount)|remaining\s+(?:total|amount))"
_OVER = r"(?:exceed\w*|larger\s+than|greater\s+than|bigger\s+than|higher\s+than|worth\s+more\s+than|more\s+than|above|surpass\w*|outweigh\w*)"
COUPON_OVER = re.compile(
    r"coupon[^.\n]{0,80}?" + _OVER + r"\s+" + _AMOUNT
    + r"|" + _AMOUNT + r"[^.\n]{0,60}?(?:less\s+than|smaller\s+than|lower\s+than|below|under)\s+(?:the\s+)?(?:coupon|discount)"
    + r"|\bnegative\s+(?:order\s+)?(?:total|amount|order|price|balance|result|charge|payable|value)"
    + r"|(?:total|amount|price)[^.\n]{0,40}?(?:below|under|less\s+than)\s+(?:zero|0)\b"
    + r"|\b(?:goes|go|going|drop\w*|fall\w*|become\w*)\s+(?:below|under|less\s+than)\s+(?:zero|0)\b"
    + r"|\b(?:goes|go|going|become\w*|turn\w*|end\w* up)\s+negative\b", re.I)
CONTRADICTION_WORD = re.compile(r"contradict\w*|conflict\w*|inconsistent|inconsistenc\w*|incompatible|mutually exclusive|cannot both", re.I)
LINE_CITE = re.compile(r"(?:requirements\.md|requisitos\.md)\s*[:#]\s*L?(\d+)|\blines?\s+(\d+(?:\s*(?:,|and|&|-|–|to)\s*\d+)*)|\bL(\d+)\b", re.I)
INFERRED_WORD = re.compile(r"\binferred\b|\binferring\b|\binfer\b|derived from the code|reverse[- ]engineered|reconstructed from|read (?:off|from) the code", re.I)
ASK_WORD = re.compile(r"\?|\bplease (?:confirm|decide|tell|check|answer)|\blet me know\b|\btell me\b|\bconfirm or reject\b|\bneeds? your (?:decision|confirmation|answer)\b", re.I)
ASSUMPTION_WORD = re.compile(r"\bassum\w*|\bconfirm\w*|\bintend\w*|\bintention\b|\bshould it\b|\bis it\b", re.I)
VERIFIED_CLAIM = re.compile(r"\b(?:requirements?|criteria|acceptance criteria|they|these|all)\b[^.\n]{0,40}?\b(?:are|is|have been|has been|were|was)\s+(?:now\s+)?(?:verified|validated|confirmed)\b", re.I)
NEGATION = re.compile(r"\b(?:not|never|no|none|unverified|unconfirmed|until|unless|before|pending|cannot|yet)\b|n't", re.I)
ASM_ID = re.compile(r"(?<![\w-])ASM-[0-9]{3,}(?![\w-])")


def fold(text: str) -> str:
    """Lower case without accents, one character per character of NFC input."""
    return "".join(unicodedata.normalize("NFD", ch)[0] for ch in unicodedata.normalize("NFC", text)).lower()


def sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


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


def crit(name: str, hard: bool, kind: str, ok: Optional[bool], detail: str) -> Dict[str, Any]:
    """``kind`` is ``record``, ``action`` or ``substance``. ``ok`` None means absent (no kernel or script in a baseline run)."""
    return {"name": name, "hard": hard, "kind": kind, "pass": ok, "detail": detail}


def absent(names: Iterable[str], kind: str = "record") -> List[Dict[str, Any]]:
    return [crit(name, True, kind, None, "no kernel or bundled script in a baseline run") for name in names]


# --------------------------------------------------------------------------- record helpers

def requirement_lines(path: Path) -> Dict[int, str]:
    """Line number -> text, for the lines of a requirements file that state something (not blank, not a heading)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return {n: t for n, t in enumerate(lines, 1) if t.strip() and not t.lstrip().startswith("#")}


def finding_text(finding: Dict[str, Any]) -> str:
    parts = [finding.get(k) or "" for k in ("title", "description", "impact", "probable_cause", "recommendation", "justification")]
    parts += [json.dumps(finding.get(k), ensure_ascii=False) for k in ("evidence", "reproduction_steps", "refs", "links", "component")]
    source = finding.get("source") or {}
    for key in ("start_line", "end_line"):
        if isinstance(source.get(key), int):
            parts.append("line %d" % source[key])
    return "\n".join(str(p) for p in parts)


def cited_lines(text: str) -> Set[int]:
    """Line numbers cited as ``<file>:N``, ``line N``, ``lines N and M``, ``lines N-M`` (both ends) or ``LN``."""
    found: Set[int] = set()
    for match in LINE_CITE.finditer(text):
        for group in match.groups():
            if group:
                found.update(int(n) for n in re.findall(r"\d+", group))
    return found


def reqs_at(state: Dict[str, Any], line: int, path: str) -> Set[str]:
    return {rid for rid, r in state["requirements"].items()
            if isinstance(r.get("source"), dict) and r["source"].get("path") == path and r["source"].get("line") == line}


def side_refs(state: Dict[str, Any], fid: str, text: str, line: int, label: str, phrase: str, path: str) -> bool:
    """Whether a finding refers to one side of the contradiction: by REQ or AC id, by the document's label, by line, or by phrase."""
    reqs = reqs_at(state, line, path)
    acs = {ac for ac, c in state["criteria"].items() if c.get("requirement") in reqs}
    ids = reqs | acs
    if any(re.search(r"(?<![\w-])%s(?![\w.-]?\d)" % re.escape(i), text) for i in ids):
        return True
    if any(l.get("source") == fid and l.get("relation") == "violates" and l.get("target") in ids for l in state.get("links", [])):
        return True
    if re.search(r"(?<![\w-])%s(?!\d)" % re.escape(label), text):
        return True
    return line in cited_lines(text) or bool(re.search(phrase, text, re.I))


def requirement_findings(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {fid: f for fid, f in state["findings"].items() if f.get("category") == "requirements"}


def recorded_types(facts: Dict[str, Any], kind: str) -> List[Dict[str, Any]]:
    return [e for e in facts["events"] if e.get("type") == kind]


def scanner_calls(stdout: str, filename: str) -> List[Dict[str, Any]]:
    return [c for c in tool_calls(stdout) if c["name"] == "Bash" and "weak_words.py" in str(c["input"].get("command", ""))
            and filename in str(c["input"].get("command", ""))]


# --------------------------------------------------------------------------- scenario: F5, requirements.md into criteria

def contradiction_findings(state: Dict[str, Any]) -> List[str]:
    found = []
    for fid, f in sorted(requirement_findings(state).items()):
        text = finding_text(f)
        if all(side_refs(state, fid, text, line, label, phrase, F5_NAME)
               for line, label, phrase in zip(CONTRA_LINES, CONTRA_LABELS, CONTRA_PHRASES)):
            found.append(fid)
    return found


def missing_behavior_findings(state: Dict[str, Any]) -> List[str]:
    return [fid for fid, f in sorted(requirement_findings(state).items()) if COUPON_OVER.search(finding_text(f))]


def names_risk(finding: Dict[str, Any]) -> bool:
    return isinstance(finding.get("impact"), str) and bool(finding["impact"].strip())


def answer_names_contradiction(text: str) -> bool:
    sides = all(re.search(r"(?<![\w-])%s(?!\d)" % label, text) or re.search(phrase, text, re.I) or line in cited_lines(text)
                for line, label, phrase in zip(CONTRA_LINES, CONTRA_LABELS, CONTRA_PHRASES))
    return bool(sides and CONTRADICTION_WORD.search(text))


def grade_f5(text: str, facts: Optional[Dict[str, Any]], with_skill: bool, stdout: str) -> List[Dict[str, Any]]:
    wanted = requirement_lines(F5_REQUIREMENTS)
    req_lines = sorted(n for n, t in wanted.items() if re.match(r"\s*R\d+\.", t))
    cited = cited_lines(text)
    substance = [
        crit("answer_cites_every_requirement_line", not with_skill, "substance", all(n in cited for n in req_lines),
             "requirement lines %s; cited in the answer: %s" % (req_lines, sorted(n for n in cited if n in req_lines))),
        crit("answer_names_the_contradiction", not with_skill, "substance", answer_names_contradiction(text),
             "R7/R8 (lines 23 and 25, or their wording) together with a contradiction word"),
        crit("answer_names_the_missing_behavior", not with_skill, "substance", bool(COUPON_OVER.search(text)),
             "a coupon worth more than the order, or a total below zero: %s" % (COUPON_OVER.search(text).group(0) if COUPON_OVER.search(text) else "none")),
    ]
    if not with_skill or facts is None:
        return absent(("every_req_cites_path_and_line", "contradiction_is_a_requirements_finding", "missing_behavior_is_a_requirements_finding",
                       "both_findings_name_their_risk")) + substance
    state = facts["state"]
    reqs = state["requirements"]
    bad = [rid for rid, r in sorted(reqs.items())
           if not (isinstance(r.get("source"), dict) and r["source"].get("path") == F5_NAME and isinstance(r["source"].get("line"), int)
                   and r["source"]["line"] in wanted)]
    contra, missing = contradiction_findings(state), missing_behavior_findings(state)
    findings = state["findings"]
    risk_ok = bool(contra) and bool(missing) and any(names_risk(findings[f]) for f in contra) and any(names_risk(findings[f]) for f in missing)
    scanned = scanner_calls(stdout, F5_NAME)
    return [
        crit("every_req_cites_path_and_line", True, "record", bool(reqs) and not bad,
             "requirements recorded: %d; without %s and a line that states something there: %s" % (len(reqs), F5_NAME, bad or "none")),
        crit("contradiction_is_a_requirements_finding", True, "record", bool(contra),
             "requirements findings naming both R7 (line 23) and R8 (line 25): %s; all requirements findings: %s"
             % (contra or "none", sorted(requirement_findings(state)) or "none")),
        crit("missing_behavior_is_a_requirements_finding", True, "record", bool(missing),
             "requirements findings about a coupon larger than the order: %s" % (missing or "none")),
        crit("both_findings_name_their_risk", True, "record", risk_ok,
             "impact of the contradiction findings %s and of the missing-behavior findings %s"
             % ({f: findings[f].get("impact") for f in contra}, {f: findings[f].get("impact") for f in missing})),
        crit("scanner_ran_on_requirements", False, "action", any(c["error"] is False for c in scanned),
             "weak_words.py calls on %s: %d, succeeded: %d" % (F5_NAME, len(scanned), sum(1 for c in scanned if c["error"] is False))),
    ] + substance


# --------------------------------------------------------------------------- scenario: F5 without requirements.md, inferred

def verified_claims(text: str) -> List[str]:
    return [s for s in sentences(text) if VERIFIED_CLAIM.search(s) and not NEGATION.search(s)]


def grade_infer(text: str, facts: Optional[Dict[str, Any]], with_skill: bool, stdout: str) -> List[Dict[str, Any]]:
    claims = verified_claims(text)
    asks = bool(ASK_WORD.search(text)) and bool(ASSUMPTION_WORD.search(text))
    substance = [
        crit("answer_marks_requirements_inferred", not with_skill, "substance", bool(INFERRED_WORD.search(text)),
             "inferred wording: %s" % (INFERRED_WORD.search(text).group(0) if INFERRED_WORD.search(text) else "none")),
        crit("answer_asks_about_assumptions", not with_skill, "substance", asks,
             "a question or a request to confirm, and an assumption or confirmation word"),
        crit("answer_claims_nothing_verified", not with_skill, "substance", not claims, "verified claims: %s" % (claims or "none")),
    ]
    names = ("every_req_inferred", "none_confirmed", "critical_assumptions_recorded_open", "assumptions_put_to_user", "nothing_marked_verified")
    if not with_skill or facts is None:
        return absent(names) + substance
    state = facts["state"]
    reqs = state["requirements"]
    not_inferred = {rid: r.get("provenance") for rid, r in sorted(reqs.items()) if r.get("provenance") != "inferred"}
    confirmations = recorded_types(facts, "requirement.confirmed")
    decisions = recorded_types(facts, "assumption.decided")
    open_asm = sorted(a for a, r in state["assumptions"].items() if r.get("status") in (None, "open"))
    named = sorted(set(ASM_ID.findall(text)) & set(open_asm))
    met = []
    for event in recorded_types(facts, "gate.evaluated"):
        for c in (event.get("data") or {}).get("criteria") or []:
            if isinstance(c, dict) and c.get("id") == "requirements_verified" and c.get("outcome") == "MET":
                met.append(event.get("id"))
    return [
        crit("every_req_inferred", True, "record", bool(reqs) and not not_inferred,
             "requirements recorded: %d; not inferred: %s" % (len(reqs), not_inferred or "none")),
        crit("none_confirmed", True, "record", not confirmations and not any(r.get("confirmed") for r in reqs.values()),
             "requirement.confirmed events: %d" % len(confirmations)),
        crit("critical_assumptions_recorded_open", True, "record", bool(open_asm) and not decisions,
             "open assumptions: %s; assumption.decided events: %d" % (open_asm or "none", len(decisions))),
        crit("assumptions_put_to_user", True, "record", bool(named) and bool(ASK_WORD.search(text)),
             "open assumptions named in the answer: %s; the answer asks: %s" % (named or "none", bool(ASK_WORD.search(text)))),
        crit("nothing_marked_verified", True, "record", not confirmations and not decisions and not met,
             "confirmations %d, decisions %d, gate evaluations with requirements_verified MET: %s"
             % (len(confirmations), len(decisions), met or "none")),
    ] + substance


# --------------------------------------------------------------------------- scenario: Spanish requirements file

def flagged_in_state(state: Dict[str, Any], line: int, pattern: str) -> List[str]:
    """Where the state flags the word: the quality issues of the requirement on that line, or a requirements finding naming it."""
    where = []
    for rid in sorted(reqs_at(state, line, _spanish.NAME)):
        if any(re.search(pattern, fold(str(issue))) for issue in state["requirements"][rid].get("quality_issues") or []):
            where.append(rid)
    for fid, f in sorted(requirement_findings(state).items()):
        if re.search(pattern, fold(finding_text(f))):
            where.append(fid)
    return where


def grade_spanish(text: str, facts: Optional[Dict[str, Any]], with_skill: bool, stdout: str) -> List[Dict[str, Any]]:
    folded = fold(text)
    missed = [label for _, label, pattern in _spanish.EXPECTED if not re.search(pattern, folded)]
    substance = [crit("answer_names_every_spanish_weak_word", not with_skill, "substance", not missed,
                      "expected %s; not named in the answer: %s" % ([e[1] for e in _spanish.EXPECTED], missed or "none"))]
    if not with_skill:
        return absent(("every_spanish_weak_word_flagged_in_the_record",)) + absent(("scanner_ran_on_the_spanish_file",), "action") + substance
    scanned = scanner_calls(stdout, _spanish.NAME)
    ran = crit("scanner_ran_on_the_spanish_file", True, "action", any(c["error"] is False for c in scanned),
               "weak_words.py calls on %s: %d, succeeded: %d" % (_spanish.NAME, len(scanned), sum(1 for c in scanned if c["error"] is False)))
    if facts is None:
        return [crit("every_spanish_weak_word_flagged_in_the_record", True, "record", False, "no ledger in the run's evidence"), ran] + substance
    state = facts["state"]
    where = {label: flagged_in_state(state, line, pattern) for line, label, pattern in _spanish.EXPECTED}
    return [crit("every_spanish_weak_word_flagged_in_the_record", True, "record", all(where.values()),
                 "where each word is flagged (requirement quality issues or requirements findings): %s" % where), ran] + substance


GRADERS = {"qa-requirements-f5-acceptance-criteria": grade_f5, "qa-requirements-f5-infer-from-code": grade_infer,
           "qa-requirements-spanish-weak-words": grade_spanish}


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
        summary = {"requirements": len(st["requirements"]), "criteria": len(st["criteria"]), "assumptions": len(st["assumptions"]),
                   "findings": {fid: f.get("category") for fid, f in sorted(st["findings"].items())},
                   "provenance": sorted({r.get("provenance") for r in st["requirements"].values()})}
    print(json.dumps({"case": case_id, "config": record["config"], "run_id": args.run_id, "cost_usd": record["cost_usd"],
                      "status": record.get("status"), "kernel_state_found": facts is not None, "state_summary": summary,
                      "skill": skill_loading(raw), "criteria": criteria, "hard_failed": hard_failed,
                      "verdict": "pass" if not hard_failed else "fail", "answer": text}, indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if not hard_failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
