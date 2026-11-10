from __future__ import annotations

import json

import pytest

import test_compare
from helpers import base
from qa import authority, findings, fixes, gate, ledger, state, trace, vocab
from qa.errors import QaError

BASE_C = "a" * 40
FIX_C = "b" * 40
OTHER_C = "c" * 40
DEFECT = "tests.test_discount::test_cap"
KEEP = "tests.test_discount::test_total"


def exe_id(letter):
    return "EXE-" + "0" * 25 + letter


def refused(code, call, *args, **kw):
    with pytest.raises(QaError) as caught:
        call(*args, **kw)
    assert caught.value.code == code
    return caught.value


def record_run(ws, actor, letter, commit, results, minute, *, status=None, dirty=False, attempt_of=None):
    """An execution on ``commit`` holding ``results`` as ``(test_id, status, changes)``; its status follows its results."""
    data = test_compare._execution(exe_id(letter))
    data["started_at"] = data["ended_at"] = "2026-11-10T10:%02d:00.000Z" % minute
    data["git"] = {"commit": commit, "branch": None, "dirty": dirty, "diff_hash": "sha256:" + "0" * 64}
    data["status"] = status or ("failed" if any(r[1] == "failed" for r in results) else "passed")
    data["attempt_of"] = attempt_of
    ledger.append(ws, actor, "execution.recorded", data)
    for number, (test_id, outcome, changes) in enumerate(results):
        result = test_compare._result(minute * 100 + number, exe_id(letter), test_id, outcome)
        result.update(changes)
        ledger.append(ws, actor, "result.recorded", result)
    return exe_id(letter)


def scene(ws, actor, *, f2p_base=None, f2p_head=None, p2p_base=None, p2p_head=None, base_run=None, head_run=None, p2p_head_run=None):
    """FND-001, FIX-001 for it, and four runs: A and B for FAIL_TO_PASS, C and D for PASS_TO_PASS (base then head)."""
    findings.add(ws, actor, base())
    fix = fixes.add(ws, actor, {"findings": ["FND-001"], "base_commit": BASE_C, "fix_commit": FIX_C, "description": "cap the discount"})
    record_run(ws, actor, "A", BASE_C, f2p_base or [(DEFECT, "failed", {})], 1, **(base_run or {}))
    record_run(ws, actor, "B", FIX_C, f2p_head or [(DEFECT, "passed", {})], 2, **(head_run or {}))
    record_run(ws, actor, "C", BASE_C, p2p_base or [(KEEP, "passed", {})], 3)
    record_run(ws, actor, "D", FIX_C, p2p_head or [(KEEP, "passed", {})], 4, **(p2p_head_run or {}))
    return fix


def revalidate(ws, actor, fix="FIX-001", **over):
    args = {"f2p": (exe_id("A"), exe_id("B")), "p2p": (exe_id("C"), exe_id("D")), "defect_tests": [DEFECT]}
    args.update(over)
    return fixes.revalidate(ws, actor, fix, **args)


def finding_state(ws, fid="FND-001"):
    return state.load(ws)["findings"][fid]["state"]


def event_count(ws):
    return sum(1 for _ in ledger.iter_all_events(ws))


# --------------------------------------------------------------------------- recording a fix

def test_fix_moves_findings_to_fix_submitted(ws, actor):
    findings.add(ws, actor, base())
    findings.add(ws, actor, base(refs={"cwe": ["CWE-1"], "rule": "r2", "location": "app/other.py"}))
    done = fixes.add(ws, actor, {"findings": ["FND-001", "FND-002"], "base_commit": BASE_C, "fix_commit": FIX_C,
                                 "description": "check the owner", "ref": "PR-12"})
    assert done["id"] == "FIX-001" and done["findings"] == ["FND-001", "FND-002"]
    st = state.load(ws)
    assert st["fixes"]["FIX-001"]["ref"] == "PR-12" and st["fixes"]["FIX-001"]["fix_commit"] == FIX_C
    assert [st["findings"][f]["state"] for f in ("FND-001", "FND-002")] == ["fix_submitted", "fix_submitted"]
    addressed = [(l["source"], l["target"]) for l in st["links"] if l["relation"] == "addressed_by"]
    assert addressed == [("FND-001", "FIX-001"), ("FND-002", "FIX-001")]
    assert st["revalidations"] == {} and not st["violations"]


