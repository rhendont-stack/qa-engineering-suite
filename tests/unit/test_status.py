from __future__ import annotations

import pytest

from qa import status
from qa.ingest import Alert, CaseResult, ReportSummary


def suite(p=0, f=0, e=0, s=0, anomalies=()):
    cases = [CaseResult(f"c::p{i}", "p", "c", None, "passed", None, None, None, None, False) for i in range(p)]
    cases += [CaseResult(f"c::f{i}", "f", "c", None, "failed", "assertion", None, None, None, False) for i in range(f)]
    cases += [CaseResult(f"c::e{i}", "e", "c", None, "failed", "error", None, None, None, False) for i in range(e)]
    cases += [CaseResult(f"c::s{i}", "s", "c", None, "not_run", None, "x", None, None, False) for i in range(s)]
    return ReportSummary("junit", "tests", tuple(cases), (), 1, tuple(anomalies))
def alerts(*levels, runs=1):
    return ReportSummary("sarif", "alerts", (), tuple(Alert("t", "R", lv, None, "m", None, None, {}, ()) for lv in levels), runs, ())
def coverage():
    return ReportSummary("lcov", "coverage", (), (), 1, ())
R = dict(report_declared=True)
CASES = [
    ("r1-policy", dict(guard_denied=True, config_drift=True, not_authorized=True), ("blocked", "policy_denied", None, ())),
    ("r1-drift", dict(config_drift=True, not_authorized=True), ("blocked", "config_drift", None, ())),
    ("r1-auth", dict(not_authorized=True), ("blocked", "not_authorized", None, ())),
    ("r2-missing", dict(spawn_error="not_found"), ("blocked", "tool_missing", None, ())),
    ("r2-notexec", dict(spawn_error="not_executable"), ("blocked", "not_executable", None, ())),
    ("r2-127-shell", dict(shell=True, exit_code=127), ("blocked", "tool_missing", "exit_code", ())),
    ("r2-126-shell", dict(shell=True, exit_code=126), ("blocked", "not_executable", "exit_code", ())),
    ("r2-127-argv", dict(exit_code=127), ("failed", "nonzero_exit", "exit_code", ())),
    ("r2-dependency", dict(dependency_unavailable=True), ("blocked", "dependency_unavailable", None, ())),
    ("r3-timeout", dict(timed_out=True), ("inconclusive", "timeout", None, ())),
    ("r3-timeout-partial", dict(timed_out=True, summary=suite(p=1), **R), ("inconclusive", "timeout", "structured", ("partial",))),
    ("r4-missing", dict(exit_code=0, report_error="report_missing", **R), ("inconclusive", "report_missing", "exit_code", ())),
    ("r4-invalid", dict(exit_code=0, report_error="report_invalid", **R), ("inconclusive", "report_invalid", "exit_code", ())),
    ("r5-empty", dict(exit_code=0, summary=suite(), **R), ("inconclusive", "no_tests_collected", "structured", ())),
    ("r5-allow-empty", dict(exit_code=0, summary=suite(), allow_empty=True, **R), ("passed", None, "structured", ("no_tests_collected",))),
    ("r6-fail", dict(exit_code=1, summary=suite(p=1, f=1), **R), ("failed", "assertion", "structured", ())),
    ("r6-error", dict(exit_code=1, summary=suite(e=1), **R), ("failed", "error", "structured", ())),
    ("r6-mismatch", dict(exit_code=0, summary=suite(f=1), **R), ("failed", "assertion", "structured", ("exit_code_mismatch",))),
    ("r7-skipped", dict(exit_code=0, summary=suite(s=2), **R), ("not_run", "all_skipped", "structured", ())),
    ("r8-pass-nonzero", dict(exit_code=1, summary=suite(p=2), **R), ("inconclusive", "exit_code_mismatch", "structured", ())),
    ("r9-pass", dict(exit_code=0, summary=suite(p=2), **R), ("passed", None, "structured", ())),
    ("r9-pass-skips", dict(exit_code=0, summary=suite(p=2, s=1), **R), ("passed", None, "structured", ())),
    ("r10-exit0", dict(exit_code=0), ("passed", None, "exit_code", ())),
    ("orphaned-output", dict(exit_code=0, orphaned_output=True), ("passed", None, "exit_code", ("partial",))),
    ("r11-nonzero", dict(exit_code=2), ("failed", "nonzero_exit", "exit_code", ())),
    ("r11-env", dict(exit_code=1, stderr_tail="ModuleNotFoundError: No module named 'x'"), ("blocked", "environment_error", "exit_code", ())),
    ("sarif-no-runs", dict(exit_code=0, summary=alerts(runs=0), **R), ("inconclusive", "report_invalid", "structured", ())),
    ("sarif-alert", dict(exit_code=1, summary=alerts("error"), **R), ("failed", "assertion", "structured", ())),
    ("sarif-note-only", dict(exit_code=0, summary=alerts("note"), **R), ("passed", None, "structured", ())),
    ("sarif-clean-nonzero", dict(exit_code=2, summary=alerts(), **R), ("inconclusive", "exit_code_mismatch", "structured", ())),
    # T13-R1: any other spawn error is "could not execute"; the process never ran.
    ("t13r1-spawn-failed", dict(spawn_error="spawn_failed"), ("blocked", "not_executable", None, ())),
    # T13-R2: a process ended by a signal (exit_code None) counts as a non-zero exit.
    ("t13r2-signal-no-report", dict(exit_code=None), ("failed", "nonzero_exit", None, ())),
    ("t13r2-signal-env", dict(exit_code=None, stderr_tail="Connection refused"), ("blocked", "environment_error", None, ())),
    ("t13r2-signal-all-pass", dict(exit_code=None, summary=suite(p=1), **R), ("inconclusive", "exit_code_mismatch", "structured", ())),
    ("t13r2-signal-failed-no-mismatch", dict(exit_code=None, summary=suite(f=1), **R), ("failed", "assertion", "structured", ())),
    ("t13r2-signal-skipped", dict(exit_code=None, summary=suite(s=1), **R), ("not_run", "all_skipped", "structured", ("exit_code_mismatch",))),
    ("t13r2-signal-sarif-clean", dict(exit_code=None, summary=alerts(), **R), ("inconclusive", "exit_code_mismatch", "structured", ())),
    # T13-R3: a parsed summary is a report whatever report_declared says; declared without one is missing.
    ("t13r3-summary-undeclared", dict(exit_code=0, summary=suite(p=1)), ("passed", None, "structured", ())),
    ("t13r3-declared-no-summary", dict(exit_code=0, report_declared=True), ("inconclusive", "report_missing", "exit_code", ())),
    # T13-R4: reason precedence, rule 7 and allow_empty.
    ("t13r4-assertion-wins", dict(exit_code=1, summary=suite(f=1, e=1), **R), ("failed", "assertion", "structured", ())),
    ("t13r4-skipped-nonzero", dict(exit_code=1, summary=suite(s=1), **R), ("not_run", "all_skipped", "structured", ("exit_code_mismatch",))),
    ("t13r4-allow-empty-nonzero", dict(exit_code=1, summary=suite(), allow_empty=True, **R), ("inconclusive", "exit_code_mismatch", "structured", ("no_tests_collected",))),
    # T13-R5: report anomalies are kept, deduplicated, in enum order; the reason is never repeated.
    ("t13r5-report-anomalies", dict(exit_code=0, summary=suite(p=1, anomalies=("partial", "retried_in_framework")), orphaned_output=True, **R),
     ("passed", None, "structured", ("retried_in_framework", "partial"))),
    ("t13r5-no-reason-repeat", dict(exit_code=1, summary=suite(p=1, anomalies=("exit_code_mismatch",)), **R),
     ("inconclusive", "exit_code_mismatch", "structured", ())),
    ("t13r5-orphaned-blocked", dict(not_authorized=True, orphaned_output=True), ("blocked", "not_authorized", None, ("partial",))),
    # T13-R6: alert levels; rules 1-4 still apply; unknown levels fail.
    ("t13r6-warning", dict(exit_code=1, summary=alerts("warning"), **R), ("failed", "assertion", "structured", ())),
    ("t13r6-none-clean", dict(exit_code=0, summary=alerts("none", "note"), **R), ("passed", None, "structured", ())),
    ("t13r6-unknown-level", dict(exit_code=0, summary=alerts("critical"), **R), ("failed", "assertion", "structured", ())),
    ("t13r6-alert-exit0", dict(exit_code=0, summary=alerts("error"), **R), ("failed", "assertion", "structured", ())),
    ("t13r6-timeout", dict(timed_out=True, summary=alerts("error"), **R), ("inconclusive", "timeout", "structured", ("partial",))),
    ("t13r6-report-missing", dict(exit_code=0, report_error="report_missing", **R), ("inconclusive", "report_missing", "exit_code", ())),
    # A measurement report never decides: with one in the inputs the rules read as if it were absent (rules 10 and 11).
    ("measurement-exit0", dict(exit_code=0, summary=coverage()), ("passed", None, "exit_code", ())),
    ("measurement-nonzero", dict(exit_code=2, summary=coverage()), ("failed", "nonzero_exit", "exit_code", ())),
    ("measurement-env-error", dict(exit_code=1, summary=coverage(), stderr_tail="ModuleNotFoundError"), ("blocked", "environment_error", "exit_code", ())),
    ("measurement-timeout", dict(timed_out=True, summary=coverage()), ("inconclusive", "timeout", None, ())),
    ("measurement-report-missing", dict(exit_code=0, report_error="report_missing"), ("inconclusive", "report_missing", "exit_code", ())),
    # T13-R7: signatures are case-sensitive, rule 11 only.
    ("t13r7-case-sensitive", dict(exit_code=1, stderr_tail="modulenotfounderror"), ("failed", "nonzero_exit", "exit_code", ())),
    ("t13r7-not-in-rule10", dict(exit_code=0, stderr_tail="ModuleNotFoundError"), ("passed", None, "exit_code", ())),
    ("t13r7-not-with-report", dict(exit_code=1, stderr_tail="ModuleNotFoundError", summary=suite(f=1), **R), ("failed", "assertion", "structured", ())),
]
HEURISTIC = {"r11-env", "t13r2-signal-env", "measurement-env-error"}


