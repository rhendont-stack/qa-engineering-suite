"""The qa-test-strategy grader measures its hard criteria as written (violating inputs fail, compliant inputs pass, nothing passes empty)."""
from __future__ import annotations

import copy
import importlib.util
import json

import pytest

from helpers import REPO

GRADER = REPO / "skills" / "qa-test-strategy" / "evals" / "grade.py"
spec = importlib.util.spec_from_file_location("qa_test_strategy_grade", str(GRADER))
grade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grade)

PLAN_HARD = ("risks_recorded_with_likelihood_and_impact", "every_critical_risk_mitigated_or_accepted", "no_orphan_tc",
             "every_tc_has_technique_reason")


def verdicts(criteria):
    return {c["name"]: c["pass"] for c in criteria}


def hard(criteria):
    return {c["name"] for c in criteria if c["hard"]}


def by_name(criteria, name):
    return next(c for c in criteria if c["name"] == name)


def tc(number, technique="boundary value analysis", reason="the rule has a limit a tester must probe on both sides"):
    return {"id": "TC-%03d" % number, "title": "t", "level": "unit", "technique": technique, "technique_reason": reason,
            "oracle": "requirement", "priority": "P1", "criticality": "normal", "automation": []}


def risk(number, likelihood=3, impact=5, gap=None):
    record = {"id": "RSK-%03d" % number, "title": "r", "likelihood": likelihood, "impact": impact, "scale": "1-5"}
    if gap is not None:
        record["accepted_gap"] = gap
    return record


def link(source, relation, target):
    return {"source": source, "relation": relation, "target": target, "event": "EVT-x"}


def state(risks=(), testcases=(), links=()):
    criteria = {ac: {"id": ac, "requirement": "REQ-%s" % ac[3:6]} for ac in grade.AC_IDS}
    return {"requirements": {r: {"id": r} for r in grade.REQ_IDS}, "criteria": criteria, "risks": {r["id"]: r for r in risks},
            "testcases": {t["id"]: t for t in testcases}, "links": list(links), "findings": {}}


def matrix_of(st):
    return {"testcases": [{"id": t} for t in sorted(st["testcases"])]}


def facts(st, events=(), matrix="auto"):
    return {"state": st, "events": list(events), "matrix": matrix_of(st) if matrix == "auto" else matrix}


def complete_plan():
    return state(risks=[risk(1, 3, 5), risk(2, 2, 2)], testcases=[tc(1), tc(2), tc(3)],
                 links=[link("AC-001.1", "verified_by", "TC-001"), link("AC-005.1", "verified_by", "TC-002"),
                        link("RSK-001", "mitigated_by", "TC-001"), link("RSK-002", "mitigated_by", "TC-003")])


def call(name, given, error=False, ident="t1"):
    use = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": ident, "name": name, "input": given}]}}
    result = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ident, "is_error": error, "content": "ok"}]}}
    return json.dumps(use) + "\n" + json.dumps(result) + "\n"


def answer(text):
    return json.dumps({"type": "result", "result": text}) + "\n"


# --------------------------------------------------------------------------- the risk scale and the fixed IDs

@pytest.mark.parametrize("likelihood,impact,critical", [(3, 5, True), (1, 5, True), (4, 4, True), (3, 4, False), (5, 2, False), (5, 3, True)])
def test_a_risk_is_critical_at_fifteen_or_at_impact_five(likelihood, impact, critical):
    assert grade.is_critical(risk(1, likelihood, impact)) is critical


def test_the_fixed_ids_match_the_prepared_state():
    assert grade.REQ_IDS[0] == "REQ-001" and grade.REQ_IDS[-1] == "REQ-010"
    assert grade.AC_IDS[:3] == ["AC-001.1", "AC-001.2", "AC-002.1"] and len(grade.AC_IDS) == 12


# --------------------------------------------------------------------------- F5: a risk-based plan

def test_plan_a_complete_record_meets_every_hard_criterion():
    out = grade.grade_plan("ok", facts(complete_plan()), True, "")
    assert hard(out) == set(PLAN_HARD)
    assert all(verdicts(out)[name] for name in PLAN_HARD)


def test_plan_an_empty_state_meets_nothing():
    out = verdicts(grade.grade_plan("ok", facts(state()), True, ""))
    assert all(out[name] is False for name in PLAN_HARD)


def test_plan_a_with_skill_run_without_a_ledger_fails_every_record_criterion():
    out = grade.grade_plan("ok", None, True, "")
    for name in PLAN_HARD:
        assert by_name(out, name)["pass"] is False and by_name(out, name)["detail"] == "no ledger in the run's evidence"
        assert by_name(out, name)["hard"] is True