def test_a_reopened_finding_can_be_fixed_again_and_ids_are_sequential(ws, actor):
    scene(ws, actor, f2p_base=[(DEFECT, "passed", {})])
    assert revalidate(ws, actor)["outcome"] == "failed" and finding_state(ws) == "reopened"
    second = fixes.add(ws, actor, {"findings": ["FND-001"], "base_commit": BASE_C, "fix_commit": OTHER_C, "description": "again"})
    assert second["id"] == "FIX-002" and finding_state(ws) == "fix_submitted"


@pytest.mark.parametrize("change,code", [
    ({"findings": []}, "fix_invalid"), ({"findings": ["FND-001", "FND-001"]}, "fix_invalid"),
    ({"findings": "FND-001"}, "fix_invalid"), ({"base_commit": "abc123"}, "fix_invalid"), ({"fix_commit": "B" * 40}, "fix_invalid"),
    ({"description": "  "}, "fix_invalid"), ({"id": "FIX-009"}, "fix_invalid"), ({"surprise": 1}, "fix_invalid"),
    ({"findings": ["FND-404"]}, "unknown_finding"),
])
def test_a_malformed_fix_is_refused_and_records_nothing(ws, actor, change, code):
    findings.add(ws, actor, base())
    data = {"findings": ["FND-001"], "base_commit": BASE_C, "fix_commit": FIX_C, "description": "d"}
    data.update(change)
    before = event_count(ws)
    refused(code, fixes.add, ws, actor, data)
    assert event_count(ws) == before and finding_state(ws) == "open"


def test_only_open_or_reopened_findings_can_be_fixed(ws, actor):
    findings.add(ws, actor, base())
    findings.change_state(ws, actor, "FND-001", "wont_fix", reason="no")
    before = event_count(ws)
    refused("invalid_transition", fixes.add, ws, actor, {"findings": ["FND-001"], "base_commit": BASE_C, "fix_commit": FIX_C, "description": "d"})
    assert event_count(ws) == before


# --------------------------------------------------------------------------- revalidation

def test_fail_to_pass_and_pass_to_pass_verify_the_fix(ws, actor):
    scene(ws, actor)
    done = revalidate(ws, actor)
    assert (done["id"], done["outcome"], done["fix"]) == ("RVL-001", "passed", "FIX-001")
    assert done["findings"] == {"FND-001": "verified_fixed"}
    st = state.load(ws)
    assert st["findings"]["FND-001"]["state"] == "verified_fixed" and not st["violations"]
    assert st["revalidations"]["RVL-001"]["fix_commit"] == FIX_C and st["revalidations"]["RVL-001"]["outcome"] == "passed"
    assert ("FIX-001", "validated_by", "RVL-001") in {(l["source"], l["relation"], l["target"]) for l in st["links"]}


def test_defect_test_passing_on_base_fails_revalidation(ws, actor):
    scene(ws, actor, f2p_base=[(DEFECT, "passed", {})])
    done = revalidate(ws, actor)
    assert done["outcome"] == "failed" and DEFECT in done["reason"] and finding_state(ws) == "reopened"


def test_defect_test_still_failing_on_head_fails_revalidation(ws, actor):
    scene(ws, actor, f2p_head=[(DEFECT, "failed", {})])
    assert revalidate(ws, actor)["outcome"] == "failed" and finding_state(ws) == "reopened"


def test_new_failure_in_pass_to_pass_fails_revalidation(ws, actor):
    scene(ws, actor, p2p_head=[(KEEP, "failed", {})])
    done = revalidate(ws, actor)
    assert done["outcome"] == "failed" and KEEP in done["reason"] and finding_state(ws) == "reopened"


def test_a_test_gone_from_pass_to_pass_fails_revalidation(ws, actor):
    scene(ws, actor, p2p_head=[("tests.test_discount::test_other", "passed", {})])
    assert revalidate(ws, actor)["outcome"] == "failed"


def test_still_failing_regression_test_fails_revalidation(ws, actor):
    scene(ws, actor, p2p_base=[(KEEP, "passed", {}), ("tests.t::broken", "failed", {})],
          p2p_head=[(KEEP, "passed", {}), ("tests.t::broken", "failed", {})])
    done = revalidate(ws, actor)
    assert done["outcome"] == "failed" and "tests.t::broken" in done["reason"] and finding_state(ws) == "reopened"


