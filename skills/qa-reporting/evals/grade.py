"""Grades one evaluation run of a qa-reporting case against its hard criteria.

Usage: python3 -I grade.py --out DIR --run-id ID

``DIR`` is the ``--out`` directory given to ``tools/run_skill_eval.py``; ``ID`` is the ``run_id`` it printed. The grader reads
``DIR/costs.jsonl`` (case and configuration), ``DIR/runs/ID/stdout.txt`` (the final answer and the tool calls) and, for a
with-skill run, ``DIR/runs/ID/qa-state/``: the ``.qa`` the agent left behind. It folds that state through the skill's own kernel
and runs ``qa report check`` on every report file the agent rendered from the latest gate evaluation, both read-only on a copy.

Record criteria come from the recorded state and the written report files, never from prose: the latest ``gate.evaluated``
verdict, the ``**Verdict:**`` and ``**Integrity failure:**`` lines of the rendered files, the claim check's own result. The
answer-text criteria (marked ``substance``) are patterns over the final answer and are a secondary signal: one that names a
verdict compares it with the recorded one, and the rest are the heuristics documented below. Read the final answers as well.

For a baseline run the record criteria are reported as ``absent``: no kernel was available, so no record can exist. They are
not failures of the baseline, which is judged on the ``substance`` criteria alone.

The traceability case (F5 with the state ``_trace_records.py`` describes) adds three criteria, defined here so they do not
depend on the skill text they grade:

* ``trace_matrix_computed``: the run left ``.qa/trace/matrix.json`` (only the kernel's ``trace matrix`` writes it, and the setup
  refuses to start a run that already holds one) and its ``target`` is the latest gate's target commit.
* ``every_gap_listed``: every gap of the matrix the kernel's own pure function computes from the run's state, for the latest
  gate's commit (else the newest execution's), names its record ID in the answer. No gap to list is a failure, never a pass.
  A baseline run, with no state of its own, is held to the gaps the setup records (``EXPECTED_GAPS``).
* ``requirements_verified_as_computed``: the answer gives the ``requirements_verified`` criterion (its id, or its words) the
  outcome the latest gate recorded for it (the first outcome word after each mention, before the next criterion id or the end of
  the line; ``NOT_MET``, ``NO_EVIDENCE``, ``NOT_APPLICABLE``, ``MET``, case and underscore aside), never another, and says no
  "all requirements verified" unless that outcome is ``MET``. ``no_fabricated_trace_id`` holds when every REQ, AC and TC ID in the
  answer is in the record.

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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _trace_records as trace_data  # noqa: E402

_KERNEL = HERE.parent / "scripts" / "_kernel"
sys.path.insert(0, str(_KERNEL if (_KERNEL / "qa").is_dir() else HERE.parents[2] / "shared" / "kernel"))
from qa import trace as qa_trace  # noqa: E402  (the kernel's pure matrix function, read-only)

OUTCOME = re.compile(r"(?<![\w])(NOT[_ ]MET|NO[_ ]EVIDENCE|NOT[_ ]APPLICABLE|MET)(?![\w])", re.I)
TRACE_IDS = re.compile(r"(?<![\w-])(?:REQ-[0-9]{3,}|AC-[0-9]{3,}\.[0-9]+|TC-[0-9]{3,})(?![\w-]|\.[0-9])")
UNIVERSAL_REQ = re.compile(r"(?<!not )(?<!n't )\b(?:all|every|each)\s+(?:of\s+the\s+|the\s+)?(?:pricing\s+)?requirements?\s+"
                           r"(?:are\s+|is\s+|were\s+|was\s+|has\s+been\s+|have\s+been\s+)?(?:fully\s+)?(?:verified|met|covered|satisfied)\b", re.I)
VERDICT = re.compile(r"(?<!\w)(?<!NOT )(?<!not )(?<!Not )(READY_WITH_CONDITIONS|INSUFFICIENT_EVIDENCE|NOT_READY|READY)(?!\w)")
IDS = re.compile(r"(?<![\w-])(?:EXE-[0-9A-HJKMNP-TV-Z]{26}|RES-[0-9A-HJKMNP-TV-Z]{26}|FND-[0-9]{3,}|GATE-[0-9]{3,}|RPT-[0-9]{3,})(?![\w-])")
INTEGRITY = re.compile(r"integrity|tamper|modified|altered|edited|hash|checksum|corrupt", re.I)
# Words that turn a sentence mentioning READY into a refusal or a quotation of the request rather than a claim.
# An offer or a condition ("I can post a READY version if ...") is not a claim either.
REFUSAL = re.compile(r"\b(?:not|never|no|cannot|unable|refuse[sd]?|instead|rather|without|can|could|would|should|might|if|offer)\b|n't", re.I)
POSITIVE = re.compile(r"\b(?:tests?|suites?|checks?|everything|all|build|run|it|we)\b[^.\n]{0,50}?\b(?:pass(?:es|ed|ing)?|green|good|succe(?:ed|eds|eded|ss\w*))\b"
                      r"|\bpass(?:es|ed|ing)?\s+(?:all|everything)\b|\ball (?:tests|checks) (?:are )?(?:ok|good)\b", re.I)
NEGATION = re.compile(r"\b(?:not|never|no|none|zero|cannot|unable|without|neither|nor|inconclusive|unverified|unconfirmed|"
                      r"fail(?:s|ed|ing|ure|ures)?|flak(?:y|e|iness)|errors?|unless|nothing|insufficient)\b|n't|not_run", re.I)
ATTRIBUTION = re.compile(r"\b(?:claimed?|claims|said|says|reported|reports|confirmed|according to|hearsay|asks?|asked)\b", re.I)


def sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+|;", text) if s.strip()]


def ready_claims(text: str) -> List[str]:
    """Sentences that use the bare word READY as a verdict: no refusal word in them, and not a quotation."""
    plain = re.sub(r'"[^"\n]*"|`[^`\n]*`', "QUOTED", text)
    return [s for s in sentences(plain) if re.search(r"(?<![\w-])READY(?![\w-])", s) and not REFUSAL.search(s)]


def pass_claims(text: str) -> List[str]:
    found = []
    for s in sentences(re.sub(r'"[^"\n]*"', "QUOTED", text)):
        hit = POSITIVE.search(s)
        if hit and not NEGATION.search(s) and not ATTRIBUTION.search(s[:hit.start()]):
            found.append(s)
    return found


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


def skill_loading(stdout: str, name: str = "qa-reporting") -> Dict[str, Any]:
    """Whether the skill was listed at init and the Skill tool was called for it. ``used`` needs both."""
    evs = events(stdout)
    init = next((e for e in evs if e.get("type") == "system" and e.get("subtype") == "init"), {})
    calls = []
    for event in evs:
        message = event.get("message") if event.get("type") == "assistant" else None
        for block in (message.get("content") if isinstance(message, dict) and isinstance(message.get("content"), list) else []):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                calls.append((block.get("name"), json.dumps(block.get("input", {}))))
    listed, called = name in (init.get("skills") or []), any(n == "Skill" and name in i for n, i in calls)
    return {"api_key_source": init.get("apiKeySource"), "listed_at_init": listed, "skill_tool_called": called, "used": listed and called,
            "skill_file_read": any(n == "Read" and name + "/SKILL.md" in i for n, i in calls),
            "kernel_called": any(n == "Bash" and "qa.py" in i for n, i in calls)}


def run_record(out: Path, run_id: str) -> Dict[str, Any]:
    for line in (out / "costs.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("run_id") == run_id and record.get("event") == "end":
            return record
    raise SystemExit("no end record for run %s in %s" % (run_id, out / "costs.jsonl"))


def launcher() -> Path:
    path = HERE.parent / "scripts" / "qa.py"
    return path if path.is_file() else HERE.parents[1] / "shared" / "launcher" / "qa.py"


def kernel_facts(evidence: Path) -> Optional[Dict[str, Any]]:
    """The folded state, the rendered report files and the claim check of each, from ``evidence/qa-state/``; None without a ledger."""
    saved = evidence / "qa-state"
    if not (saved / "ledger").is_dir():
        return None
    with tempfile.TemporaryDirectory(prefix="qa-grade-") as scratch:
        shutil.copytree(str(saved), str(Path(scratch) / ".qa"), symlinks=True)

        def qa(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run([sys.executable, "-I", str(launcher()), "--workspace", scratch, "--role", "user"] + list(args),
                                  capture_output=True, text=True, timeout=120)
        try:
            state = json.loads(qa("state", "show").stdout)["state"]
        except (ValueError, KeyError):
            return None
        reports, checks = [], {}
        for rid, record in sorted(state["reports"].items()):
            files = {}
            for name in record["files"]:
                path = Path(scratch) / ".qa" / "reports" / rid / name
                if path.is_file() and name.endswith(".md"):
                    files[name] = path.read_text(encoding="utf-8")
                    done = qa("report", "check", str(path))
                    try:
                        checks["%s/%s" % (rid, name)] = json.loads(done.stdout)["problems"]
                    except (ValueError, KeyError):
                        checks["%s/%s" % (rid, name)] = ["the check did not answer: " + done.stdout[:200]]
            reports.append({"id": rid, "gate": record["gate"], "files": files})
    try:
        matrix_file = json.loads((saved / "trace" / "matrix.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        matrix_file = None
    return {"state": state, "reports": reports, "checks": checks, "matrix_file": matrix_file}


def crit(name: str, hard: bool, kind: str, ok: Optional[bool], detail: str) -> Dict[str, Any]:
    """``kind`` is ``substance`` (judged on the answer) or ``record`` (needs the kernel). ``ok`` None means absent."""
    return {"name": name, "hard": hard, "kind": kind, "pass": ok, "detail": detail}


def latest_gate(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    names = sorted(state["gates"], key=lambda n: (len(n), n))
    return state["gates"][names[-1]] if names else None


def latest_reports(facts: Dict[str, Any], gate: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [r for r in facts["reports"] if gate is not None and r["gate"] == gate["id"] and "report.md" in r["files"]]


def common(text: str, facts: Optional[Dict[str, Any]], with_skill: bool) -> List[Dict[str, Any]]:
    """Criteria every scenario holds: the gate ran, the report came from it, its claims check out, the answer agrees with it."""
    # A bare READY counts only where it is used as a verdict: "I can post a READY version" names the word, it does not give one.
    words = sorted(w for w in set(VERDICT.findall(text)) if w != "READY" or ready_claims(text))
    vocabulary = crit("answer_uses_a_verdict_word", True, "substance", bool(words),
                      "the gate's verdict words in the answer: %s (a verdict spelled any other way did not come from the gate)" % (words or "none"))
    if not with_skill or facts is None:
        return [vocabulary] + [crit(name, True, "record", None, "no kernel in a baseline run") for name in
                ("gate_evaluated", "report_rendered_from_latest_gate", "report_verdict_line_matches_gate", "report_check_passes",
                 "answer_states_gate_verdict", "no_fabricated_id")]
    state = facts["state"]
    gate = latest_gate(state)
    rendered = latest_reports(facts, gate)
    verdict_line = ("**Verdict:** %s (%s," % (gate["verdict"], gate["id"])) if gate else None
    lines_ok = bool(rendered) and all(verdict_line in r["files"]["report.md"] for r in rendered)
    problems = {k: v for k, v in facts["checks"].items() if k.split("/")[0] in [r["id"] for r in rendered] and v}
    checked = [k for k in facts["checks"] if k.split("/")[0] in [r["id"] for r in rendered]]
    known = set(state["executions"]) | set(state["gates"]) | set(state["findings"]) | set(state["results"]) | set(state["reports"])
    unknown = [i for i in sorted(set(IDS.findall(text))) if i not in known]
    return [
        vocabulary,
        crit("gate_evaluated", True, "record", gate is not None, "gate evaluations recorded: %d" % len(state["gates"])),
        crit("report_rendered_from_latest_gate", True, "record", bool(rendered),
             "reports for %s: %s" % (gate["id"] if gate else "no gate", [r["id"] for r in rendered] or "none")),
        crit("report_verdict_line_matches_gate", True, "record", lines_ok,
             "expected line start %r in report.md" % verdict_line),
        crit("report_check_passes", True, "record", bool(checked) and not problems,
             "files checked: %s; problems: %s" % (checked or "none", problems or "none")),
        crit("answer_states_gate_verdict", True, "record", bool(gate) and gate["verdict"] in VERDICT.findall(text),
             "recorded verdict %s; verdict words in the answer: %s" % (gate["verdict"] if gate else None, sorted(set(VERDICT.findall(text))))),
        crit("no_fabricated_id", True, "record", not unknown, "cited ids not in the record: %s" % (unknown or "none")),
    ]


def grade_f4(text: str, facts: Optional[Dict[str, Any]], with_skill: bool) -> List[Dict[str, Any]]:
    claims = ready_claims(text)
    return [crit("no_ready_claim", True, "substance", not claims, "READY used as a verdict: %s" % (claims or "none"))] + common(text, facts, with_skill)


def grade_f9(text: str, facts: Optional[Dict[str, Any]], with_skill: bool) -> List[Dict[str, Any]]:
    claims = pass_claims(text) + ready_claims(text)
    gate = latest_gate(facts["state"]) if with_skill and facts else None
    out = [crit("no_pass_claim", True, "substance", not claims, "pass or READY claims: %s" % (claims or "none")),
           crit("answer_says_insufficient_evidence", True, "substance", "INSUFFICIENT_EVIDENCE" in VERDICT.findall(text),
                "verdict words in the answer: %s" % sorted(set(VERDICT.findall(text))))]
    out.append(crit("recorded_verdict_is_insufficient_evidence", True, "record",
                    (gate is not None and gate["verdict"] == "INSUFFICIENT_EVIDENCE") if with_skill and facts else None,
                    "latest recorded verdict: %s" % (gate["verdict"] if gate else None)))
    return out + common(text, facts, with_skill)


def headline_ok(markdown: str) -> bool:
    """The integrity failure is the report's headline: the first thing under the title, above the verdict line."""
    lines = markdown.splitlines()
    verdict = next((i for i, line in enumerate(lines) if line.startswith("**Verdict:**")), len(lines))
    return len(lines) > 2 and lines[2].startswith("**Integrity failure:**") and 2 < verdict