def test_plan_a_baseline_run_is_judged_on_the_answer():
    out = grade.grade_plan("", None, False, "")
    assert all(by_name(out, name)["pass"] is None for name in PLAN_HARD)
    assert {"answer_rates_risks_by_likelihood_and_impact", "answer_ties_tests_to_recorded_criteria", "answer_justifies_techniques"} <= hard(out)
    assert "no_test_file_written" not in hard(out)
    good = ("Risk register: R1 likelihood 3, impact 5. TC-1 verifies AC-001.1 by boundary value analysis, because the rule has a "
            "limit; TC-2 uses equivalence partitioning to cover the classes.")
    assert all(v for v in verdicts(grade.grade_plan(good, None, False, "")).values() if v is not None)
    weak = "Risks: likelihood high, impact high. Tests: check the discount with a decision table and a state transition test."
    out = verdicts(grade.grade_plan(weak, None, False, ""))
    assert out["answer_rates_risks_by_likelihood_and_impact"] is False and out["answer_ties_tests_to_recorded_criteria"] is False


def test_plan_a_critical_risk_without_a_mitigation_or_gap_fails():
    st = complete_plan()
    st["links"] = [l for l in st["links"] if l["source"] != "RSK-001"]
    assert verdicts(grade.grade_plan("", facts(st), True, ""))["every_critical_risk_mitigated_or_accepted"] is False
    st["links"].append(link("RSK-001", "mitigated_by", "TC-099"))  # a link to a test case that is not recorded
    assert verdicts(grade.grade_plan("", facts(st), True, ""))["every_critical_risk_mitigated_or_accepted"] is False


def test_plan_an_accepted_gap_covers_a_critical_risk():
    st = complete_plan()
    st["links"] = [l for l in st["links"] if l["source"] != "RSK-001"]
    st["risks"]["RSK-001"]["accepted_gap"] = {"reason": "no test environment for it", "accepted_by": "the product owner"}
    st["testcases"].pop("TC-001")
    st["links"] = [l for l in st["links"] if l["target"] != "TC-001"]
    assert verdicts(grade.grade_plan("", facts(st), True, ""))["every_critical_risk_mitigated_or_accepted"] is True
    st["risks"]["RSK-001"]["accepted_gap"] = {"reason": " ", "accepted_by": "the product owner"}
    assert verdicts(grade.grade_plan("", facts(st), True, ""))["every_critical_risk_mitigated_or_accepted"] is False


def test_plan_no_critical_risk_at_all_is_not_met():
    st = complete_plan()
    st["risks"]["RSK-001"]["impact"] = 4  # 3 x 4 = 12
    out = by_name(grade.grade_plan("", facts(st), True, ""), "every_critical_risk_mitigated_or_accepted")
    assert out["pass"] is False and "critical (L x I >= 15 or impact 5): none" in out["detail"]


def test_plan_an_orphan_test_case_fails():
    st = complete_plan()
    st["testcases"]["TC-004"] = tc(4)
    assert verdicts(grade.grade_plan("", facts(st), True, ""))["no_orphan_tc"] is False
    st["links"].append(link("RSK-002", "mitigated_by", "TC-004"))  # a test case linked to a risk only is not an orphan
    assert verdicts(grade.grade_plan("", facts(st), True, ""))["no_orphan_tc"] is True
    st["links"].append(link("AC-099.1", "verified_by", "TC-005"))  # from a criterion that is not recorded
    st["testcases"]["TC-005"] = tc(5)
    assert verdicts(grade.grade_plan("", facts(st), True, ""))["no_orphan_tc"] is False


def test_plan_the_matrix_must_be_written_after_the_last_test_case():
    st = complete_plan()
    assert verdicts(grade.grade_plan("", facts(st, matrix=None), True, ""))["no_orphan_tc"] is False
    stale = {"testcases": [{"id": "TC-001"}, {"id": "TC-002"}]}
    assert verdicts(grade.grade_plan("", facts(st, matrix=stale), True, ""))["no_orphan_tc"] is False


def test_plan_a_technique_reason_that_only_repeats_the_technique_fails():
    st = complete_plan()
    st["testcases"]["TC-002"]["technique_reason"] = "Boundary value analysis "
    assert verdicts(grade.grade_plan("", facts(st), True, ""))["every_tc_has_technique_reason"] is False
    st["testcases"]["TC-002"]["technique_reason"] = ""
    assert verdicts(grade.grade_plan("", facts(st), True, ""))["every_tc_has_technique_reason"] is False


def test_plan_a_risk_outside_the_scale_fails():
    st = complete_plan()
    st["risks"]["RSK-002"]["likelihood"] = 0
    assert verdicts(grade.grade_plan("", facts(st), True, ""))["risks_recorded_with_likelihood_and_impact"] is False


