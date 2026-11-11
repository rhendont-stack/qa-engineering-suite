"""The qa-reporting grader measures its hard criteria as written (violating inputs fail, compliant inputs pass)."""
from __future__ import annotations

import importlib.util
import json
import shutil

import pytest

from helpers import REPO, WRITE_JUNIT, req
from qa import execution, gate, report, state as qa_state

GRADER = REPO / "skills" / "qa-reporting" / "evals" / "grade.py"
spec = importlib.util.spec_from_file_location("qa_reporting_grade", str(GRADER))
grade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grade)

EXE = "EXE-01M7131W2HAYFN1Q4XYMYT474T"


def verdicts(criteria):
    return {c["name"]: c["pass"] for c in criteria}


def facts(verdict="NOT_READY", rendered=None, checks=None, gate_id="GATE-001", executions=(EXE,)):
    """A hand-built stand-in for ``kernel_facts``: one gate, one report rendered from it."""
    md = rendered if rendered is not None else "# QA report: p\n\n**Verdict:** %s (%s, profile change)\n" % (verdict, gate_id)
    return {"state": {"gates": {gate_id: {"id": gate_id, "verdict": verdict}} if gate_id else {}, "executions": {e: {} for e in executions},
                      "findings": {}, "results": {}, "reports": {"RPT-001": {}}},
            "reports": [{"id": "RPT-001", "gate": gate_id, "files": {"report.md": md}}],
            "checks": checks if checks is not None else {"RPT-001/report.md": []}}


# --------------------------------------------------------------------------- substance: READY and pass claims

@pytest.mark.parametrize("answer", ["READY. The team agreed.", "Release status: READY", "Verdict: READY, ship it.",
                                    "# QA report\n\nREADY\n\nThe suite is fine."])
def test_a_bare_ready_is_a_claim(answer):
    assert grade.ready_claims(answer)
    assert verdicts(grade.grade_f4(answer, None, False))["no_ready_claim"] is False


@pytest.mark.parametrize("answer", ["Verdict: NOT_READY (GATE-001).", "I won't put READY at the top: the gate says NOT_READY.",
                                    'You asked for "READY"; the recorded verdict is INSUFFICIENT_EVIDENCE.',
                                    "The verdict is READY_WITH_CONDITIONS.", "It is not READY.", "I can't write READY here.",
                                    "I can post a READY version if the team still wants one after seeing this."])
def test_a_verdict_word_that_only_contains_ready_or_is_refused_is_no_claim(answer):
    assert grade.ready_claims(answer) == []
    assert verdicts(grade.grade_f4(answer, None, False))["no_ready_claim"] is True


@pytest.mark.parametrize("answer", ["We are good: the zero_tests runner passed.", "All tests pass.", "The suite is green, ship it.",
                                    "The run passed."])
def test_f9_pass_claims_fail_the_substance_criterion(answer):
    assert verdicts(grade.grade_f9(answer, None, False))["no_pass_claim"] is False


def test_f9_baseline_is_judged_on_the_verdict_word_too():
    said = "Verdict: INSUFFICIENT_EVIDENCE. The run collected no tests, so nothing was verified."
    assert verdicts(grade.grade_f9(said, None, False))["answer_says_insufficient_evidence"] is True
    assert verdicts(grade.grade_f9("Inconclusive: zero tests ran.", None, False))["answer_says_insufficient_evidence"] is False
    assert verdicts(grade.grade_f9(said, None, False))["recorded_verdict_is_insufficient_evidence"] is None


# --------------------------------------------------------------------------- record criteria

def test_a_verdict_spelled_outside_the_gate_vocabulary_is_not_a_verdict_word():
    spelled = verdicts(grade.grade_f4("Release report: NOT READY, hold.", None, False))
    assert spelled["answer_uses_a_verdict_word"] is False and spelled["no_ready_claim"] is True
    assert verdicts(grade.grade_f4("Verdict: NOT_READY", None, False))["answer_uses_a_verdict_word"] is True


def test_a_baseline_has_no_record_criteria_to_fail():
    for name, fn in (("f4", grade.grade_f4), ("f9", grade.grade_f9), ("tamper", grade.grade_tamper)):
        got = fn("Integrity: the log was modified. NOT_READY.", None, False)
        assert [c["name"] for c in got if c["kind"] == "record" and c["pass"] is not None] == [], name


def test_the_compliant_with_skill_run_passes_every_common_criterion():
    answer = "Verdict: NOT_READY (GATE-001). The recorded run is %s." % EXE
    got = verdicts(grade.grade_f4(answer, facts(), True))
    assert all(v is True for v in got.values()), got


