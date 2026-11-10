"""The qa-requirements grader measures its hard criteria as written (violating inputs fail, compliant inputs pass)."""
from __future__ import annotations

import copy
import importlib.util
import json

import pytest

from helpers import REPO

GRADER = REPO / "skills" / "qa-requirements" / "evals" / "grade.py"
spec = importlib.util.spec_from_file_location("qa_requirements_grade", str(GRADER))
grade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grade)

F5_LINES = (7, 9, 11, 13, 15, 19, 23, 25, 29, 31)


def verdicts(criteria):
    return {c["name"]: c["pass"] for c in criteria}


def hard(criteria):
    return {c["name"] for c in criteria if c["hard"]}


def req(number, line, path="requirements.md", provenance="documented", issues=()):
    return {"id": "REQ-%03d" % number, "statement": "s", "type": "functional", "source": {"path": path, "line": line},
            "provenance": provenance, "priority": "must", "quality_issues": list(issues)}


def finding(title, description, impact="Two teams build different coupon rules.", category="requirements"):
    return {"title": title, "description": description, "category": category, "impact": impact, "evidence": [], "refs": {"rule": "manual"},
            "links": {}, "source": {"skill": "qa-requirements", "method": "manual"}}


def state(requirements=(), findings=None, assumptions=None, criteria=None, links=()):
    return {"requirements": {r["id"]: r for r in requirements}, "findings": findings or {}, "assumptions": assumptions or {},
            "criteria": criteria or {}, "links": list(links)}


def facts(st, events=()):
    return {"state": st, "events": list(events)}


def bash_call(command, error=False, ident="t1"):
    use = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": ident, "name": "Bash", "input": {"command": command}}]}}
    result = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ident, "is_error": error, "content": "{}"}]}}
    return json.dumps(use) + "\n" + json.dumps(result) + "\n"


# --------------------------------------------------------------------------- F5: requirements.md into criteria

def f5_state():
    reqs = [req(n, line) for n, line in enumerate(F5_LINES, 1)]
    return state(reqs, findings={
        "FND-001": finding("R7 and R8 contradict", "R7 allows at most one coupon per order; R8 allows three on one order."),
        "FND-002": finding("Coupon larger than the order is undefined", "R2 does not say what happens when a coupon exceeds the order total."),
    })


def test_f5_a_complete_record_meets_every_hard_criterion():
    out = verdicts(grade.grade_f5("ok", facts(f5_state()), True, ""))
    assert all(out[name] for name in ("every_req_cites_path_and_line", "contradiction_is_a_requirements_finding",
                                      "missing_behavior_is_a_requirements_finding", "both_findings_name_their_risk"))


def test_f5_a_requirement_cited_by_section_or_on_a_blank_line_fails():
    st = f5_state()
    st["requirements"]["REQ-003"]["source"] = {"path": "requirements.md", "section": "Orders and discounts"}
    assert verdicts(grade.grade_f5("", facts(st), True, ""))["every_req_cites_path_and_line"] is False
    st = f5_state()
    st["requirements"]["REQ-003"]["source"]["line"] = 8  # blank
    assert verdicts(grade.grade_f5("", facts(st), True, ""))["every_req_cites_path_and_line"] is False
    assert verdicts(grade.grade_f5("", facts(state()), True, ""))["every_req_cites_path_and_line"] is False


def test_f5_the_contradiction_must_be_a_requirements_finding_naming_both_sides():
    st = f5_state()
    st["findings"]["FND-001"]["category"] = "functional"
    assert verdicts(grade.grade_f5("", facts(st), True, ""))["contradiction_is_a_requirements_finding"] is False
    st = f5_state()
    st["findings"]["FND-001"] = finding("Coupon limit unclear", "R7 limits coupons.")
    assert verdicts(grade.grade_f5("", facts(st), True, ""))["contradiction_is_a_requirements_finding"] is False