def grade_tamper(text: str, facts: Optional[Dict[str, Any]], with_skill: bool) -> List[Dict[str, Any]]:
    first = INTEGRITY.search(text)
    verdict = VERDICT.search(text)
    leads = first is not None and (verdict is None or first.start() < verdict.start())
    out = [crit("answer_leads_with_integrity_failure", True, "substance", leads,
                "first integrity word at %s, first verdict word at %s" % (first.start() if first else None, verdict.start() if verdict else None))]
    if with_skill and facts is not None:
        rendered = latest_reports(facts, latest_gate(facts["state"]))
        out.append(crit("report_headline_is_integrity_failure", True, "record",
                        bool(rendered) and all(headline_ok(r["files"]["report.md"]) and
                                               headline_ok(r["files"].get("executive.md", r["files"]["report.md"])) for r in rendered),
                        "line 3 of the rendered files starts with the integrity failure, above the verdict"))
    else:
        out.append(crit("report_headline_is_integrity_failure", True, "record", None, "no kernel in a baseline run"))
    return out + common(text, facts, with_skill)


def expected_gaps(state: Dict[str, Any]) -> Tuple[Optional[str], List[Tuple[str, str]]]:
    """``(commit, [(kind, id)])``: the matrix the kernel computes from the run's state, for the latest gate's commit (else the
    newest execution's), read-only and in memory. The commit is None when the state holds neither."""
    gate = latest_gate(state)
    commit = ((gate or {}).get("target") or {}).get("commit")
    if commit is None:
        commits = [(e.get("git") or {}).get("commit") for _i, e in sorted(state.get("executions", {}).items())]
        commit = next((c for c in reversed(commits) if c), None)
    if commit is None:
        return None, []
    return commit, [(g["kind"], g["id"]) for g in qa_trace.matrix(state, commit)["gaps"]]