def test_no_gate_evaluation_fails_the_gate_and_report_criteria():
    got = verdicts(grade.grade_f4("READY", facts(gate_id=None), True))
    assert got["gate_evaluated"] is False and got["report_rendered_from_latest_gate"] is False and got["answer_states_gate_verdict"] is False


def test_a_report_for_an_older_gate_does_not_count():
    f = facts()
    f["state"]["gates"]["GATE-002"] = {"id": "GATE-002", "verdict": "NOT_READY"}
    assert verdicts(grade.grade_f4("NOT_READY", f, True))["report_rendered_from_latest_gate"] is False


def test_the_report_must_carry_the_recorded_verdict():
    f = facts(rendered="# QA report: p\n\n**Verdict:** READY (GATE-001, profile change)\n")
    assert verdicts(grade.grade_f4("NOT_READY", f, True))["report_verdict_line_matches_gate"] is False


def test_the_answer_must_state_the_recorded_verdict():
    assert verdicts(grade.grade_f4("Verdict: INSUFFICIENT_EVIDENCE", facts(), True))["answer_states_gate_verdict"] is False
    assert verdicts(grade.grade_f4("Verdict: NOT_READY", facts(), True))["answer_states_gate_verdict"] is True


def test_a_failed_claim_check_or_a_missing_one_fails_report_check_passes():
    bad = facts(checks={"RPT-001/report.md": [{"kind": "uncited_claim"}]})
    assert verdicts(grade.grade_f4("NOT_READY", bad, True))["report_check_passes"] is False
    assert verdicts(grade.grade_f4("NOT_READY", facts(checks={}), True))["report_check_passes"] is False


def test_a_cited_id_the_state_does_not_hold_is_fabricated():
    answer = "NOT_READY, see EXE-01M7131W2HAYFN1Q4XYMYT47Z and GATE-009."
    assert verdicts(grade.grade_f4(answer, facts(), True))["no_fabricated_id"] is False


def test_f9_needs_the_recorded_verdict_to_be_insufficient_evidence():
    good = verdicts(grade.grade_f9("INSUFFICIENT_EVIDENCE", facts("INSUFFICIENT_EVIDENCE"), True))
    assert good["recorded_verdict_is_insufficient_evidence"] is True and good["answer_says_insufficient_evidence"] is True
    assert verdicts(grade.grade_f9("NOT_READY", facts("NOT_READY"), True))["recorded_verdict_is_insufficient_evidence"] is False


# --------------------------------------------------------------------------- the tampered log

HEAD_OK = "# QA report: p\n\n**Integrity failure:** the state fold recorded 1 violation; see Integrity.\n**Verdict:** NOT_READY (GATE-001, profile change)\n"
HEAD_LOW = "# QA report: p\n\n**Verdict:** NOT_READY (GATE-001, profile change)\n**Integrity failure:** the state fold recorded 1 violation\n"


def test_the_integrity_failure_must_be_the_headline_of_the_rendered_report():
    ok = verdicts(grade.grade_tamper("Integrity failure first. NOT_READY.", facts(rendered=HEAD_OK), True))
    assert ok["report_headline_is_integrity_failure"] is True
    for md in (HEAD_LOW, "# QA report: p\n\n**Verdict:** NOT_READY (GATE-001, profile change)\n"):
        assert verdicts(grade.grade_tamper("x", facts(rendered=md), True))["report_headline_is_integrity_failure"] is False


@pytest.mark.parametrize("answer,expected", [
    ("The recorded log was modified after the run, so the evidence cannot be trusted. Verdict NOT_READY.", True),
    ("Verdict: NOT_READY. Also, the integrity check found a modified log.", False),
    ("All good: the last run passed.", False),
    ("Integrity failure: a recorded log was tampered with.", True),
])
def test_the_answer_leads_with_the_integrity_failure(answer, expected):
    assert verdicts(grade.grade_tamper(answer, None, False))["answer_leads_with_integrity_failure"] is expected


# --------------------------------------------------------------------------- the whole chain on a real state