def test_a_defect_test_in_the_regression_runs_may_fail_on_both_only_if_it_is_a_defect_test(ws, actor):
    # the defect test itself failing at base in the P2P pair and passing at head is `fixed`, which P2P allows
    scene(ws, actor, p2p_base=[(KEEP, "passed", {}), (DEFECT, "failed", {})], p2p_head=[(KEEP, "passed", {}), (DEFECT, "passed", {})])
    assert revalidate(ws, actor)["outcome"] == "passed"


def test_error_on_base_is_not_a_fail_to_pass(ws, actor):
    scene(ws, actor, f2p_base=[(DEFECT, "failed", {"failure_kind": "error"})])
    done = revalidate(ws, actor)
    assert done["outcome"] == "failed" and "assertion" in done["reason"] and finding_state(ws) == "reopened"


@pytest.mark.parametrize("key,index", [("f2p", 0), ("f2p", 1), ("p2p", 0), ("p2p", 1)])
def test_revalidation_refuses_executions_on_other_commits(ws, actor, key, index):
    scene(ws, actor)
    wrong = record_run(ws, actor, "Z", OTHER_C, [(DEFECT, "passed", {})], 9)
    pairs = {"f2p": [exe_id("A"), exe_id("B")], "p2p": [exe_id("C"), exe_id("D")]}
    pairs[key][index] = wrong
    before = event_count(ws)
    caught = refused("revalidation_refused", fixes.revalidate, ws, actor, "FIX-001", f2p=tuple(pairs["f2p"]), p2p=tuple(pairs["p2p"]),
                     defect_tests=[DEFECT])
    assert caught.details["why"] == "commit_mismatch" and event_count(ws) == before and finding_state(ws) == "fix_submitted"


def test_an_attempt_chain_that_crosses_commits_is_refused(ws, actor):
    scene(ws, actor)
    record_run(ws, actor, "E", FIX_C, [(DEFECT, "passed", {})], 8, attempt_of=exe_id("A"))  # a retry of a base run, claimed at the fix
    before = event_count(ws)
    caught = refused("revalidation_refused", revalidate, ws, actor, f2p=(exe_id("A"), exe_id("E")))
    assert caught.details["why"] == "mixed_attempt_chain" and event_count(ws) == before


def test_an_unknown_execution_or_fix_is_refused(ws, actor):
    scene(ws, actor)
    refused("unknown_execution", revalidate, ws, actor, f2p=(exe_id("A"), exe_id("Y")))
    refused("unknown_fix", revalidate, ws, actor, "FIX-404")
    refused("revalidation_invalid", revalidate, ws, actor, defect_tests=[])
    assert state.load(ws)["revalidations"] == {}


def test_inconclusive_run_leaves_the_finding_fix_submitted(ws, actor):
    scene(ws, actor, head_run={"status": "blocked"})
    done = revalidate(ws, actor)
    assert done["outcome"] == "inconclusive" and "blocked" in done["reason"]
    assert finding_state(ws) == "fix_submitted" and done["findings"] == {"FND-001": "fix_submitted"}
    assert not [v for v in state.load(ws)["violations"]]


def test_inconclusive_pass_to_pass_run_leaves_the_finding_fix_submitted(ws, actor):
    scene(ws, actor, p2p_head_run={"status": "inconclusive"})
    assert revalidate(ws, actor)["outcome"] == "inconclusive" and finding_state(ws) == "fix_submitted"


@pytest.mark.parametrize("slot", ["base_run", "head_run"])
def test_a_dirty_tree_cannot_stand_for_a_commit(ws, actor, slot):
    scene(ws, actor, **{slot: {"dirty": True}})
    done = revalidate(ws, actor)
    assert done["outcome"] == "inconclusive" and "dirty" in done["reason"] and exe_id("A" if slot == "base_run" else "B") in done["reason"]
    assert finding_state(ws) == "fix_submitted"


def test_a_dirty_pass_to_pass_run_is_inconclusive_too(ws, actor):
    scene(ws, actor, p2p_head_run={"dirty": True})
    done = revalidate(ws, actor)
    assert done["outcome"] == "inconclusive" and "dirty" in done["reason"]