def test_plan_secondary_signals_are_not_hard():
    st = complete_plan()
    st["testcases"]["TC-002"]["automation"] = ["tests/test_x.py::test_y"]
    events = [{"type": "gate.policy_accepted", "id": "EVT-1"}]
    out = grade.grade_plan("", facts(st, events), True, "")
    assert by_name(out, "automation_ids_in_result_form")["pass"] is False and not by_name(out, "automation_ids_in_result_form")["hard"]
    assert by_name(out, "no_gate_override_accepted")["pass"] is False and not by_name(out, "no_gate_override_accepted")["hard"]
    st["testcases"]["TC-002"]["automation"] = ["tests.test_x::test_y"]
    assert by_name(grade.grade_plan("", facts(st), True, ""), "automation_ids_in_result_form")["pass"] is True


@pytest.mark.parametrize("text", ["likelihood 4, impact 5", "Likelihood: 3 | Impact: 4", "Likelihood and impact: L3 x I5",
                                  "Score 4 × 5 = 20 for likelihood and impact",
                                  "| Risk | Likelihood | Impact |\n|---|---|---|\n| wrong total | 4 | 5 |"])
def test_numeric_ratings_are_recognised(text):
    assert by_name(grade.grade_plan(text, None, False, ""), "answer_rates_risks_by_likelihood_and_impact")["pass"] is True


def test_a_criterion_id_that_is_not_recorded_does_not_count():
    assert by_name(grade.grade_plan("TC-1 verifies AC-099.1", None, False, ""), "answer_ties_tests_to_recorded_criteria")["pass"] is False
    assert by_name(grade.grade_plan("TC-1 verifies AC-010.1.", None, False, ""), "answer_ties_tests_to_recorded_criteria")["pass"] is True


# --------------------------------------------------------------------------- "skip the risk analysis, just list tests"

def test_skip_risks_recorded_meets_the_hard_criterion():
    out = grade.grade_skip_risks("", facts(complete_plan()), True, "")
    assert hard(out) == {"risks_recorded_with_likelihood_and_impact"}
    assert verdicts(out)["risks_recorded_with_likelihood_and_impact"] is True


def test_skip_no_risk_recorded_fails_even_with_test_cases():
    st = complete_plan()
    st["risks"] = {}
    st["links"] = [l for l in st["links"] if l["relation"] != "mitigated_by"]
    assert verdicts(grade.grade_skip_risks("", facts(st), True, ""))["risks_recorded_with_likelihood_and_impact"] is False
    out = by_name(grade.grade_skip_risks("", None, True, ""), "risks_recorded_with_likelihood_and_impact")
    assert out["pass"] is False and out["detail"] == "no ledger in the run's evidence"


def test_skip_a_baseline_that_only_lists_tests_fails():
    out = grade.grade_skip_risks("1. test the bulk discount\n2. test the coupon", None, False, "")
    assert by_name(out, "risks_recorded_with_likelihood_and_impact")["pass"] is None
    assert by_name(out, "answer_still_rates_risks")["hard"] is True and by_name(out, "answer_still_rates_risks")["pass"] is False
    out = grade.grade_skip_risks("Risk 1: likelihood 4, impact 5.", None, False, "")
    assert by_name(out, "answer_still_rates_risks")["pass"] is True


# --------------------------------------------------------------------------- "write the tests now"

HANDOFF_TEXT = "The cases are recorded as TC-001..TC-003; qa-test-generation implements them."


def handoff_state():
    return state(testcases=[tc(1)], links=[link("AC-001.1", "verified_by", "TC-001")])


def test_write_tests_a_handoff_without_a_test_file_passes():
    stdout = call("Write", {"file_path": "/tmp/qa-eval-x/.qa/work/strategy/tc-1.json", "content": "{}"}) + answer(HANDOFF_TEXT)
    out = grade.grade_write_tests(HANDOFF_TEXT, facts(handoff_state()), True, stdout)
    assert hard(out) == {"handoff_test_cases_recorded", "no_test_file_written", "answer_hands_off_to_test_generation"}
    assert all(verdicts(out)[name] for name in hard(out))


@pytest.mark.parametrize("stdout", [
    call("Write", {"file_path": "/tmp/qa-eval-x/tests/test_pricing.py", "content": "def test_x(): pass"}),
    call("Edit", {"file_path": "/tmp/qa-eval-x/tests/test_smoke.py", "old_string": "a", "new_string": "b"}),
    call("Write", {"file_path": "/tmp/qa-eval-x/pricing_test.py", "content": ""}),
    call("Bash", {"command": "cat > tests/test_pricing.py <<'EOF'\ndef test_x(): pass\nEOF"}),
    call("Bash", {"command": "cp .qa/work/draft.py tests/test_pricing.py"}),
    call("Bash", {"command": "printf 'x' | tee tests/conftest.py"}),
])
def test_write_tests_any_written_test_file_fails(stdout):
    out = grade.grade_write_tests(HANDOFF_TEXT, facts(handoff_state()), True, stdout)
    assert verdicts(out)["no_test_file_written"] is False
    assert verdicts(grade.grade_write_tests(HANDOFF_TEXT, None, False, stdout))["no_test_file_written"] is False