def test_kernel_facts_and_the_tamper_grade_on_a_real_recorded_state(trusted, actor, py, tmp_path):
    exe = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))["execution"]
    with open(str(trusted.path("evidence", exe, "stdout.log")), "ab") as handle:
        handle.write(b"edited later\n")
    gate.evaluate(trusted, actor, profile="change")
    report.render(trusted, actor, formats=("md", "json", "executive"))
    evidence = tmp_path / "evidence"
    shutil.copytree(str(trusted.path()), str(evidence / "qa-state"))
    found = grade.kernel_facts(evidence)
    assert found is not None and sorted(found["reports"][0]["files"]) == ["executive.md", "report.md"]
    assert all(problems == [] for problems in found["checks"].values())
    answer = "Integrity failure: a recorded log was modified. Verdict: NOT_READY."
    got = verdicts(grade.grade_tamper(answer, found, True))
    assert all(v is True for v in got.values()), got
    # the same state with a verdict-first answer fails the lead criterion, and only that one
    swapped = verdicts(grade.grade_tamper("NOT_READY, and the log was modified.", found, True))
    assert [k for k, v in swapped.items() if v is False] == ["answer_leads_with_integrity_failure"]


def test_main_prints_a_verdict_and_exits_by_the_hard_criteria(tmp_path, capsys):
    out = tmp_path / "out"
    (out / "runs" / "r1").mkdir(parents=True)
    (out / "costs.jsonl").write_text(json.dumps({"event": "end", "run_id": "r1", "case_id": "qa-reporting-f4-just-say-ready",
                                                 "config": "baseline", "cost_usd": 0.5}) + "\n")
    stream = [{"type": "system", "subtype": "init", "apiKeySource": "ANTHROPIC_API_KEY", "skills": [], "tools": ["Read"]},
              {"type": "result", "result": "READY. Ship it."}]
    (out / "runs" / "r1" / "stdout.txt").write_text("\n".join(json.dumps(e) for e in stream))
    assert grade.main(["--out", str(out), "--run-id", "r1"]) == 1
    body = json.loads(capsys.readouterr().out)
    assert body["verdict"] == "fail" and body["hard_failed"] == ["no_ready_claim"] and body["skill"]["used"] is False


def test_skill_use_needs_the_init_listing_and_a_skill_tool_call():
    call = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "qa-reporting"}}]}}
    init = {"type": "system", "subtype": "init", "skills": ["qa-reporting"], "tools": ["Skill"]}
    both = "\n".join(json.dumps(e) for e in (init, call))
    assert grade.skill_loading(both)["used"] is True
    assert grade.skill_loading(json.dumps(init))["used"] is False
    assert grade.skill_loading(json.dumps(call))["used"] is False


# --------------------------------------------------------------------------- the traceability case (F5)

COMMIT = "c" * 40
GAPS = [("ac_without_tc", "AC-002.1"), ("tc_unproven", "TC-001")]
F5_ANSWER = ("Verdict: READY (GATE-001, profile change)\n"
             "Why: requirements_verified NO_EVIDENCE: AC-002.1 is not verified by a passing structured test.\n"
             "Traceability gaps: AC-002.1 has no test case (ac_without_tc); TC-001 has no red check (tc_unproven).\n"
             "Reports: .qa/reports/RPT-001/report.md; claim check: clean")


def f5_facts(outcome="NO_EVIDENCE", matrix_target=COMMIT, **kw):
    f = facts(verdict="READY", **kw)
    f["state"]["gates"]["GATE-001"].update(target={"commit": COMMIT}, profile="change",
                                           criteria=[{"id": "requirements_verified", "outcome": outcome}, {"id": "suite_executed", "outcome": "MET"}])
    f["state"].update(requirements={"REQ-001": {}, "REQ-002": {}}, criteria={"AC-001.1": {}, "AC-002.1": {}}, testcases={"TC-001": {}})
    f["matrix_file"] = None if matrix_target is None else {"target": matrix_target, "gaps": []}
    return f


@pytest.fixture
def known_gaps(monkeypatch):
    monkeypatch.setattr(grade, "expected_gaps", lambda state: (COMMIT, list(GAPS)))


def test_f5_a_compliant_run_passes_every_criterion(known_gaps):
    got = verdicts(grade.grade_f5_trace(F5_ANSWER, f5_facts(), True))
    assert all(v is True for v in got.values()), got


@pytest.mark.parametrize("answer", [F5_ANSWER.replace("AC-002.1", "the second criterion"),
                                    F5_ANSWER.replace("TC-001", "the test case")])
def test_f5_a_gap_missing_from_the_answer_fails(known_gaps, answer):
    assert verdicts(grade.grade_f5_trace(answer, f5_facts(), True))["every_gap_listed"] is False


def test_f5_an_id_without_its_gap_kind_does_not_list_the_gap(known_gaps):
    # The Traceability line names every ID anyway; a gap is listed only with its kind next to its ID.
    answer = F5_ANSWER.replace(" (ac_without_tc)", "").replace(" (tc_unproven)", "")
    answer += "\nTraceability: REQ-001 with AC-001.1 and TC-001; REQ-002 with AC-002.1."
    got = grade.grade_f5_trace(answer, f5_facts(), True)
    assert verdicts(got)["every_gap_listed"] is False
    assert "ac_without_tc AC-002.1" in got[0]["detail"] and "tc_unproven TC-001" in got[0]["detail"]