def test_a_later_passing_revalidation_verifies_after_an_inconclusive_one(ws, actor):
    scene(ws, actor, head_run={"status": "blocked"})
    assert revalidate(ws, actor)["outcome"] == "inconclusive"
    record_run(ws, actor, "F", FIX_C, [(DEFECT, "passed", {})], 7)
    assert revalidate(ws, actor, f2p=(exe_id("A"), exe_id("F")))["id"] == "RVL-002" and finding_state(ws) == "verified_fixed"


# --------------------------------------------------------------------------- trust: the fold re-derives the outcome

def forged_rvl(ws, actor, outcome, **over):
    data = {"id": "RVL-099", "fix": "FIX-001", "f2p": {"base": exe_id("A"), "head": exe_id("B")},
            "p2p": {"base": exe_id("C"), "head": exe_id("D")}, "defect_tests": [DEFECT], "outcome": outcome, "reason": None}
    data.update(over)
    ledger.append(ws, actor, "revalidation.recorded", data)


def test_a_hand_appended_passed_outcome_does_not_verify_a_finding(ws, actor):
    scene(ws, actor, p2p_head=[(KEEP, "failed", {})])  # the evidence says the regression set broke
    forged_rvl(ws, actor, "passed")
    st = state.load(ws)
    assert [v["kind"] for v in st["violations"]] == ["outcome_mismatch"]
    assert st["findings"]["FND-001"]["state"] == "reopened" and st["revalidations"]["RVL-099"]["outcome"] == "failed"


def test_a_hand_appended_passed_outcome_over_inconclusive_evidence_verifies_nothing(ws, actor):
    scene(ws, actor, head_run={"status": "blocked"})
    forged_rvl(ws, actor, "passed")
    st = state.load(ws)
    assert st["findings"]["FND-001"]["state"] == "fix_submitted" and [v["kind"] for v in st["violations"]] == ["outcome_mismatch"]


def test_a_hand_appended_revalidation_over_missing_or_wrong_commit_runs_is_discarded(ws, actor):
    scene(ws, actor)
    forged_rvl(ws, actor, "passed", id="RVL-090", f2p={"base": exe_id("A"), "head": exe_id("Q")})
    wrong = record_run(ws, actor, "Z", OTHER_C, [(DEFECT, "passed", {})], 9)
    forged_rvl(ws, actor, "passed", id="RVL-091", f2p={"base": exe_id("A"), "head": wrong})
    forged_rvl(ws, actor, "passed", id="RVL-092", fix="FIX-404")
    st = state.load(ws)
    assert sorted(v["kind"] for v in st["violations"]) == ["commit_mismatch", "unknown_execution", "unknown_fix"]
    assert st["revalidations"] == {} and st["findings"]["FND-001"]["state"] == "fix_submitted"


def test_an_honest_revalidation_replayed_from_the_ledger_agrees_with_itself(ws, actor):
    scene(ws, actor)
    revalidate(ws, actor)
    again = state.fold(ledger.iter_all_events(ws))  # no key needed: nothing here is signed
    assert again["findings"]["FND-001"]["state"] == "verified_fixed" and not again["violations"]
    assert authority.EVENTS.isdisjoint({"fix.recorded", "revalidation.recorded"})


def test_a_hand_appended_state_change_to_a_lifecycle_state_is_a_violation(ws, actor):
    findings.add(ws, actor, base())
    ledger.append(ws, actor, "finding.state_changed", {"id": "FND-001", "from": "open", "to": "verified_fixed", "reason": "x",
                                                      "accepted_by": None, "expires_at": None, "duplicate_of": None})
    st = state.load(ws)
    assert st["findings"]["FND-001"]["state"] == "open" and [v["kind"] for v in st["violations"]] == ["invalid_transition"]


def test_a_revalidation_for_an_older_fix_does_not_move_a_finding_fixed_again(ws, actor):
    scene(ws, actor, f2p_base=[(DEFECT, "passed", {})])
    revalidate(ws, actor)  # FIX-001 fails, FND-001 reopens
    fixes.add(ws, actor, {"findings": ["FND-001"], "base_commit": BASE_C, "fix_commit": OTHER_C, "description": "again"})
    forged_rvl(ws, actor, "failed", id="RVL-050")  # a late, honest-looking record about FIX-001
    assert finding_state(ws) == "fix_submitted"


