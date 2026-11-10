"""F9 ``blocked-env``: honesty scenarios, run through the real CLI against the answer key."""
from __future__ import annotations

import sys

from helpers import answer_key


def scenario(scenario_id):
    return next(case for case in answer_key("blocked-env")["scenarios"] if case["id"] == scenario_id)


def run_scenario(project, case):
    command = [sys.executable if word == "python3" else word for word in case["command"]]
    report = ["--report", case["report"]] if "report" in case else []
    return project.cli("run", *report, "--", *command)


def test_f9_zero_tests_is_inconclusive(qa_on_fixture):
    case = scenario("F9-02")
    code, out = run_scenario(qa_on_fixture("blocked-env"), case)
    assert (code, out["status"], out["reason"]) == (case["expected_exit_code"], case["expected_status"], case["expected_reason"])
    assert out["reason"] == "no_tests_collected" and out["tests"]["total"] == 0


def test_f9_junit_failures_with_exit_zero(qa_on_fixture):
    case = scenario("F9-03")
    project = qa_on_fixture("blocked-env")
    code, out = run_scenario(project, case)
    assert (code, out["status"]) == (case["expected_exit_code"], case["expected_status"])
    assert set(case["expected_anomalies"]) <= set(out["anomalies"])
    assert project.results(out["execution"]) == case["expected_tests"]


def test_f9_missing_tool_blocks_and_gate_says_insufficient(qa_on_fixture):
    case = scenario("F9-01")
    project = qa_on_fixture("blocked-env", git=True)
    code, ran = run_scenario(project, case)
    assert (code, ran["status"], ran["reason"]) == (case["expected_exit_code"], case["expected_status"], case["expected_reason"])
    code, release = project.cli("--role", "evaluator", "--independence", "I2", "gate", "evaluate", "--profile", case["expected_gate"]["profile"])
    assert (code, release["verdict"]) == (3, case["expected_gate"]["verdict"])
    code, change = project.cli("gate", "evaluate", "--profile", "change")
    suite = next(c for c in change["criteria"] if c["type"] == "suite_executed")
    assert code == 3 and suite["outcome"] == "NO_EVIDENCE" and ran["execution"] in suite["detail"]
