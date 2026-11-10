"""``qa report check --ledger``: a report may cite evidence that other ledgers hold, and every claim is checked against
the ledger that owns the ID. Each extra ledger is opened read-only and must verify like ``qa ledger verify``."""
from __future__ import annotations

import copy
import os
import shutil

import pytest

from helpers import WRITE_JUNIT, req
from qa import config, execution, report, state
from qa.report import check_markdown
from qa.workspace import init_workspace


def _kinds(problems):
    return sorted(p["kind"] if isinstance(p, dict) else p.kind for p in problems)


def _tree(path):
    """Every file under ``path`` with its size and mtime, to prove nothing was written."""
    seen = {}
    for folder, _dirs, names in os.walk(str(path)):
        for name in names:
            full = os.path.join(folder, name)
            info = os.lstat(full)
            seen[full] = (info.st_size, info.st_mtime_ns)
    return seen


@pytest.fixture
def foreign(tmp_path, actor, py):
    """A second workspace whose ledger holds one failed execution; (workspace, execution id)."""
    other = init_workspace(tmp_path / "other project")[0]
    config.record_initialized(other, actor)
    config.set_value(other, actor, "project.trust", "trusted", "test approval")
    exe = execution.execute(other, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "fail"))["execution"]
    return other, exe


def test_a_foreign_id_resolves_with_ledger_and_fails_without(ws, foreign):
    other, exe = foreign
    text = "%s failed.\n" % exe
    assert _kinds(report.check(ws, text)) == ["uncited_claim", "unknown_id"]
    assert report.check(ws, text, ledgers=[str(other.qa_dir)]) == []


def test_the_ledger_directory_names_a_ledger_too(ws, foreign):
    other, exe = foreign
    assert report.check(ws, "%s failed.\n" % exe, ledgers=[str(other.path("ledger"))]) == []


def test_a_wrong_status_claim_against_a_foreign_id_fails(ws, foreign):
    other, exe = foreign
    assert _kinds(report.check(ws, "%s passed.\n" % exe, ledgers=[str(other.qa_dir)])) == ["status_mismatch"]


def test_a_tampered_extra_ledger_fails_the_check(ws, foreign):
    other, exe = foreign
    path = next(other.path("ledger").glob("*.jsonl"))
    path.write_bytes(path.read_bytes() + b"not an event\n")
    problems = report.check(ws, "%s failed.\n" % exe, ledgers=[str(other.qa_dir)])
    assert "extra_ledger_invalid" in _kinds(problems)


def test_tampered_evidence_in_an_extra_ledger_fails_the_check(ws, foreign):
    other, exe = foreign
    victim = next(p for p in other.path("evidence", exe).iterdir() if p.name != "manifest.json")
    victim.write_bytes(victim.read_bytes() + b"x")
    assert "extra_ledger_invalid" in _kinds(report.check(ws, "%s failed.\n" % exe, ledgers=[str(other.qa_dir)]))


def test_a_path_that_holds_no_ledger_fails_the_check(ws, tmp_path):
    (tmp_path / "empty").mkdir()
    assert _kinds(report.check(ws, "Nothing.\n", ledgers=[str(tmp_path / "empty")])) == ["extra_ledger_invalid"]
    assert _kinds(report.check(ws, "Nothing.\n", ledgers=[str(tmp_path / "missing")])) == ["extra_ledger_invalid"]


def test_a_symlinked_ledger_path_is_refused(ws, foreign, tmp_path):
    other, exe = foreign
    link = tmp_path / "link"
    link.symlink_to(other.qa_dir, target_is_directory=True)
    assert _kinds(report.check(ws, "%s failed.\n" % exe, ledgers=[str(link)])) == ["extra_ledger_invalid", "uncited_claim", "unknown_id"]


def test_a_symlinked_ledger_directory_is_refused(ws, foreign, tmp_path):
    other, exe = foreign
    shell = tmp_path / "shell"
    shell.mkdir()
    (shell / "ledger").symlink_to(other.path("ledger"), target_is_directory=True)
    assert _kinds(report.check(ws, "%s failed.\n" % exe, ledgers=[str(shell)])) == ["extra_ledger_invalid", "uncited_claim", "unknown_id"]