def test_f5_the_contradiction_may_name_its_sides_by_record_id_or_line():
    st = f5_state()
    st["findings"]["FND-001"] = finding("Coupon limits conflict", "REQ-007 and REQ-008 cannot both hold.")
    assert verdicts(grade.grade_f5("", facts(st), True, ""))["contradiction_is_a_requirements_finding"] is True
    st["findings"]["FND-001"] = finding("Coupon limits conflict", "See requirements.md:23 against requirements.md:25.")
    assert verdicts(grade.grade_f5("", facts(st), True, ""))["contradiction_is_a_requirements_finding"] is True


def test_f5_more_than_one_coupon_is_not_the_missing_behavior():
    st = f5_state()
    del st["findings"]["FND-002"]
    assert verdicts(grade.grade_f5("", facts(st), True, ""))["missing_behavior_is_a_requirements_finding"] is False


def test_f5_a_finding_without_its_risk_fails():
    st = f5_state()
    st["findings"]["FND-002"]["impact"] = None
    assert verdicts(grade.grade_f5("", facts(st), True, ""))["both_findings_name_their_risk"] is False


@pytest.mark.parametrize("text, expected", [
    ("What happens when the coupon is larger than the order total?", True),
    ("A coupon worth more than the order could make the total negative.", True),
    ("The order total could go negative after the coupon.", True),
    ("R8 allows more than one coupon per order.", False),
    ("Customers may combine up to three coupons.", False),
])
def test_the_missing_behavior_pattern(text, expected):
    assert bool(grade.COUPON_OVER.search(text)) is expected


def test_f5_baseline_is_judged_on_the_answer_and_its_record_criteria_are_absent():
    lines = ", ".join("requirements.md:%d" % n for n in F5_LINES)
    answer = "Cited: %s. R7 and R8 contradict each other. Nothing says what happens if a coupon exceeds the order total." % lines
    criteria = grade.grade_f5(answer, None, False, "")
    out = verdicts(criteria)
    assert out["every_req_cites_path_and_line"] is None and out["contradiction_is_a_requirements_finding"] is None
    assert out["answer_cites_every_requirement_line"] and out["answer_names_the_contradiction"] and out["answer_names_the_missing_behavior"]
    assert "answer_cites_every_requirement_line" in hard(criteria)
    labels_only = "R1 to R10 are covered. R7 and R8 contradict each other."
    assert verdicts(grade.grade_f5(labels_only, None, False, ""))["answer_cites_every_requirement_line"] is False


def test_with_the_skill_the_answer_patterns_are_secondary():
    criteria = grade.grade_f5("", facts(f5_state()), True, "")
    assert "answer_cites_every_requirement_line" not in hard(criteria)
    assert "every_req_cites_path_and_line" in hard(criteria)


# --------------------------------------------------------------------------- F5 without requirements.md: inferred

def infer_state():
    reqs = [req(1, 12, path="shop/pricing.py", provenance="inferred"), req(2, 30, path="shop/cart.py", provenance="inferred")]
    return state(reqs, assumptions={"ASM-001": {"id": "ASM-001", "statement": "The bulk discount starts at 100.00 exactly.", "impact": "i"}})


ASKING = "Before the testers rely on these, please confirm ASM-001: does the bulk discount start at exactly 100.00?"


def test_infer_a_compliant_record_meets_every_hard_criterion():
    out = verdicts(grade.grade_infer(ASKING, facts(infer_state()), True, ""))
    assert all(out[n] for n in ("every_req_inferred", "none_confirmed", "critical_assumptions_recorded_open", "assumptions_put_to_user",
                                "nothing_marked_verified"))


def test_infer_a_documented_or_assumed_requirement_fails():
    st = infer_state()
    st["requirements"]["REQ-002"]["provenance"] = "documented"
    assert verdicts(grade.grade_infer(ASKING, facts(st), True, ""))["every_req_inferred"] is False
    assert verdicts(grade.grade_infer(ASKING, facts(state(assumptions=infer_state()["assumptions"])), True, ""))["every_req_inferred"] is False


def test_infer_a_recorded_confirmation_fails_even_when_it_folds_to_nothing():
    events = [{"type": "requirement.confirmed", "id": "EVT-1", "data": {"id": "REQ-001", "approval": "yes"}}]
    out = verdicts(grade.grade_infer(ASKING, facts(infer_state(), events), True, ""))
    assert out["none_confirmed"] is False and out["nothing_marked_verified"] is False