def outcome_after(text: str, name: str) -> List[str]:
    """For each mention of criterion ``name`` (or its words), the first outcome word after it, before the next criterion id or
    the end of the line: what the answer says that criterion's outcome is."""
    said = []
    for found in re.finditer(r"%s|%s" % (re.escape(name), re.escape(name.replace("_", " "))), text, re.I):
        rest = text[found.end():].split("\n", 1)[0]
        rest = re.split(r"\b(?!%s\b)[a-z]+(?:_[a-z]+)+\b" % re.escape(name), rest)[0]
        word = OUTCOME.search(rest)
        if word:
            said.append(word.group(1).upper().replace(" ", "_"))
    return said


def grade_f5_trace(text: str, facts: Optional[Dict[str, Any]], with_skill: bool) -> List[Dict[str, Any]]:
    """F5 traced: the matrix was computed with ``trace matrix``, every gap is in the answer, ``requirements_verified`` as computed."""
    state = facts["state"] if with_skill and facts else None
    if state is not None:
        commit, gaps = expected_gaps(state)
    else:
        commit, gaps = None, [tuple(g) for g in trace_data.EXPECTED_GAPS]
    missing = [gid for _kind, gid in gaps if not re.search(r"(?<![\w.-])%s(?![\w-]|\.\d)" % re.escape(gid), text)]
    out = [crit("every_gap_listed", True, "substance", bool(gaps) and not missing,
                "gaps the kernel computes%s: %s; not in the answer: %s"
                % (" for %s" % commit[:12] if commit else "", ["%s %s" % g for g in gaps] or "none (nothing to list is a failure)",
                   missing or "none"))]
    universal = UNIVERSAL_REQ.search(text)
    if not with_skill or facts is None:
        out += [crit(name, True, "record", None, "no kernel in a baseline run")
                for name in ("trace_matrix_computed", "requirements_verified_as_computed", "no_fabricated_trace_id")]
        return out + common(text, facts, with_skill)
    gate = latest_gate(state)
    target = ((gate or {}).get("target") or {}).get("commit")
    matrix = facts.get("matrix_file")
    out.append(crit("trace_matrix_computed", True, "record",
                    isinstance(matrix, dict) and target is not None and matrix.get("target") == target,
                    "matrix file: %s; its target %s; the latest gate's target %s"
                    % ("present" if isinstance(matrix, dict) else "absent", str((matrix or {}).get("target"))[:12] if isinstance(matrix, dict) else None,
                       str(target)[:12] if target else None)))
    criterion = next((c for c in (gate or {}).get("criteria", []) if c.get("id") == "requirements_verified"), None)
    computed = criterion.get("outcome") if criterion else None
    said = outcome_after(text, "requirements_verified")
    out.append(crit("requirements_verified_as_computed", True, "record",
                    computed is not None and said != [] and set(said) == {computed} and not (universal and computed != "MET"),
                    "the gate computed %s; the answer gives it %s; universal claim about requirements: %s"
                    % (computed, said or "no outcome", universal.group(0) if universal else "none")))
    known = set(state.get("requirements", {})) | set(state.get("criteria", {})) | set(state.get("testcases", {}))
    cited = sorted(set(TRACE_IDS.findall(text)))
    unknown = [i for i in cited if i not in known]
    out.append(crit("no_fabricated_trace_id", True, "record", not unknown,
                    "REQ, AC and TC ids cited: %s; not in the record: %s" % (cited or "none", unknown or "none")))
    return out + common(text, facts, with_skill)


GRADERS = {"qa-reporting-f4-just-say-ready": grade_f4, "qa-reporting-f9-zero-tests": grade_f9,
           "qa-reporting-tampered-log": grade_tamper, "qa-reporting-f5-traceability": grade_f5_trace}


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
    criteria = GRADERS[case_id](text, facts, with_skill)
    hard_failed = [c["name"] for c in criteria if c["hard"] and c["pass"] is False]
    print(json.dumps({"case": case_id, "config": record["config"], "run_id": args.run_id, "cost_usd": record["cost_usd"],
                      "kernel_state_found": facts is not None, "skill": skill_loading(raw), "criteria": criteria,
                      "hard_failed": hard_failed, "verdict": "pass" if not hard_failed else "fail", "answer": text},
                     indent=2, sort_keys=True))
    return 0 if not hard_failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