def test_a_symlink_to_the_ledger_directory_itself_is_refused(ws, foreign, tmp_path):
    other, exe = foreign
    link = tmp_path / "ledger"
    link.symlink_to(other.path("ledger"), target_is_directory=True)
    assert "extra_ledger_invalid" in _kinds(report.check(ws, "%s failed.\n" % exe, ledgers=[str(link)]))


def test_an_extra_ledger_is_never_written(ws, foreign):
    other, exe = foreign
    before = _tree(other.qa_dir)
    report.check(ws, "%s failed.\n" % exe, ledgers=[str(other.qa_dir)])
    assert _tree(other.qa_dir) == before


def test_the_same_ledger_given_twice_is_no_conflict(ws, foreign, tmp_path):
    other, exe = foreign
    twin = tmp_path / "twin"
    shutil.copytree(str(other.qa_dir), str(twin))
    assert report.check(ws, "%s failed.\n" % exe, ledgers=[str(other.qa_dir), str(twin)]) == []


def _states(exe, status, scope="smoke"):
    base = state.empty_state()
    base["executions"][exe] = {"status": status, "scope": scope, "evidence_manifest": None, "ingested": []}
    return base


EXE = "EXE-" + "0" * 25 + "A"


def test_an_id_with_conflicting_records_in_two_ledgers_is_a_problem():
    assert "conflicting_id" in _kinds(check_markdown("%s failed.\n" % EXE, _states(EXE, "failed"), [_states(EXE, "passed")]))


def test_an_execution_id_whose_records_differ_in_any_field_is_a_problem():
    one, two = _states(EXE, "failed", "smoke"), _states(EXE, "failed", "full")
    assert "conflicting_id" in _kinds(check_markdown("%s is cited.\n" % EXE, one, [two]))


def _gated(verdict, report_gate="GATE-001", **numbered):
    st = state.empty_state()
    st["gates"]["GATE-001"] = {"verdict": verdict}
    st["reports"]["RPT-001"] = {"gate": report_gate}
    for key, ids in numbered.items():
        for one in ids:
            st[key][one] = {}
    return st


def test_a_bare_numbered_id_held_by_two_ledgers_is_ambiguous_whatever_the_verdicts():  # I2
    text = "GATE-001 is NOT_READY.\n"
    assert _kinds(check_markdown(text, _gated("NOT_READY"), [_gated("NOT_READY")])) == ["ambiguous_id"]
    assert _kinds(check_markdown(text, _gated("NOT_READY"), [_gated("READY")])) == ["ambiguous_id"]


@pytest.mark.parametrize("token, key", [("RPT-001", None), ("AUTH-001", "authorizations"), ("PLAN-0001", "plans"),
                                        ("FND-001", "findings")])
def test_every_numbered_kind_is_ambiguous_when_bare_and_two_ledgers_hold_it(token, key):
    extra = {key: [token]} if key else {}
    text = "%s is cited.\n" % token
    assert _kinds(check_markdown(text, _gated("READY", **extra), [_gated("READY", **extra)])) == ["ambiguous_id"]
    assert check_markdown("b:%s is cited.\n" % token, _gated("READY", **extra), [_gated("READY", **extra)], ["b"]) == []


def test_a_qualified_id_is_judged_by_the_ledger_its_alias_names():
    own, f4 = _gated("READY"), _gated("NOT_READY")
    assert check_markdown("f4:GATE-001 is NOT_READY.\n", own, [f4], ["f4"]) == []
    assert _kinds(check_markdown("f4:GATE-001 is READY.\n", own, [f4], ["f4"])) == ["status_mismatch"]
    assert check_markdown("project:GATE-001 is READY.\n", own, [f4], ["f4"]) == []


def test_a_qualified_id_with_an_unknown_alias_or_absent_from_that_ledger_is_unknown():
    own, f4 = _gated("READY"), state.empty_state()
    assert _kinds(check_markdown("f9:GATE-001 is READY.\n", own, [f4], ["f4"])) == ["uncited_claim", "unknown_id"]
    assert _kinds(check_markdown("f4:GATE-001 is READY.\n", own, [f4], ["f4"])) == ["uncited_claim", "unknown_id"]