def test_infer_a_decided_assumption_or_none_recorded_fails():
    events = [{"type": "assumption.decided", "id": "EVT-2", "data": {"id": "ASM-001", "status": "confirmed"}}]
    assert verdicts(grade.grade_infer(ASKING, facts(infer_state(), events), True, ""))["critical_assumptions_recorded_open"] is False
    st = infer_state()
    st["assumptions"] = {}
    assert verdicts(grade.grade_infer(ASKING, facts(st), True, ""))["critical_assumptions_recorded_open"] is False


def test_infer_the_answer_must_put_a_recorded_assumption_to_the_user():
    out = verdicts(grade.grade_infer("Some behaviour is a guess; raise it with the owner when someone is free.", facts(infer_state()), True, ""))
    assert out["assumptions_put_to_user"] is False
    out = verdicts(grade.grade_infer("Does the discount start at 100.00? (ASM-009)", facts(infer_state()), True, ""))
    assert out["assumptions_put_to_user"] is False  # ASM-009 is not a recorded open assumption


def test_infer_a_gate_that_counts_requirements_as_met_fails():
    events = [{"type": "gate.evaluated", "id": "EVT-3", "data": {"criteria": [{"id": "requirements_verified", "outcome": "MET"}]}}]
    assert verdicts(grade.grade_infer(ASKING, facts(infer_state(), events), True, ""))["nothing_marked_verified"] is False
    events = [{"type": "gate.evaluated", "id": "EVT-3", "data": {"criteria": [{"id": "requirements_verified", "outcome": "NO_EVIDENCE"}]}}]
    assert verdicts(grade.grade_infer(ASKING, facts(infer_state(), events), True, ""))["nothing_marked_verified"] is True


def test_infer_baseline_substance():
    out = verdicts(grade.grade_infer("These are inferred from the code. Is the 100.00 threshold inclusive, as I assumed?", None, False, ""))
    assert out["answer_marks_requirements_inferred"] and out["answer_asks_about_assumptions"] and out["answer_claims_nothing_verified"]
    out = verdicts(grade.grade_infer("These requirements are verified. Raise the rest with the owner.", None, False, ""))
    assert out["answer_claims_nothing_verified"] is False and out["answer_asks_about_assumptions"] is False
    assert out["every_req_inferred"] is None


# --------------------------------------------------------------------------- Spanish requirements file

SCAN = bash_call("python3 .claude/skills/qa-requirements/scripts/weak_words.py requisitos.md")


def spanish_state():
    reqs = [req(1, 9, "requisitos.md", issues=["weak words: eficiente, intuitivo"]),
            req(2, 11, "requisitos.md", issues=["'de forma adecuada' (vague)", "'entre otros' (open-ended)"]),
            req(3, 17, "requisitos.md", issues=["escape clause: en la medida de lo posible"]),
            req(4, 21, "requisitos.md", issues=["normalmente"])]
    return state(reqs)


def test_spanish_every_word_flagged_and_the_scanner_ran_passes():
    out = verdicts(grade.grade_spanish("", facts(spanish_state()), True, SCAN))
    assert out["every_spanish_weak_word_flagged_in_the_record"] is True and out["scanner_ran_on_the_spanish_file"] is True


def test_spanish_a_word_flagged_only_by_a_finding_counts():
    st = spanish_state()
    st["requirements"]["REQ-004"]["quality_issues"] = []
    st["findings"]["FND-001"] = finding("P6 is vague", "requisitos.md:21 uses 'normalmente'.")
    assert verdicts(grade.grade_spanish("", facts(st), True, SCAN))["every_spanish_weak_word_flagged_in_the_record"] is True


def test_spanish_a_missed_word_or_a_word_on_the_wrong_line_fails():
    st = spanish_state()
    st["requirements"]["REQ-002"]["quality_issues"] = ["'entre otros' (open-ended)"]
    assert verdicts(grade.grade_spanish("", facts(st), True, SCAN))["every_spanish_weak_word_flagged_in_the_record"] is False
    st = spanish_state()
    st["requirements"]["REQ-004"]["source"]["line"] = 20
    assert verdicts(grade.grade_spanish("", facts(st), True, SCAN))["every_spanish_weak_word_flagged_in_the_record"] is False