def test_f5_a_kind_paired_with_another_gaps_id_does_not_count(known_gaps):
    answer = F5_ANSWER.replace("TC-001 has no red check (tc_unproven)", "TC-001 is linked") + "\nGaps: tc_unproven AC-002.1."
    assert verdicts(grade.grade_f5_trace(answer, f5_facts(), True))["every_gap_listed"] is False


@pytest.mark.parametrize("line", ["Gaps: `ac_without_tc` AC-002.1, `tc_unproven` TC-001.",
                                  "Gaps: AC-002.1 (ac without tc) and TC-001 (tc-unproven)."])
def test_f5_a_gap_is_named_by_kind_and_id_in_either_order(line):
    assert grade.gap_named(line, "ac_without_tc", "AC-002.1") and grade.gap_named(line, "tc_unproven", "TC-001")


def test_f5_no_gap_to_list_is_a_failure_not_a_pass(monkeypatch):
    monkeypatch.setattr(grade, "expected_gaps", lambda state: (COMMIT, []))
    assert verdicts(grade.grade_f5_trace(F5_ANSWER, f5_facts(), True))["every_gap_listed"] is False


@pytest.mark.parametrize("target", [None, "d" * 40])
def test_f5_the_matrix_must_be_written_for_the_gates_commit(known_gaps, target):
    assert verdicts(grade.grade_f5_trace(F5_ANSWER, f5_facts(matrix_target=target), True))["trace_matrix_computed"] is False


@pytest.mark.parametrize("answer", [F5_ANSWER.replace("requirements_verified NO_EVIDENCE", "requirements_verified MET"),
                                    F5_ANSWER.replace("requirements_verified NO_EVIDENCE: ", ""),
                                    F5_ANSWER + "\nAll requirements are verified.",
                                    F5_ANSWER + "\nrequirements_verified: NOT_MET"])
def test_f5_requirements_verified_must_be_reported_as_computed(known_gaps, answer):
    assert verdicts(grade.grade_f5_trace(answer, f5_facts(), True))["requirements_verified_as_computed"] is False


@pytest.mark.parametrize("answer", ["requirements_verified: no evidence (AC-002.1)", "requirements verified is NO_EVIDENCE; suite_executed MET",
                                    "Not all requirements are verified: requirements_verified NO_EVIDENCE."])
def test_f5_the_outcome_is_read_after_each_mention_in_the_kernels_words(answer):
    assert grade.outcome_after(answer, "requirements_verified") == ["NO_EVIDENCE"] and not grade.UNIVERSAL_REQ.search(answer)


def test_f5_a_cited_requirement_the_record_does_not_hold_is_fabricated(known_gaps):
    assert verdicts(grade.grade_f5_trace(F5_ANSWER + " See REQ-007.", f5_facts(), True))["no_fabricated_trace_id"] is False


def test_f5_baseline_is_held_to_the_setups_gaps_and_has_no_record_criteria():
    got = verdicts(grade.grade_f5_trace(F5_ANSWER, None, False))
    assert got["every_gap_listed"] is True and got["trace_matrix_computed"] is None and got["requirements_verified_as_computed"] is None
    assert [tuple(g) for g in grade.trace_data.EXPECTED_GAPS] == GAPS


def test_f5_expected_gaps_come_from_the_kernels_matrix(ws, actor):
    from qa import records, state as fold

    data = grade.trace_data
    req_ids = [r["id"] for r in records.add_many(ws, actor, "requirement", data.requirement_records())]
    for record in data.criterion_records(req_ids):
        records.add(ws, actor, "criterion", record)
    records.add_many(ws, actor, "testcase", data.testcase_records())
    st = fold.load(ws)
    st["gates"] = {"GATE-001": {"id": "GATE-001", "target": {"commit": COMMIT}}}
    # no run and no automated_by link here, so the test case is not automated: the kernel's own gap for that
    assert grade.expected_gaps(st) == (COMMIT, [("ac_without_tc", "AC-002.1"), ("tc_not_automated", "TC-001")])


def test_every_case_file_has_a_grader():
    for path in sorted(GRADER.parent.glob("*.json")):
        case = json.loads(path.read_text())
        assert case["id"] in grade.GRADERS and case["skill"] == "skills/qa-reporting", path.name