def test_a_bare_numbered_id_held_by_one_ledger_still_resolves():
    assert check_markdown("GATE-001 is NOT_READY.\n", state.empty_state(), [_gated("NOT_READY")], ["f4"]) == []


def test_execution_ids_keep_the_any_field_rule_when_qualified_or_bare():
    one, two = _states(EXE, "failed", "smoke"), _states(EXE, "failed", "full")
    assert "conflicting_id" in _kinds(check_markdown("%s failed.\n" % EXE, one, [two], ["b"]))
    assert check_markdown("b:%s failed.\n" % EXE, one, [two], ["b"]) == []


# --------------------------------------------------------------------------- verdict coverage (I3)

def test_a_verdict_is_judged_against_every_cited_gate():
    own, f8 = _gated("READY"), state.empty_state()
    f8["gates"]["GATE-001"] = {"verdict": "NOT_READY"}
    f8["gates"]["GATE-002"] = {"verdict": "NOT_READY"}
    assert check_markdown("f8:GATE-001 and f8:GATE-002 are both NOT_READY.\n", own, [f8], ["f8"]) == []
    f8["gates"]["GATE-002"]["verdict"] = "READY"
    assert _kinds(check_markdown("f8:GATE-001 and f8:GATE-002 are both NOT_READY.\n", own, [f8], ["f8"])) == ["status_mismatch"]


def test_a_report_id_stands_for_the_verdict_of_its_gate():
    assert check_markdown("RPT-001 says NOT_READY.\n", _gated("NOT_READY")) == []
    assert _kinds(check_markdown("RPT-001 says READY.\n", _gated("NOT_READY"))) == ["status_mismatch"]
    assert _kinds(check_markdown("RPT-001 says READY.\n", _gated("NOT_READY", report_gate=None))) == ["uncited_claim"]


def test_a_verdict_beside_only_an_execution_id_is_uncited():
    assert _kinds(check_markdown("%s failed, so NOT_READY.\n" % EXE, _states(EXE, "failed"))) == ["uncited_claim"]


def test_an_expected_label_leaves_only_the_token_it_labels_unjudged():
    st = _gated("NOT_READY")
    assert check_markdown("GATE-001 is NOT_READY (expected: INSUFFICIENT_EVIDENCE).\n", st) == []
    assert _kinds(check_markdown("Expected: `INSUFFICIENT_EVIDENCE` for this fixture.\n", st)) == ["unpaired_expectation"]
    assert _kinds(check_markdown("GATE-001 is READY (expected: INSUFFICIENT_EVIDENCE).\n", st)) == ["status_mismatch"]
    assert _kinds(check_markdown("GATE-001 is NOT_READY, INSUFFICIENT_EVIDENCE was expected.\n", st)) == ["status_mismatch"]
    assert _kinds(check_markdown("The verdict READY was expected: INSUFFICIENT_EVIDENCE.\n", st)) == ["uncited_claim"]


def test_a_claim_is_checked_against_the_ledger_that_owns_the_id():
    other = _states(EXE, "failed")
    assert check_markdown("%s failed.\n" % EXE, state.empty_state(), [other]) == []
    assert _kinds(check_markdown("%s passed.\n" % EXE, state.empty_state(), [other])) == ["status_mismatch"]


def test_a_universal_claim_is_backed_by_a_full_scope_pass_in_another_ledger():
    other = _states(EXE, "passed", "full")
    assert check_markdown("All tests passed (%s).\n" % EXE, state.empty_state(), [other]) == []