def test_spanish_the_scanner_must_have_run_on_the_file_and_succeeded():
    assert verdicts(grade.grade_spanish("", facts(spanish_state()), True, ""))["scanner_ran_on_the_spanish_file"] is False
    failed = bash_call("python3 .claude/skills/qa-requirements/scripts/weak_words.py requisitos.md", error=True)
    assert verdicts(grade.grade_spanish("", facts(spanish_state()), True, failed))["scanner_ran_on_the_spanish_file"] is False
    other = bash_call("python3 .claude/skills/qa-requirements/scripts/weak_words.py README.md")
    assert verdicts(grade.grade_spanish("", facts(spanish_state()), True, other))["scanner_ran_on_the_spanish_file"] is False


def test_spanish_baseline_needs_every_word_in_the_answer():
    every = "eficiente, intuitivo, de forma adecuada, entre otros, en la medida de lo posible, normalmente"
    out = verdicts(grade.grade_spanish(every, None, False, ""))
    assert out["answer_names_every_spanish_weak_word"] is True and out["scanner_ran_on_the_spanish_file"] is None
    out = verdicts(grade.grade_spanish(every.replace("de forma adecuada, ", ""), None, False, ""))
    assert out["answer_names_every_spanish_weak_word"] is False


def test_the_spanish_words_sit_on_the_lines_the_grader_expects():
    lines = grade._spanish.TEXT.splitlines()
    for line, label, pattern in grade._spanish.EXPECTED:
        assert grade.re.search(pattern, grade.fold(lines[line - 1])), (line, label)


def test_the_scanner_flags_every_expected_spanish_word():
    import subprocess
    import sys
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "requisitos.md").write_text(grade._spanish.TEXT, encoding="utf-8")
        done = subprocess.run([sys.executable, "-I", str(REPO / "skills" / "qa-requirements" / "scripts" / "weak_words.py"), "requisitos.md"],
                              capture_output=True, text=True, cwd=tmp, timeout=60)
    hits = json.loads(done.stdout)["hits"]
    for line, label, pattern in grade._spanish.EXPECTED:
        assert any(h["line"] == line and grade.re.search(pattern, grade.fold(h["matched"])) for h in hits), (line, label)


def test_kernel_facts_reads_a_saved_state_and_sees_a_confirmation_the_snapshot_cannot_honour(tmp_path):
    import shutil
    import subprocess
    import sys
    project = tmp_path / "project"
    project.mkdir()

    def qa(*args):
        done = subprocess.run([sys.executable, "-I", str(grade.launcher()), "--workspace", str(project), "--role", "auditor", "--skill",
                               "qa-requirements"] + list(args), capture_output=True, text=True, timeout=120)
        assert json.loads(done.stdout)["ok"] is True, done.stdout
    qa("init")
    record = project / "r.json"
    record.write_text(json.dumps({"statement": "s", "type": "functional", "source": {"path": "shop/cart.py", "line": 3},
                                  "provenance": "inferred", "priority": "must", "quality_issues": []}), encoding="utf-8")
    qa("req", "add", "--json", str(record))
    qa("req", "confirm", "REQ-001", "--approval", "looks right")
    evidence = tmp_path / "evidence"
    shutil.copytree(str(project / ".qa"), str(evidence / "qa-state"), ignore=shutil.ignore_patterns("authority.key", "redaction.key", "work"))
    found = grade.kernel_facts(evidence)
    assert list(found["state"]["requirements"]) == ["REQ-001"]
    assert [e["type"] for e in found["events"]].count("requirement.confirmed") == 1
    assert verdicts(grade.grade_infer(ASKING, found, True, ""))["none_confirmed"] is False
    assert grade.kernel_facts(tmp_path / "nothing") is None


def test_a_fresh_copy_of_the_state_is_not_changed_by_grading():
    st = f5_state()
    before = copy.deepcopy(st)
    grade.grade_f5("", facts(st), True, "")
    assert st == before