@pytest.mark.parametrize("name,kw,want", CASES, ids=[c[0] for c in CASES])
def test_decide(name, kw, want):
    d = status.decide(status.StatusInputs(**kw))
    assert (d.status, d.reason, d.strength, d.anomalies) == want
    assert d.classification == ("heuristic" if name in HEURISTIC else "deterministic")


def test_every_env_signature_is_recognised():
    assert len(status.ENV_ERROR_SIGNATURES) == 9
    for sig in status.ENV_ERROR_SIGNATURES:
        d = status.decide(status.StatusInputs(exit_code=1, stderr_tail="boom " + sig + " boom"))
        assert (d.status, d.reason, d.classification) == ("blocked", "environment_error", "heuristic")


def test_defaults():
    inp = status.StatusInputs()
    assert (inp.exit_code, inp.summary, inp.stderr_tail, inp.allow_empty) == (None, None, "", False)


@pytest.mark.parametrize("old,new,ok", [("failed", "blocked", True), ("blocked", "not_run", True), ("passed", "inconclusive", True),
                                        ("passed", "failed", False), ("failed", "passed", False), ("inconclusive", "passed", False), ("failed", "failed", False),
                                        # T13-R8: only the five canonical statuses count.
                                        ("failed", "weird", False), ("weird", "failed", False), ("passed", "weird", False)])
def test_can_reclassify(old, new, ok):
    assert status.can_reclassify(old, new) is ok