def test_the_cli_takes_a_repeatable_ledger_option(ws, qa_cli, foreign, tmp_path):
    other, exe = foreign
    (tmp_path / "r.md").write_text("%s failed.\n" % exe)
    code, out = qa_cli("report", "check", str(tmp_path / "r.md"))
    assert code == 1 and sorted(p["kind"] for p in out["problems"]) == ["uncited_claim", "unknown_id"]
    code, out = qa_cli("report", "check", str(tmp_path / "r.md"), "--ledger", str(other.qa_dir))
    assert (code, out["problems"]) == (0, [])
    code, out = qa_cli("report", "check", str(tmp_path / "r.md"), "--ledger", str(tmp_path / "nowhere"), "--ledger", str(other.qa_dir))
    assert code == 1 and "extra_ledger_invalid" in [p["kind"] for p in out["problems"]]


def test_the_cli_takes_an_alias_and_the_report_cites_through_it(ws, qa_cli, foreign, tmp_path):
    other, exe = foreign
    (tmp_path / "r.md").write_text("other:%s failed.\n" % exe)
    code, out = qa_cli("report", "check", str(tmp_path / "r.md"), "--ledger", "other=" + str(other.qa_dir))
    assert (code, out["problems"]) == (0, [])
    code, out = qa_cli("report", "check", str(tmp_path / "r.md"))
    assert code == 1 and sorted(p["kind"] for p in out["problems"]) == ["uncited_claim", "unknown_id"]


@pytest.mark.parametrize("values", [["Bad_Alias={path}"], ["project={path}"], ["a={path}", "a={path}"]],
                         ids=["shape", "reserved", "twice"])
def test_a_bad_or_repeated_alias_is_a_problem(ws, foreign, values):
    other, exe = foreign
    problems = report.check(ws, "Nothing.\n", ledgers=[v.format(path=other.qa_dir) for v in values])
    assert _kinds(problems) == ["extra_ledger_invalid"]


def test_a_path_with_a_slash_before_the_equals_sign_is_a_plain_path(ws, foreign, tmp_path):
    other, exe = foreign
    odd = tmp_path / "x=y"
    shutil.copytree(str(other.qa_dir), str(odd / ".qa"))
    assert report.check(ws, "%s failed.\n" % exe, ledgers=[str(odd)]) == []


# --------------------------------------------------------------------------- expectations pair, status words attribute

def _two_runs():
    st = state.empty_state()
    st["executions"][EXE] = {"status": "passed", "scope": "smoke", "evidence_manifest": None, "ingested": []}
    st["executions"][EXE_B] = {"status": "failed", "scope": "smoke", "evidence_manifest": None, "ingested": []}
    return st


EXE_B = "EXE-" + "0" * 25 + "B"


def test_an_expected_label_alone_is_unpaired():
    st = _gated("NOT_READY")
    assert _kinds(check_markdown("GATE-001 is expected: READY.\n", st)) == ["unpaired_expectation"]
    assert _kinds(check_markdown("%s is expected: passed.\n" % EXE, _two_runs())) == ["unpaired_expectation"]
    assert _kinds(check_markdown("GATE-001 is NOT_READY (expected: passed).\n", st)) == ["unpaired_expectation"]  # wrong kind


def test_an_expected_label_beside_a_measurement_is_judged():
    st = _gated("NOT_READY")
    assert check_markdown("GATE-001 is NOT_READY (expected: NOT_READY).\n", st) == []
    assert _kinds(check_markdown("GATE-001 is READY, expected: NOT_READY.\n", st)) == ["status_mismatch"]
    assert check_markdown("%s failed (expected: passed).\n" % EXE_B, _two_runs()) == []


def test_status_words_are_judged_against_the_nearest_preceding_id():
    st = _two_runs()
    assert check_markdown("%s passed and %s failed.\n" % (EXE, EXE_B), st) == []
    assert _kinds(check_markdown("%s failed and %s passed.\n" % (EXE, EXE_B), st)) == ["status_mismatch", "status_mismatch"]
    assert _kinds(check_markdown("%s passed and %s passed.\n" % (EXE, EXE_B), st)) == ["status_mismatch"]


def test_a_status_word_before_any_id_is_judged_against_the_first():
    st = _two_runs()
    assert check_markdown("Passed: %s, and %s failed.\n" % (EXE, EXE_B), st) == []
    assert _kinds(check_markdown("Failed: %s, and %s failed.\n" % (EXE, EXE_B), st)) == ["status_mismatch"]