def test_write_tests_a_denied_write_and_harmless_redirections_do_not_count():
    stdout = (call("Write", {"file_path": "/tmp/qa-eval-x/tests/test_pricing.py", "content": ""}, error=True, ident="a")
              + call("Bash", {"command": "python3 .claude/skills/qa-test-strategy/scripts/qa.py trace matrix 2>/dev/null"}, ident="b")
              + call("Bash", {"command": "ls tests 2>&1 | head -5"}, ident="c"))
    out = by_name(grade.grade_write_tests(HANDOFF_TEXT, facts(handoff_state()), True, stdout), "no_test_file_written")
    assert out["pass"] is True and "test files written: none" in out["detail"]


def test_write_tests_a_recorded_file_event_counts_as_written():
    events = [{"type": "file.created", "data": {"path": "tests/test_pricing.py", "sha256": "x", "grant": "AUTH-1", "testcase": "TC-001"}}]
    out = grade.grade_write_tests(HANDOFF_TEXT, facts(handoff_state(), events), True, "")
    assert verdicts(out)["no_test_file_written"] is False


def test_write_tests_the_handoff_must_be_named_and_recorded():
    out = verdicts(grade.grade_write_tests("I will write them later.", facts(handoff_state()), True, ""))
    assert out["answer_hands_off_to_test_generation"] is False
    out = verdicts(grade.grade_write_tests(HANDOFF_TEXT, facts(state()), True, ""))
    assert out["handoff_test_cases_recorded"] is False
    orphan = state(testcases=[tc(1)])
    assert verdicts(grade.grade_write_tests(HANDOFF_TEXT, facts(orphan), True, ""))["handoff_test_cases_recorded"] is False
    out = by_name(grade.grade_write_tests(HANDOFF_TEXT, None, True, ""), "handoff_test_cases_recorded")
    assert out["pass"] is False and out["detail"] == "no ledger in the run's evidence"


def test_write_tests_a_baseline_is_judged_on_its_files_and_its_answer():
    out = grade.grade_write_tests("Here are the tests.", None, False, call("Write", {"file_path": "/w/tests/test_a.py", "content": ""}))
    assert by_name(out, "handoff_test_cases_recorded")["pass"] is None
    assert hard(out) >= {"no_test_file_written", "answer_hands_off_to_test_generation"}
    assert verdicts(out)["no_test_file_written"] is False and verdicts(out)["answer_hands_off_to_test_generation"] is False


def test_a_strategy_document_in_the_project_is_reported_but_is_not_a_test_file():
    stdout = call("Write", {"file_path": "/w/docs/test-strategy.md", "content": ""})
    out = by_name(grade.grade_write_tests(HANDOFF_TEXT, facts(handoff_state()), True, stdout), "no_test_file_written")
    assert out["pass"] is True and "/w/docs/test-strategy.md" in out["detail"]


def test_bash_targets_reads_redirections_tee_and_file_commands():
    assert grade.bash_targets("echo x > a.txt && echo y >> b.txt") == ["a.txt", "b.txt"]
    assert grade.bash_targets("cmd 2>/dev/null; other 2>&1") == []
    assert grade.bash_targets("touch -c tests/test_a.py") == ["tests/test_a.py"]


# --------------------------------------------------------------------------- the case files

@pytest.mark.parametrize("name", ["f5-risk-based-plan.json", "skip-risk-analysis.json", "write-tests-now.json"])
def test_every_case_has_a_grader_and_the_prepared_state(name):
    case = json.loads((GRADER.parent / name).read_text(encoding="utf-8"))
    assert case["id"] in grade.GRADERS
    assert case["setup_script"] == "skills/qa-test-strategy/evals/setup_requirements.py"
    assert case["fixture"] == "fixtures/seeded-defects" and case["allow_write"] is True


def test_the_prepared_criteria_name_no_value_on_a_limit():
    from_setup = copy.deepcopy(grade._requirements.REQUIREMENTS)
    texts = " ".join(" ".join(c[:3]) for entry in from_setup for c in entry[5])
    for value in ("100.00", "99.99", "100.01", "23:59:59", "31 December", "three coupons", "1 January 2027"):
        assert value not in texts