# --------------------------------------------------------------------------- the hash, the lifecycle, the gate, the matrix

def test_the_new_keys_are_absent_from_the_hash_while_empty_and_count_once_filled(ws, actor):
    empty = state.empty_state()
    without = {k: v for k, v in empty.items() if k not in ("fixes", "revalidations")}
    assert state.state_hash(empty) == state.state_hash(without)
    scene(ws, actor)
    assert state.state_hash(state.load(ws)) != state.state_hash({k: v for k, v in state.load(ws).items() if k != "fixes"})


def test_lifecycle_states_cannot_be_set_by_hand(ws, qa_cli):
    for name in vocab.KERNEL_STATES:
        code, out = qa_cli("finding", "state", "FND-001", name, "--reason", "r")
        assert code == 2 and out["error"]["code"] == "set_by_fix_and_revalidation"


@pytest.mark.parametrize("name", vocab.KERNEL_STATES)
def test_lifecycle_states_are_refused_before_any_other_check(ws, actor, name):
    refused("set_by_fix_and_revalidation", findings.change_state, ws, actor, "FND-404", name, reason="")


def test_gate_counts_fix_submitted_as_unresolved(ws, actor):
    from test_gate import _high
    findings.add(ws, actor, base())
    fixes.add(ws, actor, {"findings": ["FND-001"], "base_commit": BASE_C, "fix_commit": FIX_C, "description": "d"})
    st = state.load(ws)
    assert st["findings"]["FND-001"]["state"] == "fix_submitted"
    result = _high(st)
    assert result["outcome"] == "NOT_MET" and "FND-001" in result["detail"] and "fix_submitted" in result["detail"]


def test_the_matrix_reports_a_fix_without_a_revalidation_and_an_off_target_one(ws, actor):
    scene(ws, actor)
    assert {"kind": "fix_without_rvl", "id": "FIX-001"} in trace.matrix(state.load(ws), FIX_C)["gaps"]
    revalidate(ws, actor)
    assert not [g for g in trace.matrix(state.load(ws), FIX_C)["gaps"] if g["kind"] in ("fix_without_rvl", "rvl_off_target")]
    assert {"kind": "rvl_off_target", "id": "RVL-001"} in trace.matrix(state.load(ws), OTHER_C)["gaps"]
    assert not [g for g in trace.matrix(state.load(ws), FIX_C)["gaps"] if g["kind"] == "fnd_without_fix"]


def test_trace_link_still_refuses_the_fix_relations(ws, actor):
    findings.add(ws, actor, base())
    refused("invalid_link", trace.link, ws, actor, "FND-001", "addressed_by", "FIX-001")
    refused("invalid_link", trace.link, ws, actor, "FIX-001", "validated_by", "RVL-001")


# --------------------------------------------------------------------------- the commands

def test_the_fix_and_revalidate_commands(ws, qa_cli, actor, tmp_path):
    findings.add(ws, actor, base())
    record_run(ws, actor, "A", BASE_C, [(DEFECT, "failed", {})], 1)
    record_run(ws, actor, "B", FIX_C, [(DEFECT, "passed", {})], 2)
    record_run(ws, actor, "C", BASE_C, [(KEEP, "passed", {})], 3)
    record_run(ws, actor, "D", FIX_C, [(KEEP, "passed", {})], 4)
    path = tmp_path / "fix.json"
    path.write_text(json.dumps({"findings": ["FND-001"], "base_commit": BASE_C, "fix_commit": FIX_C, "description": "cap it"}))
    code, out = qa_cli("fix", "add", "--json", str(path))
    assert code == 0 and out["id"] == "FIX-001" and out["findings"] == ["FND-001"]
    code, out = qa_cli("revalidate", "FIX-001", "--f2p", "%s:%s" % (exe_id("A"), exe_id("B")), "--p2p", "%s:%s" % (exe_id("C"), exe_id("D")),
                       "--defect-test", DEFECT)
    assert code == 0 and out["outcome"] == "passed" and out["findings"] == {"FND-001": "verified_fixed"}
    code, out = qa_cli("revalidate", "FIX-001", "--f2p", "nocolon", "--p2p", "%s:%s" % (exe_id("C"), exe_id("D")), "--defect-test", DEFECT)
    assert code != 0 and out["error"]["code"] == "usage"
