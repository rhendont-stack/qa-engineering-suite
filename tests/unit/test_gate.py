from __future__ import annotations

import contextlib
import dataclasses
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

import test_compare
import test_records
import test_trace
from helpers import WRITE_JUNIT, WRITE_SARIF, base, req, without_mac, write_event_at
from qa import KERNEL_VERSION, config, execution, findings, gate, gitinfo, ids, integrity, ledger, records, resource_dir, schema, state, trace, vocab
from qa.errors import QaError
from qa.ledger import Problem
from qa.workspace import Workspace

POLICY = "gate-policy.schema.json"
EVALUATED = "events/gate.evaluated.schema.json"
EVT = "EVT-" + "0" * 26
COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
TREE = "sha256:" + "1" * 64
OTHER_TREE = "sha256:" + "2" * 64
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
T0 = datetime(2035, 1, 1, tzinfo=timezone.utc)  # after every event a fixture records on the real clock


# --------------------------------------------------------------------------- the brief's tests

def c(outcome, blocking=True): return {"id": "x", "type": "t", "blocking": blocking, "outcome": outcome}
@pytest.mark.parametrize("criteria,risks,want", [
    ([c("MET"), c("NO_EVIDENCE", False)], [], "READY"),
    ([c("MET"), c("MET", False)], ["FND-002"], "READY_WITH_CONDITIONS"),
    ([c("MET"), c("NOT_MET", False)], [], "READY_WITH_CONDITIONS"),
    ([c("NO_EVIDENCE"), c("NOT_MET", False)], [], "INSUFFICIENT_EVIDENCE"),
    ([c("NOT_MET"), c("NO_EVIDENCE")], [], "NOT_READY"),
    ([c("NOT_APPLICABLE"), c("MET")], [], "READY"),
])
def test_verdict_truth_table(criteria, risks, want):
    assert gate.verdict(criteria, risks) == want
@pytest.fixture
def repo_ws(trusted):
    if shutil.which("git") is None: pytest.skip("tool missing: git")   # the Linux images ship no git
    subprocess.run(["git", "init", "-q", "-b", "main", str(trusted.root)], check=True)
    (trusted.root / "a.txt").write_text("1")
    for a in (["add", "a.txt"], ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "c"]):
        subprocess.run(["git", "-C", str(trusted.root), *a], check=True)
    return trusted
def test_change_profile_ready_after_passing_suite(repo_ws, actor, py):
    execution.execute(repo_ws, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))
    assert gate.evaluate(repo_ws, actor, profile="change")["data"]["verdict"] == "READY"
def test_failing_suite_is_not_ready(repo_ws, actor, py):
    execution.execute(repo_ws, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "fail"))
    assert gate.evaluate(repo_ws, actor, profile="change")["data"]["verdict"] == "NOT_READY"
def test_missing_tool_gives_insufficient_evidence(repo_ws, actor):
    exe = execution.execute(repo_ws, actor, execution.RunRequest(command=["qa-no-such-tool"]))["execution"]
    evaluator = dataclasses.replace(actor, role="evaluator", independence="I2")   # release needs I2 (independence_min)
    assert gate.evaluate(repo_ws, evaluator, profile="release")["data"]["verdict"] == "INSUFFICIENT_EVIDENCE"
    g = gate.evaluate(repo_ws, actor, profile="change")["data"]   # under change, suite_executed is the blocking criterion that lacks evidence
    se = next(x for x in g["criteria"] if x["type"] == "suite_executed")
    assert (g["verdict"], se["outcome"]) == ("INSUFFICIENT_EVIDENCE", "NO_EVIDENCE") and exe in se["detail"]
def test_open_high_finding_blocks_with_detail(repo_ws, actor, py):
    execution.execute(repo_ws, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass")); findings.add(repo_ws, actor, base())
    g = gate.evaluate(repo_ws, actor, profile="change")["data"]
    crit = next(x for x in g["criteria"] if x["id"] == "no_unresolved_high")
    assert g["verdict"] == "NOT_READY" and (crit["outcome"], crit["detail"]) == ("NOT_MET", "FND-001 (HIGH, open)")
def test_accepted_risk_conditions_then_expiry(repo_ws, actor, py, frozen_now):
    execution.execute(repo_ws, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass")); findings.add(repo_ws, actor, base())
    exp = ids.format_ts(ids.now_utc() + timedelta(days=1))
    findings.change_state(repo_ws, actor, "FND-001", "accepted_risk", reason="r", accepted_by="user", expires_at=exp)
    assert gate.evaluate(repo_ws, actor, profile="change")["data"]["verdict"] == "READY_WITH_CONDITIONS"
    frozen_now.advance(2 * 86400)
    assert gate.evaluate(repo_ws, actor, profile="change")["data"]["verdict"] == "NOT_READY"
def test_unevaluable_criteria_say_why(repo_ws, actor):
    g = gate.evaluate(repo_ws, actor, profile="release")["data"]
    sc = next(x for x in g["criteria"] if x["type"] == "security_scans_executed")
    assert sc["outcome"] == "NO_EVIDENCE" and "milestone 4" in sc["detail"]
def test_opt_in_criterion_off_is_not_applicable(trusted, actor):
    g = gate.evaluate(trusted, actor, profile="release")["data"]
    m = next(x for x in g["criteria"] if x["type"] == "mutation_score_min")
    assert (m["outcome"], m["blocking"], m["justification"]) == ("NOT_APPLICABLE", False, "Project has not opted in (config.gate.overrides)")
def test_override_via_propose_accept_is_not_drift(repo_ws, actor):
    p = gate.propose(repo_ws, actor, "suite_executed", {"not_applicable": "documentation-only change"}, "docs PR")
    gate.accept(repo_ws, actor, p["id"], "yes, docs only")
    g = gate.evaluate(repo_ws, actor, profile="change")["data"]
    assert next(x for x in g["criteria"] if x["type"] == "suite_executed")["outcome"] == "NOT_APPLICABLE"
    assert config.check_drift(repo_ws, state.load(repo_ws), actor, "test-execution-local") is None
def test_evaluation_is_deterministic(repo_ws, actor, py):
    execution.execute(repo_ws, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))
    a, b = (gate.evaluate(repo_ws, actor, profile="change")["data"] for _ in range(2))
    assert (a["policy_hash"], a["state_hash"], a["verdict"], a["criteria"]) == (b["policy_hash"], b["state_hash"], b["verdict"], b["criteria"])
def test_deleting_a_session_after_evaluation_is_detected(repo_ws, actor, py):
    execution.execute(repo_ws, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass")); gate.evaluate(repo_ws, actor, profile="change")
    st = state.load(repo_ws); repo_ws.path("ledger", actor.session + ".jsonl").unlink()
    assert "missing" in [p.kind for p in integrity.problems(repo_ws, st)]


# --------------------------------------------------------------------------- helpers

def _git(ws, *args):
    subprocess.run(["git", "-C", str(ws.root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], check=True)


def _head(ws):
    return subprocess.run(["git", "-C", str(ws.root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


def _run(ws, actor, py, outcome="pass", **kw):
    return execution.execute(ws, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", outcome, **kw))["execution"]


def _criterion(event, criterion_id):
    return next(x for x in event["data"]["criteria"] if x["id"] == criterion_id)


def _accept(ws, actor, criterion_id, override):
    return gate.accept(ws, actor, gate.propose(ws, actor, criterion_id, override, "because")["id"], "yes")


def _ledger(ws):
    return {p.name: p.read_bytes() for p in ws.path("ledger").iterdir()}


def _refused(code, ws, call, *args, **kw):
    """``call`` raises ``code`` and leaves the ledger and config.json as they were (T16-R12)."""
    before = _ledger(ws), ws.path("config.json").read_bytes()
    with pytest.raises(QaError) as caught:
        call(*args, **kw)
    assert caught.value.code == code
    assert (_ledger(ws), ws.path("config.json").read_bytes()) == before


def _config(**overrides):
    return {"gate": {"profile": "change", "overrides": {k: dict(v, accepted_in=EVT) for k, v in overrides.items()}}}


def _policy(profile, **overrides):
    return gate.load_policy(profile, _config(**overrides))[0]


def _from(policy, criterion_id):
    return next(x for x in policy["criteria"] if x["id"] == criterion_id)


def _crit(profile, criterion_id, **overrides):
    return _from(_policy(profile, **overrides), criterion_id)


def _context(**changes):
    values = dict(target_commit=COMMIT, independence="I1", integrity_problems=[],
                  kernel={"status": "ok", "source_checkout": False}, now=NOW)
    values.update(changes)
    return gate.GateContext(**values)


GIT = {"commit": COMMIT, "branch": "main", "dirty": False, "diff_hash": TREE}


def _exe(execution_id="EXE-1", **changes):
    record = {"id": execution_id, "status": "passed", "reason": None, "strength": "structured", "activity": "test-execution-local",
              "scope": "targeted", "attempt_of": None, "git": GIT, "report": {"kind": "tests"}, "environment_id": "local"}
    record.update(changes)
    return record


def _finding(severity="HIGH", fstate="open", verification="suspected", category="security", **changes):
    return dict(severity=severity, state=fstate, verification_status=verification, category=category, **changes)


def _st(*executions, found=None, tests=None, results=None):
    st = state.empty_state()
    st["executions"] = {record["id"]: record for record in executions}
    st["findings"] = dict(found or {})
    st["tests"] = dict(tests or {})
    st["results"] = dict(results or {})
    return st


def _suite(st, profile="change", context=None, **overrides):
    return gate.evaluate_criterion(_crit(profile, "suite_executed", **overrides), st, context or _context())


# --------------------------------------------------------------------------- T16-R1: the policy files

B, A = True, False
TABLE = [  # the brief's table: blocking per profile, in vocab.PROFILES order; None is absent
    ("no_unresolved_high", (B, B, A, B)),
    ("requirements_verified", (A, A, A, B)),
    ("suite_executed", (B, B, A, B)),
    ("no_blocked_critical", (A, A, A, B)),
    ("flaky_rate_max", (A, A, A, B)),
    ("security_scans_executed", (A, None, A, B)),
    ("mutation_score_min", (None, None, A, B)),
    ("performance_objectives_met", (None, None, A, B)),
    ("migrations_validated", (None, None, A, B)),
    ("rollback_validated", (None, None, None, B)),
    ("test_integrity", (B, B, B, B)),
    ("independence_min", (A, A, A, B)),
]
TYPE_OF = {"no_unresolved_high": "no_unresolved_findings"}
SUITE = {"change": ("targeted", False, "exit_code"), "fix": ("targeted", False, "exit_code"),
         "audit": ("full", False, "structured"), "release": ("full", True, "structured")}
INDEPENDENCE = {"change": "I1", "fix": "I1", "audit": "I2", "release": "I2"}
PLAIN_PARAMS = {"no_unresolved_high": {"min_severity": "HIGH"}, "flaky_rate_max": {"max_ratio": 0.02},
                "mutation_score_min": {"min_score": 70}}


def _file(profile):
    return json.loads((resource_dir("policies") / ("gate.%s.json" % profile)).read_text(encoding="utf-8"))


def _expected(criterion_id, profile, blocking):
    scope, clean_tree, strength = SUITE[profile]
    params = PLAIN_PARAMS.get(criterion_id, {})
    if criterion_id == "suite_executed":
        params = {"scope": scope, "clean_tree": clean_tree, "require_passed": True}
    elif criterion_id == "independence_min":
        params = {"min": INDEPENDENCE[profile]}
    return {"id": criterion_id, "type": TYPE_OF.get(criterion_id, criterion_id), "blocking": blocking,
            "min_strength": strength if criterion_id == "suite_executed" else None, "min_independence": None,
            "params": params, "opt_in": criterion_id == "mutation_score_min"}


@pytest.mark.parametrize("profile", vocab.PROFILES)
def test_policy_file_is_the_brief_table_with_its_exact_values(profile):
    column = vocab.PROFILES.index(profile)
    assert _file(profile) == {"profile": profile, "criteria": [_expected(cid, profile, flags[column])
                                                               for cid, flags in TABLE if flags[column] is not None]}


@pytest.mark.parametrize("profile", vocab.PROFILES)
def test_policy_file_fits_its_schema(profile):
    schema.validate(_file(profile), POLICY)


@pytest.mark.parametrize("break_it", [
    lambda d: d.update(version=1),
    lambda d: d["criteria"][0].update(note="x"),
    lambda d: d["criteria"][0]["params"].update(typo=1),
    lambda d: d["criteria"][0].update(type="made_up"),
    lambda d: d.update(profile="yolo"),
    lambda d: d.update(criteria=[]),
    lambda d: d["criteria"][0].pop("opt_in"),
    lambda d: d["criteria"][0].update(min_strength="great"),
    lambda d: d["criteria"][0].update(min_independence="I9"),
])
def test_policy_schema_is_closed_and_needs_a_criterion(break_it):
    doc = _file("change")
    break_it(doc)
    with pytest.raises(QaError) as caught:
        schema.validate(doc, POLICY)
    assert caught.value.code == "schema_invalid"


def test_policy_schema_enums_match_vocabulary_and_the_twelve_types():
    doc = schema.load_schema(POLICY)
    assert doc["properties"]["profile"]["enum"] == list(vocab.PROFILES)
    assert doc["$defs"]["criterion_type"]["enum"] == [TYPE_OF.get(cid, cid) for cid, _flags in TABLE]
    assert schema.load_schema(EVALUATED)["properties"]["profile"]["enum"] == list(vocab.PROFILES)


# --------------------------------------------------------------------------- T16-R2: load_policy

@pytest.mark.parametrize("profile", ["yolo", "", "../gate", None, ["change"]])
def test_unknown_profile_is_refused(profile):
    with pytest.raises(QaError) as caught:
        gate.load_policy(profile, _config())
    assert caught.value.code == "unknown_profile"


def test_no_overrides_gives_the_file_and_a_hash_of_it():
    policy, digest = gate.load_policy("change", _config())
    assert policy == _file("change") and digest.startswith("sha256:") and digest == gate.load_policy("change", _config())[1]


def test_override_for_a_criterion_the_profile_lacks_is_ignored():
    assert gate.load_policy("change", _config(rollback_validated={"blocking": True})) == gate.load_policy("change", _config())


def test_params_merge_over_the_file():
    assert _crit("change", "suite_executed", suite_executed={"params": {"scope": "full"}})["params"] == {
        "scope": "full", "clean_tree": False, "require_passed": True}


def test_blocking_replaces():
    assert _crit("change", "flaky_rate_max", flaky_rate_max={"blocking": True})["blocking"] is True
    assert _crit("change", "suite_executed", suite_executed={"blocking": False})["blocking"] is False


def test_an_override_names_its_acceptance():
    assert _crit("change", "suite_executed", suite_executed={"blocking": False})["accepted_in"] == EVT
    assert "accepted_in" not in _crit("change", "suite_executed")


@pytest.mark.parametrize("override", [{"blocking": False}, {"enabled": True}, {"params": {"scope": "full"}}, {"not_applicable": "x"}])
def test_hash_covers_the_effective_policy(override):
    assert gate.load_policy("change", _config())[1] != gate.load_policy("change", _config(suite_executed=override))[1]


def test_two_overrides_with_different_effects_hash_differently():
    one, two = (_config(flaky_rate_max={"params": {"max_ratio": r}}) for r in (0.1, 0.2))
    assert gate.load_policy("change", one)[1] != gate.load_policy("change", two)[1]


@pytest.mark.parametrize("criterion_id", [cid for cid, flags in TABLE if flags[3]])
def test_enabled_false_is_a_justified_non_blocking_not_applicable_and_stays_listed(criterion_id):
    policy = _policy("release", **{criterion_id: {"enabled": False}})
    assert [x["id"] for x in policy["criteria"]] == [x["id"] for x in _file("release")["criteria"]]
    r = gate.evaluate_criterion(_from(policy, criterion_id), _st(), _context())
    justification = "Disabled by config.gate.overrides (%s)" % EVT
    assert (r["outcome"], r["blocking"], r["justification"], r["detail"]) == ("NOT_APPLICABLE", False, justification, justification)


def test_enabled_false_wins_over_a_not_applicable_text_and_blocking():
    r = gate.evaluate_criterion(_crit("change", "suite_executed", suite_executed={"enabled": False, "not_applicable": "x", "blocking": True}),
                                _st(), _context())
    assert (r["outcome"], r["blocking"], r["justification"]) == ("NOT_APPLICABLE", False, "Disabled by config.gate.overrides (%s)" % EVT)


def test_enabled_true_turns_an_opt_in_criterion_on():
    on = _crit("release", "mutation_score_min", mutation_score_min={"enabled": True})
    r = gate.evaluate_criterion(on, _st(), _context())
    assert (r["outcome"], r["blocking"]) == ("NO_EVIDENCE", True) and "justification" not in r


def test_an_opt_in_criterion_stays_off_without_enabled():
    for override in ({}, {"blocking": True}, {"params": {"min_score": 80}}):
        off = _crit("release", "mutation_score_min", mutation_score_min=override)
        r = gate.evaluate_criterion(off, _st(), _context())
        assert (r["outcome"], r["blocking"], r["justification"]) == ("NOT_APPLICABLE", False, "Project has not opted in (config.gate.overrides)")


def test_not_applicable_keeps_its_text_and_its_blocking():
    r = gate.evaluate_criterion(_crit("change", "suite_executed", suite_executed={"not_applicable": "no database"}), _st(), _context())
    assert (r["outcome"], r["blocking"], r["justification"], r["detail"], r["evidence"]) == ("NOT_APPLICABLE", True, "no database", "no database", [])


@pytest.mark.parametrize("criterion_id,override", [
    ("suite_executed", {"not_applicable": "   "}),
    ("flaky_rate_max", {"params": {"max_ratio": "a lot"}}),
    ("suite_executed", {"params": {"scope": "everything"}}),
    ("no_unresolved_high", {"params": {"min_severity": "SEVERE"}}),
    ("independence_min", {"params": {"min": "I9"}}),
])
def test_an_accepted_override_nothing_can_read_is_refused_not_applied(criterion_id, override):
    # config.accept approves a file as it stands, so what propose refuses can still arrive here.
    with pytest.raises(QaError) as caught:
        gate.load_policy("change", _config(**{criterion_id: override}))
    assert caught.value.code == "config_invalid"


def test_a_param_no_criterion_reads_is_merged_and_harmless():
    assert _crit("change", "test_integrity", test_integrity={"params": {"ignored": 1}})["params"] == {"ignored": 1}


# --------------------------------------------------------------------------- the criterion result

def test_a_result_has_exactly_the_brief_keys():
    r = _suite(_st(_exe()))
    assert set(r) == {"id", "type", "blocking", "outcome", "detail", "evidence"}


@pytest.mark.parametrize("profile", vocab.PROFILES)
def test_every_detail_is_a_string_and_only_not_applicable_is_justified(trusted, actor, profile):
    for x in gate.evaluate(trusted, actor, profile=profile)["data"]["criteria"]:
        assert isinstance(x["detail"], str) and x["detail"]
        assert ("justification" in x) == (x["outcome"] == "NOT_APPLICABLE")
        if x["outcome"] == "NOT_APPLICABLE":
            assert x["detail"] == x["justification"]


@pytest.mark.parametrize("ctype,capability,milestone", [
    ("mutation_score_min", "mutation ingestion", 3),
    ("security_scans_executed", "scan and performance skills", 4),
    ("performance_objectives_met", "scan and performance skills", 4),
    ("migrations_validated", "database skills", 4),
    ("rollback_validated", "database skills", 4),
])
def test_an_unevaluable_type_names_what_it_needs(ctype, capability, milestone):
    criterion = {"id": ctype, "type": ctype, "blocking": True, "min_strength": None, "min_independence": None, "params": {}, "opt_in": False}
    r = gate.evaluate_criterion(criterion, _st(), _context())
    assert (r["outcome"], r["detail"], r["evidence"]) == (
        "NO_EVIDENCE", "not evaluable by kernel %s (needs %s, milestone %d)" % (KERNEL_VERSION, capability, milestone), [])


# --------------------------------------------------------------------------- T16-R3, T16-R14: findings

def _high(st, profile="change", context=None, **overrides):
    return gate.evaluate_criterion(_crit(profile, "no_unresolved_high", **overrides), st, context or _context())


@pytest.mark.parametrize("fstate,unresolved", [("open", True), ("reopened", True), ("fix_submitted", True), ("verified_fixed", False),
                                               ("wont_fix", False), ("duplicate", False)])
def test_which_states_are_unresolved(fstate, unresolved):
    r = _high(_st(found={"FND-001": _finding(fstate=fstate)}))
    assert r["outcome"] == ("NOT_MET" if unresolved else "MET")


@pytest.mark.parametrize("verification", ["confirmed", "probable", "suspected", "not_reproducible"])
def test_every_verification_status_but_false_positive_counts(verification):
    assert _high(_st(found={"FND-001": _finding(verification=verification)}))["detail"] == "FND-001 (HIGH, open)"


def test_a_false_positive_does_not_count():
    assert _high(_st(found={"FND-001": _finding(verification="false_positive", justification="disproved by EXE-1")}))["outcome"] == "MET"


@pytest.mark.parametrize("extra", [{}, {"justification": "  "}, {"justification": None}], ids=["absent", "blank", "null"])
def test_a_false_positive_with_no_justification_still_counts(extra):  # I5, §10.6
    assert _high(_st(found={"FND-001": _finding(verification="false_positive", **extra)}))["outcome"] == "NOT_MET"


@pytest.mark.parametrize("severity,counts", [("CRITICAL", True), ("HIGH", True), ("MEDIUM", False), ("LOW", False), ("INFORMATIONAL", False)])
def test_severity_at_or_above_the_minimum_counts(severity, counts):
    assert _high(_st(found={"FND-001": _finding(severity=severity)}))["outcome"] == ("NOT_MET" if counts else "MET")


def test_an_overridden_minimum_severity_applies():
    st = _st(found={"FND-001": _finding(severity="MEDIUM")})
    assert _high(st, no_unresolved_high={"params": {"min_severity": "LOW"}})["outcome"] == "NOT_MET"


def test_details_are_sorted_by_id_and_joined_and_the_ids_are_the_evidence():
    st = _st(found={"FND-1000": _finding(severity="CRITICAL"), "FND-002": _finding(fstate="reopened"), "FND-999": _finding()})
    r = _high(st)
    assert r["detail"] == "FND-002 (HIGH, reopened); FND-999 (HIGH, open); FND-1000 (CRITICAL, open)"
    assert r["evidence"] == ["FND-002", "FND-999", "FND-1000"]


def test_an_expired_acceptance_reads_expired():
    expired = _finding(fstate="accepted_risk", expires_at="2026-01-01T00:00:00.000Z")
    r = _high(_st(found={"FND-002": expired}))
    assert (r["outcome"], r["detail"], r["evidence"]) == ("NOT_MET", "FND-002 (HIGH, accepted_risk expired)", ["FND-002"])


def test_expiry_at_the_exact_instant_is_expired_and_a_millisecond_before_is_not():
    acceptance = _finding(fstate="accepted_risk", expires_at=ids.format_ts(NOW))
    assert _high(_st(found={"FND-001": acceptance}), context=_context(now=NOW))["outcome"] == "NOT_MET"
    assert _high(_st(found={"FND-001": acceptance}), context=_context(now=NOW - timedelta(milliseconds=1)))["outcome"] == "MET"


@pytest.mark.parametrize("record", [_finding(fstate="accepted_risk"), _finding(fstate="accepted_risk", expires_at=None),
                                    _finding(fstate="accepted_risk", expires_at="2026-13-45T00:00:00.000Z")],
                         ids=["absent", "null", "impossible"])
def test_an_acceptance_with_no_usable_expiry_counts_as_expired(record):
    # A finding.opened record has no expires_at at all (T16-R14), and §10.6 says an acceptance needs one.
    assert _high(_st(found={"FND-001": record}))["detail"] == "FND-001 (HIGH, accepted_risk expired)"


def test_no_unresolved_findings_is_met_with_a_string_detail():
    r = _high(_st())
    assert r["outcome"] == "MET" and r["detail"] and r["evidence"] == []


def _accepted(ws, actor, fid, expires_at):
    findings.change_state(ws, actor, fid, "accepted_risk", reason="r", accepted_by="user", expires_at=expires_at)


def _low_finding(location):
    return base(severity_rationale={"anchor": "SEC-L1", "modifiers": []}, refs={"rule": "manual", "location": location})


def test_conditions_list_advisory_not_met_in_policy_order_then_risks_by_id(repo_ws, actor, py, frozen_now):
    _run(repo_ws, actor, py)
    _accept(repo_ws, actor, "no_unresolved_high", {"blocking": False})
    findings.add(repo_ws, actor, base())
    findings.add(repo_ws, actor, _low_finding("b.py"))
    findings.add(repo_ws, actor, _low_finding("c.py"))
    soon, later = (ids.format_ts(ids.now_utc() + timedelta(days=d)) for d in (1, 2))
    _accepted(repo_ws, actor, "FND-003", soon)
    _accepted(repo_ws, actor, "FND-002", later)
    weak = dataclasses.replace(actor, independence="I0")
    g = gate.evaluate(repo_ws, weak, profile="change")["data"]
    assert g["conditions"] == ["no_unresolved_high: FND-001 (HIGH, open)", "independence_min: I0 < I1",
                               "FND-002 accepted_risk until %s" % later, "FND-003 accepted_risk until %s" % soon]
    assert g["verdict"] == "READY_WITH_CONDITIONS"


def test_advisory_no_evidence_is_not_a_condition(repo_ws, actor, py):
    _run(repo_ws, actor, py)
    g = gate.evaluate(repo_ws, actor, profile="change")["data"]
    assert g["verdict"] == "READY" and g["conditions"] == []
    assert _criterion({"data": g}, "requirements_verified")["outcome"] == "NO_EVIDENCE"


def test_an_expired_or_false_positive_acceptance_is_no_accepted_risk(repo_ws, actor, py, frozen_now):
    _run(repo_ws, actor, py)
    findings.add(repo_ws, actor, _low_finding("b.py"))
    findings.add(repo_ws, actor, _low_finding("c.py"))
    findings.update(repo_ws, actor, "FND-002", {"verification_status": "false_positive", "impact": "i", "recommendation": "r", "justification": "disproved"})
    expiry = ids.format_ts(ids.now_utc() + timedelta(days=1))
    _accepted(repo_ws, actor, "FND-001", expiry)
    _accepted(repo_ws, actor, "FND-002", expiry)
    assert gate.evaluate(repo_ws, actor, profile="change")["data"]["conditions"] == ["FND-001 accepted_risk until %s" % expiry]
    frozen_now.advance(2 * 86400)
    g = gate.evaluate(repo_ws, actor, profile="change")["data"]
    assert (g["conditions"], g["verdict"]) == ([], "READY")  # a LOW finding is below the blocking minimum


# --------------------------------------------------------------------------- T16-R4: qualifying executions

DISQUALIFIED = {
    "another_activity": {"activity": "static-analysis"},
    "an_alert_report": {"report": {"kind": "alerts"}},
    "a_rerun": {"attempt_of": "EXE-0"},
    "no_git_facts": {"git": None},
    "another_commit": {"git": dict(GIT, commit=OTHER_COMMIT)},
}
WEAK = {"attested": {"strength": "attested"}, "no_strength_at_all": {"strength": None}}


@pytest.mark.parametrize("change", DISQUALIFIED.values(), ids=DISQUALIFIED)
@pytest.mark.parametrize("status", ["passed", "failed"])
def test_a_disqualified_execution_is_no_evidence(change, status):
    r = _suite(_st(_exe(status=status, **change)))
    assert (r["outcome"], r["evidence"]) == ("NO_EVIDENCE", []) and "no execution qualified" in r["detail"]


@pytest.mark.parametrize("change", DISQUALIFIED.values(), ids=DISQUALIFIED)
def test_a_disqualified_execution_cannot_be_the_latest_word(change):
    r = _suite(_st(_exe("EXE-1"), _exe("EXE-2", status="failed", reason="assertion", **change)))
    assert (r["outcome"], r["evidence"]) == ("MET", ["EXE-1"])


def test_a_passing_rerun_cannot_hide_a_failed_root():
    r = _suite(_st(_exe("EXE-1", status="failed", reason="assertion"), _exe("EXE-2", attempt_of="EXE-1")))
    assert (r["outcome"], r["detail"], r["evidence"]) == ("NOT_MET", "EXE-1 failed (assertion)", ["EXE-1"])


def test_a_passing_rerun_cannot_hide_a_failed_root_end_to_end(repo_ws, actor, py):
    first = _run(repo_ws, actor, py, "fail")
    _run(repo_ws, actor, py, "pass", attempt_of=first)
    e = gate.evaluate(repo_ws, actor, profile="change")
    se, flaky = _criterion(e, "suite_executed"), _criterion(e, "flaky_rate_max")
    assert (e["data"]["verdict"], se["outcome"], se["evidence"]) == ("NOT_READY", "NOT_MET", [first])
    assert (flaky["outcome"], flaky["detail"], flaky["evidence"]) == ("NOT_MET", "1/1 flaky (1 > 0.02)", [first])


def test_a_scan_as_the_latest_execution_does_not_count_end_to_end(repo_ws, actor, py):
    suite = _run(repo_ws, actor, py)
    execution.execute(repo_ws, actor, execution.RunRequest(command=py.argv(WRITE_SARIF, "{evidence}/r.sarif"), reports=(("sarif", "{evidence}/r.sarif"),)))
    se = _criterion(gate.evaluate(repo_ws, actor, profile="change"), "suite_executed")
    assert (se["outcome"], se["evidence"]) == ("MET", [suite])


def test_a_clean_scan_alone_is_not_a_suite_run():
    r = _suite(_st(_exe(report={"kind": "alerts", "alerts": 0})))
    assert r["outcome"] == "NO_EVIDENCE"


def test_a_run_with_no_report_qualifies_on_its_exit_code_under_change():
    assert _suite(_st(_exe(report=None, strength="exit_code")))["outcome"] == "MET"


def test_clean_tree_needs_a_clean_run_only_when_the_profile_says_so():
    dirty = _exe(scope="full", git=dict(GIT, dirty=True))
    assert _suite(_st(dirty), profile="release")["outcome"] == "NO_EVIDENCE"
    assert _suite(_st(dirty), profile="audit")["outcome"] == "MET"
    assert _suite(_st(dirty), profile="change")["outcome"] == "MET"


def test_full_scope_satisfies_targeted_but_not_the_reverse():
    assert _suite(_st(_exe(scope="full")), profile="change")["outcome"] == "MET"
    assert _suite(_st(_exe(scope="targeted")), profile="audit")["outcome"] == "NO_EVIDENCE"
    assert _suite(_st(_exe(scope="full")), profile="audit")["outcome"] == "MET"


@pytest.mark.parametrize("strength,minimum,counts", [
    ("claimed", "attested", False), ("attested", "attested", True), ("attested", "exit_code", False), ("exit_code", "exit_code", True),
    ("exit_code", "structured", False), ("structured", "structured", True), ("structured", None, True), (None, None, True),
])
def test_strength_order(strength, minimum, counts):
    criterion = dict(_crit("change", "suite_executed"), min_strength=minimum)
    r = gate.evaluate_criterion(criterion, _st(_exe(strength=strength)), _context())
    assert r["outcome"] == ("MET" if counts else "NO_EVIDENCE")


def test_exit_code_strength_is_rejected_under_release(repo_ws, actor, py):
    execution.execute(repo_ws, actor, execution.RunRequest(command=py.argv("pass"), scope="full"))
    se = _criterion(gate.evaluate(repo_ws, actor, profile="release"), "suite_executed")
    assert (se["outcome"], se["evidence"]) == ("NO_EVIDENCE", []) and "no execution qualified" in se["detail"]


@pytest.mark.parametrize("status,reason", [("blocked", "tool_missing"), ("inconclusive", "timeout"), ("not_run", "all_skipped")])
@pytest.mark.parametrize("require_passed", [True, False])
def test_a_latest_with_no_verdict_is_no_evidence_naming_it(status, reason, require_passed):
    run = _exe("EXE-2", status=status, reason=reason, strength=None)
    r = _suite(_st(_exe("EXE-1"), run), suite_executed={"params": {"require_passed": require_passed}})
    assert (r["outcome"], r["detail"], r["evidence"]) == ("NO_EVIDENCE", "EXE-2 %s (%s)" % (status, reason), ["EXE-2"])


def test_strength_does_not_apply_to_a_run_with_no_verdict():
    r = _suite(_st(_exe(status="blocked", reason="tool_missing", strength=None, scope="full")), profile="release")
    assert (r["outcome"], r["evidence"]) == ("NO_EVIDENCE", ["EXE-1"]) and "tool_missing" in r["detail"]


def test_the_latest_qualifying_execution_decides():
    failed = _exe("EXE-1", status="failed", reason="assertion", git=dict(GIT, diff_hash=OTHER_TREE))
    assert _suite(_st(failed, _exe("EXE-2")))["evidence"] == ["EXE-2"]
    assert _suite(_st(_exe("EXE-2"), failed))["outcome"] == "NOT_MET"


@pytest.mark.parametrize("change", WEAK.values(), ids=WEAK)
def test_a_weak_pass_is_no_evidence(change):
    r = _suite(_st(_exe(**change)))
    assert (r["outcome"], r["evidence"]) == ("NO_EVIDENCE", []) and "no execution qualified" in r["detail"]


@pytest.mark.parametrize("change", WEAK.values(), ids=WEAK)
def test_a_weak_failure_is_still_the_latest_word(change):  # I3
    r = _suite(_st(_exe("EXE-1"), _exe("EXE-2", status="failed", reason="signal", **change)))
    assert (r["outcome"], r["evidence"]) == ("NOT_MET", ["EXE-2"])


def test_a_failure_too_weak_for_release_still_fails_the_gate():  # I3
    runs = _st(_exe("EXE-1", scope="full"), _exe("EXE-2", status="failed", reason="exit_nonzero", strength="exit_code", scope="full"))
    r = _suite(runs, profile="release")
    assert (r["outcome"], r["evidence"]) == ("NOT_MET", ["EXE-2"])


def test_a_pass_after_a_failure_on_the_same_tree_is_not_met():  # I4, §5.1, §8.3
    r = _suite(_st(_exe("EXE-1", status="failed", reason="assertion"), _exe("EXE-2")))
    assert (r["outcome"], r["evidence"]) == ("NOT_MET", ["EXE-1", "EXE-2"])
    assert "EXE-1" in r["detail"] and "EXE-2" in r["detail"]


@pytest.mark.parametrize("change", [
    {"git": dict(GIT, diff_hash=OTHER_TREE)}, {"environment_id": "staging"}, {"scope": "full"},
], ids=["another_tree", "another_environment", "another_scope"])
def test_a_failure_elsewhere_does_not_taint_the_pass(change):  # I4
    r = _suite(_st(_exe("EXE-1", status="failed", reason="assertion", **change), _exe("EXE-2")))
    assert (r["outcome"], r["evidence"]) == ("MET", ["EXE-2"])


def test_mixed_outcomes_are_met_when_a_pass_is_not_required():  # I4: this criterion only asks that the suite ran
    r = _suite(_st(_exe("EXE-1", status="failed", reason="assertion"), _exe("EXE-2")), suite_executed={"params": {"require_passed": False}})
    assert (r["outcome"], r["evidence"]) == ("MET", ["EXE-2"])


def _result(result_id, execution_id="EXE-1", status_now="passed", reclassified=False):
    changes = [{"event": "EVT-1", "from": "passed", "to": status_now, "reason": "r", "evidence": [execution_id]}] if reclassified else []
    return {"id": result_id, "execution": execution_id, "status": status_now, "reclassifications": changes}


def test_a_result_reclassified_down_withdraws_the_pass():  # M8
    results = {"RES-1": _result("RES-1"), "RES-2": _result("RES-2", status_now="inconclusive", reclassified=True)}
    r = _suite(_st(_exe(), results=results))
    assert (r["outcome"], r["evidence"]) == ("NO_EVIDENCE", ["EXE-1", "RES-2"]) and "RES-2" in r["detail"]


def test_a_reclassified_result_of_another_execution_changes_nothing():  # M8
    r = _suite(_st(_exe(), results={"RES-2": _result("RES-2", "EXE-9", status_now="inconclusive", reclassified=True)}))
    assert (r["outcome"], r["evidence"]) == ("MET", ["EXE-1"])


def test_reclassifying_a_recorded_result_reaches_the_gate(repo_ws, actor, py):  # M8, through the real event
    run = execution.execute(repo_ws, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))
    assert gate.evaluate(repo_ws, actor, profile="change")["data"]["verdict"] == "READY"
    result = next(rid for rid, rec in state.load(repo_ws)["results"].items() if rec["execution"] == run["execution"])
    execution.reclassify(repo_ws, actor, result, "inconclusive", "the oracle was wrong", [run["execution"]])
    se = _criterion(gate.evaluate(repo_ws, actor, profile="change"), "suite_executed")
    assert se["outcome"] == "NO_EVIDENCE" and result in se["evidence"]


def test_require_passed_is_the_default_and_false_accepts_a_failed_latest():
    failed = _exe(status="failed", reason="assertion")
    criterion = _crit("change", "suite_executed")
    del criterion["params"]["require_passed"]
    assert gate.evaluate_criterion(criterion, _st(failed), _context())["outcome"] == "NOT_MET"
    r = _suite(_st(failed), suite_executed={"params": {"require_passed": False}})
    assert (r["outcome"], r["evidence"]) == ("MET", ["EXE-1"]) and r["detail"].startswith("EXE-1 failed (assertion)")


def test_a_passed_latest_is_met_with_its_evidence():
    r = _suite(_st(_exe()))
    assert (r["outcome"], r["detail"], r["evidence"]) == ("MET", "EXE-1 passed", ["EXE-1"])


def test_a_null_target_is_no_evidence_naming_the_limitation():
    r = _suite(_st(_exe()), context=_context(target_commit=None, git_limitation="no_commit"))
    assert (r["outcome"], r["evidence"]) == ("NO_EVIDENCE", []) and "no_commit" in r["detail"]


def test_no_execution_at_all_says_none_qualified():
    r = _suite(_st())
    assert (r["outcome"], r["evidence"]) == ("NO_EVIDENCE", []) and "no execution qualified" in r["detail"]


# --------------------------------------------------------------------------- T16-R5: the working tree

def test_a_run_on_another_tree_reads_as_changed_since():
    r = _suite(_st(_exe()), context=_context(diff_hash=OTHER_TREE))
    assert (r["outcome"], r["evidence"]) == ("NO_EVIDENCE", [])
    assert "the working tree changed since the execution" in r["detail"] and "EXE-1" in r["detail"]


def test_the_latest_run_on_this_tree_is_the_one_that_counts():
    st = _st(_exe("EXE-1"), _exe("EXE-2", status="failed", reason="assertion", git=dict(GIT, diff_hash=OTHER_TREE)))
    assert _suite(st, context=_context(diff_hash=TREE))["evidence"] == ["EXE-1"]
    assert _suite(st, context=_context(diff_hash=OTHER_TREE))["evidence"] == ["EXE-2"]


def test_an_edit_after_the_run_reads_no_evidence_under_change(repo_ws, actor, py):
    exe = _run(repo_ws, actor, py)
    (repo_ws.root / "a.txt").write_text("2")
    g = gate.evaluate(repo_ws, actor, profile="change")
    se = _criterion(g, "suite_executed")
    assert (g["data"]["verdict"], se["outcome"], se["evidence"]) == ("INSUFFICIENT_EVIDENCE", "NO_EVIDENCE", [])
    assert "the working tree changed since the execution" in se["detail"] and exe in se["detail"]


def test_a_run_on_the_dirty_tree_being_evaluated_counts(repo_ws, actor, py):
    (repo_ws.root / "a.txt").write_text("2")
    exe = _run(repo_ws, actor, py)
    se = _criterion(gate.evaluate(repo_ws, actor, profile="change"), "suite_executed")
    assert (se["outcome"], se["evidence"]) == ("MET", [exe])


def test_an_explicit_target_commit_does_not_compare_the_tree(repo_ws, actor, py):
    exe = _run(repo_ws, actor, py)
    (repo_ws.root / "a.txt").write_text("2")
    se = _criterion(gate.evaluate(repo_ws, actor, profile="change", target_commit=_head(repo_ws)), "suite_executed")
    assert (se["outcome"], se["evidence"]) == ("MET", [exe])


def test_with_an_explicit_target_clean_tree_still_governs(repo_ws, actor, py):
    (repo_ws.root / "a.txt").write_text("2")
    _run(repo_ws, actor, py, scope="full")
    se = _criterion(gate.evaluate(repo_ws, actor, profile="release", target_commit=_head(repo_ws)), "suite_executed")
    assert se["outcome"] == "NO_EVIDENCE"


# --------------------------------------------------------------------------- T16-R18: what a tree mismatch means

TREE_CHANGED = ("the working tree changed since the execution %s was recorded; "
                "files a run leaves in the tree count unless they are git-ignored")


def _run_leaving(ws, actor, py, name):
    """A passing run that also writes ``name`` into the tree, as a coverage tool does."""
    code = "import pathlib; pathlib.Path(%r).write_text('<coverage/>')\n%s" % (name, WRITE_JUNIT)
    return execution.execute(ws, actor, req(py, code, "{evidence}/junit.xml", "pass"))["execution"]


def test_a_tree_mismatch_says_what_it_means_and_what_to_do():
    r = _suite(_st(_exe()), context=_context(diff_hash=OTHER_TREE))
    assert (r["outcome"], r["detail"], r["evidence"]) == ("NO_EVIDENCE", TREE_CHANGED % "EXE-1", [])


def test_a_tree_mismatch_names_the_latest_run_it_rules_out():
    r = _suite(_st(_exe("EXE-1"), _exe("EXE-2")), context=_context(diff_hash=OTHER_TREE))
    assert r["detail"] == TREE_CHANGED % "EXE-2"


def test_a_run_disqualified_for_another_reason_is_not_blamed_on_the_tree():
    r = _suite(_st(_exe()), suite_executed={"params": {"scope": "full"}}, context=_context(diff_hash=OTHER_TREE))
    assert "no execution qualified" in r["detail"] and "working tree" not in r["detail"]


def test_a_run_that_leaves_an_untracked_file_says_what_to_do(repo_ws, actor, py):
    exe = _run_leaving(repo_ws, actor, py, "coverage.xml")
    g = gate.evaluate(repo_ws, actor, profile="change")
    se = _criterion(g, "suite_executed")
    assert (g["data"]["verdict"], se["outcome"], se["detail"], se["evidence"]) == (
        "INSUFFICIENT_EVIDENCE", "NO_EVIDENCE", TREE_CHANGED % exe, [])


def test_a_run_that_leaves_a_git_ignored_file_counts(repo_ws, actor, py):
    (repo_ws.root / ".gitignore").write_text("coverage.xml\n")
    _git(repo_ws, "add", ".gitignore")
    _git(repo_ws, "commit", "-q", "-m", "ignore what the run leaves")
    exe = _run_leaving(repo_ws, actor, py, "coverage.xml")
    se = _criterion(gate.evaluate(repo_ws, actor, profile="change"), "suite_executed")
    assert (se["outcome"], se["evidence"]) == ("MET", [exe])


# --------------------------------------------------------------------------- T16-R6: the target

@pytest.mark.parametrize("bad", ["abc123", "A" * 40, "a" * 39, "a" * 41, "g" * 40, "a" * 40 + "\n", "", 7])
def test_invalid_target_is_refused(trusted, actor, bad):
    _refused("invalid_target", trusted, gate.evaluate, trusted, actor, profile="change", target_commit=bad)


def test_the_default_target_is_the_current_commit(repo_ws, actor):
    assert gate.evaluate(repo_ws, actor, profile="change")["data"]["target"] == {"commit": _head(repo_ws)}


def test_an_explicit_target_is_recorded(repo_ws, actor):
    assert gate.evaluate(repo_ws, actor, profile="change", target_commit=OTHER_COMMIT)["data"]["target"] == {"commit": OTHER_COMMIT}


def test_no_git_facts_is_a_null_target_and_the_evaluation_still_runs(trusted, actor):
    e = gate.evaluate(trusted, actor, profile="change")
    se = _criterion(e, "suite_executed")
    limitation = "not_a_git_repo" if shutil.which("git") else "git_unavailable"   # the Linux images ship no git
    assert e["data"]["target"] == {"commit": None} and se["outcome"] == "NO_EVIDENCE" and limitation in se["detail"]


def test_the_default_profile_comes_from_config(trusted, actor):
    assert gate.evaluate(trusted, actor)["data"]["profile"] == config.load_config(trusted)["gate"]["profile"] == "release"


# --------------------------------------------------------------------------- T16-R7: flaky_rate_max

def _flaky(st, context=None, profile="change", **overrides):
    return gate.evaluate_criterion(_crit(profile, "flaky_rate_max", **overrides), st, context or _context())


def _chain(attempts=1, flaky=False):
    return {"attempts": attempts, "flaky": flaky}


def _tests(n, flaky=0, root="EXE-1"):
    return {"t%d" % i: {root: _chain(flaky=i < flaky)} for i in range(n)}


def test_flaky_exactly_at_the_limit_is_met():
    r = _flaky(_st(_exe(), tests=_tests(50, flaky=1)))
    assert (r["outcome"], r["detail"], r["evidence"]) == ("MET", "1/50 flaky (0.02 <= 0.02)", ["EXE-1"])


def test_flaky_over_the_limit_is_not_met():
    r = _flaky(_st(_exe(), tests=_tests(40, flaky=1)))
    assert (r["outcome"], r["detail"], r["evidence"]) == ("NOT_MET", "1/40 flaky (0.025 > 0.02)", ["EXE-1"])


def test_no_flaky_tests_is_met_with_no_evidence_ids():
    r = _flaky(_st(_exe(), tests=_tests(3)))
    assert (r["outcome"], r["evidence"]) == ("MET", [])


def test_no_executed_tests_is_no_evidence():
    assert _flaky(_st(_exe()))["outcome"] == "NO_EVIDENCE"
    assert _flaky(_st(_exe(), tests={"t": {"EXE-1": _chain(attempts=0)}}))["outcome"] == "NO_EVIDENCE"


@pytest.mark.parametrize("change", [DISQUALIFIED["another_activity"], DISQUALIFIED["an_alert_report"], DISQUALIFIED["another_commit"],
                                    DISQUALIFIED["no_git_facts"]],
                         ids=["another_activity", "an_alert_report", "another_commit", "no_git_facts"])
def test_a_root_outside_conditions_1_and_3_is_not_counted(change):
    assert _flaky(_st(_exe(**change), tests=_tests(3)))["outcome"] == "NO_EVIDENCE"


def test_a_root_on_another_tree_is_not_counted():
    assert _flaky(_st(_exe(), tests=_tests(3)), context=_context(diff_hash=OTHER_TREE))["outcome"] == "NO_EVIDENCE"
    assert _flaky(_st(_exe(), tests=_tests(3)), context=_context(diff_hash=TREE))["outcome"] == "MET"


@pytest.mark.parametrize("change", [{"strength": "claimed"}, {"scope": "full"}, {"git": dict(GIT, dirty=True)}, {"status": "failed"}],
                         ids=["weak", "scope", "dirty", "failed"])
def test_the_other_conditions_do_not_apply_to_the_flaky_count(change):
    assert _flaky(_st(_exe(**change), tests=_tests(2)), profile="release")["detail"] == "0/2 flaky (0 <= 0.02)"


def test_a_test_counts_once_and_is_flaky_when_any_chain_is():
    st = _st(_exe("EXE-B"), _exe("EXE-A"), tests={"t": {"EXE-B": _chain(flaky=True), "EXE-A": _chain()},
                                                 "u": {"EXE-A": _chain(flaky=True), "EXE-B": _chain(flaky=True)}})
    r = _flaky(st)
    assert (r["detail"], r["evidence"]) == ("2/2 flaky (1 > 0.02)", ["EXE-A", "EXE-B"])


def test_only_chains_about_the_target_make_a_test_flaky():
    st = _st(_exe("EXE-A"), _exe("EXE-B", git=dict(GIT, commit=OTHER_COMMIT)),
             tests={"t": {"EXE-A": _chain(), "EXE-B": _chain(flaky=True)}})
    assert _flaky(st)["detail"] == "0/1 flaky (0 <= 0.02)"


def test_an_overridden_limit_applies():
    assert _flaky(_st(_exe(), tests=_tests(2, flaky=1)), flaky_rate_max={"params": {"max_ratio": 0.5}})["outcome"] == "MET"


# --------------------------------------------------------------------------- T16-R8: test_integrity

def _integrity(st=None, **context):
    return gate.evaluate_criterion(_crit("change", "test_integrity"), _st() if st is None else st, _context(**context))


@pytest.mark.parametrize("kernel,outcome", [
    ({"status": "ok", "source_checkout": False}, "MET"),
    ({"status": "ok", "source_checkout": True}, "MET"),
    ({"status": "absent", "source_checkout": True}, "MET"),
    ({"status": "absent", "source_checkout": False}, "NOT_MET"),
    ({"status": "modified", "source_checkout": False}, "NOT_MET"),
    ({"status": "modified", "source_checkout": True}, "NOT_MET"),
])
def test_kernel_status(kernel, outcome):
    assert _integrity(kernel=kernel)["outcome"] == outcome


def test_a_source_checkout_is_met_with_the_limitation():
    r = _integrity(kernel={"status": "absent", "source_checkout": True})
    assert (r["outcome"], r["detail"]) == ("MET", "kernel manifest applies to vendored copies (milestone 2)")


def test_the_detail_names_every_failure():
    st = _st(found={"FND-002": _finding(category="test-quality"), "FND-001": _finding(severity="LOW", category="test-quality")})
    problems = [Problem("missing", "a", "d"), Problem("modified", "b", "d"), Problem("missing", "c", "d")]
    r = _integrity(st, integrity_problems=problems, kernel={"status": "modified", "source_checkout": False})
    assert r["outcome"] == "NOT_MET" and r["evidence"] == ["FND-001", "FND-002"]
    for fragment in ("2 missing", "1 modified", "FND-001", "FND-002", "kernel modified"):
        assert fragment in r["detail"]


@pytest.mark.parametrize("record,outcome", [
    (_finding(severity="LOW", category="test-quality"), "NOT_MET"),
    (_finding(category="security"), "MET"),
    (_finding(category="test-quality", verification="false_positive", justification="disproved"), "MET"),
    (_finding(category="test-quality", fstate="accepted_risk", expires_at="2030-01-01T00:00:00.000Z"), "MET"),
    (_finding(category="test-quality", fstate="accepted_risk", expires_at="2026-01-01T00:00:00.000Z"), "NOT_MET"),
    (_finding(category="test-quality", fstate="wont_fix"), "MET"),
], ids=["low", "other_category", "false_positive", "accepted", "expired", "wont_fix"])
def test_unresolved_test_quality_findings_at_any_severity(record, outcome):
    assert _integrity(_st(found={"FND-001": record}))["outcome"] == outcome


def test_tampered_evidence_fails_the_gate_end_to_end(repo_ws, actor, py):
    exe = _run(repo_ws, actor, py)
    repo_ws.path("evidence", exe, "stdout.log").unlink()
    e = gate.evaluate(repo_ws, actor, profile="change")
    ti = _criterion(e, "test_integrity")
    assert (e["data"]["verdict"], ti["outcome"]) == ("NOT_READY", "NOT_MET") and "1 missing" in ti["detail"]


# --------------------------------------------------------------------------- T16-R9: independence_min

@pytest.mark.parametrize("have,need,outcome", [("I0", "I1", "NOT_MET"), ("I1", "I1", "MET"), ("I1", "I2", "NOT_MET"), ("I2", "I2", "MET"),
                                               ("I3", "I2", "MET"), ("I0", "I2", "NOT_MET")])
def test_independence_order(have, need, outcome):
    r = gate.evaluate_criterion(_crit("change", "independence_min", independence_min={"params": {"min": need}}), _st(), _context(independence=have))
    assert r["outcome"] == outcome
    if outcome == "NOT_MET":
        assert r["detail"] == "%s < %s" % (have, need)


def test_an_unknown_independence_is_never_enough():
    assert gate.evaluate_criterion(_crit("change", "independence_min"), _st(), _context(independence="I9"))["outcome"] == "NOT_MET"


# --------------------------------------------------------------------------- T16-R11: what is recorded

def test_the_event_holds_the_brief_fields_and_evaluated_at(trusted, actor, frozen_now):
    frozen_now.advance(5)
    heads = ledger.heads(trusted)
    e = gate.evaluate(trusted, actor, profile="change")
    d = e["data"]
    assert e["type"] == "gate.evaluated" and e["id"].startswith("EVT-") and set(d) == {
        "id", "profile", "policy_hash", "state_hash", "target", "diff_hash", "criteria", "independence", "verdict", "conditions", "heads",
        "evaluated_at"}
    assert (d["id"], d["profile"], d["independence"]) == ("GATE-001", "change", actor.independence)
    assert d["heads"] == heads and ledger.heads(trusted)[actor.session]["seq"] == heads[actor.session]["seq"] + 1
    assert d["evaluated_at"] == ids.format_ts(frozen_now())
    assert d["policy_hash"] == gate.load_policy("change", config.load_config(trusted))[1]
    assert d["state_hash"] == state.state_hash(state.load(trusted))
    assert [x["id"] for x in d["criteria"]] == [x["id"] for x in _file("change")["criteria"]]


def test_the_event_records_the_hash_of_the_tree_it_evaluated(repo_ws, actor):
    clean = gate.evaluate(repo_ws, actor, profile="change")["data"]["diff_hash"]
    assert clean == gitinfo.git_info(repo_ws.root)[0]["diff_hash"]
    (repo_ws.root / "a.txt").write_text("2")
    edited = gate.evaluate(repo_ws, actor, profile="change")["data"]["diff_hash"]
    assert edited == gitinfo.git_info(repo_ws.root)[0]["diff_hash"] != clean


def test_an_explicit_target_records_no_tree(repo_ws, actor):
    (repo_ws.root / "a.txt").write_text("2")
    assert gate.evaluate(repo_ws, actor, profile="change", target_commit=_head(repo_ws))["data"]["diff_hash"] is None


def test_no_git_facts_records_no_tree(trusted, actor):
    assert gate.evaluate(trusted, actor, profile="change")["data"]["diff_hash"] is None


def test_gate_ids_count_up(trusted, actor):
    assert [gate.evaluate(trusted, actor, profile="change")["data"]["id"] for _ in range(2)] == ["GATE-001", "GATE-002"]


def test_independence_comes_from_the_actor(trusted, actor):
    evaluator = dataclasses.replace(actor, independence="I3")
    e = gate.evaluate(trusted, evaluator, profile="change")
    assert e["data"]["independence"] == "I3" and _criterion(e, "independence_min")["outcome"] == "MET"


def test_evaluate_folds_afresh_under_the_lock(trusted, actor, monkeypatch):
    real_lock, real_load, held, seen = Workspace.lock, state.load, [], []

    @contextlib.contextmanager
    def tracking(self, *args, **kw):
        with real_lock(self, *args, **kw):
            held.append(True)
            try:
                yield
            finally:
                held.pop()

    def load(ws):
        seen.append(bool(held))
        return real_load(ws)

    monkeypatch.setattr(Workspace, "lock", tracking)
    monkeypatch.setattr(state, "load", load)
    gate.evaluate(trusted, actor, profile="change")
    assert seen == [True]


def test_an_unknown_profile_is_refused_before_anything_is_recorded(trusted, actor):
    _refused("unknown_profile", trusted, gate.evaluate, trusted, actor, profile="yolo")


def test_a_hand_edit_is_not_an_accepted_override(trusted, actor):
    # §11.1: an override applies only after you accept it; T11: an agent edits config.json to loosen the gate.
    cfg = config.load_config(trusted)
    cfg["gate"]["overrides"]["suite_executed"] = {"not_applicable": "trust me", "accepted_in": EVT}
    config.write_config(trusted, cfg)
    _refused("config_drift", trusted, gate.evaluate, trusted, actor, profile="change")


def test_an_edit_the_user_accepted_applies(trusted, actor):
    cfg = config.load_config(trusted)
    cfg["gate"]["overrides"]["suite_executed"] = {"not_applicable": "docs only", "accepted_in": EVT}
    config.write_config(trusted, cfg)
    config.accept(trusted, actor, "I reviewed the edit")
    assert _criterion(gate.evaluate(trusted, actor, profile="change"), "suite_executed")["outcome"] == "NOT_APPLICABLE"


def _evaluated(**changes):
    data = {"id": "GATE-001", "profile": "change", "policy_hash": TREE, "state_hash": TREE, "target": {"commit": None}, "diff_hash": None,
            "criteria": [{"id": "x", "type": "suite_executed", "blocking": True, "outcome": "MET", "detail": "d", "evidence": []},
                         {"id": "y", "type": "mutation_score_min", "blocking": False, "outcome": "NOT_APPLICABLE", "detail": "w", "evidence": [], "justification": "w"}],
            "independence": "I1", "verdict": "READY", "conditions": [], "heads": {"S-" + "0" * 26: {"seq": 1, "hash": TREE}},
            "evaluated_at": "2026-11-08T12:00:00.000Z"}
    data.update(changes)
    return data


def test_the_recorded_shape_fits_its_schema():
    schema.validate(_evaluated(), EVALUATED)
    schema.validate(_evaluated(diff_hash=TREE), EVALUATED)


@pytest.mark.parametrize("break_it", [
    lambda d: d.update(extra=1),
    lambda d: d.pop("evaluated_at"),
    lambda d: d.pop("heads"),
    lambda d: d.pop("diff_hash"),
    lambda d: d.update(diff_hash="abc"),
    lambda d: d.update(target={"commit": None, "tag": "v1"}),
    lambda d: d.update(target={}),
    lambda d: d.update(verdict="MAYBE"),
    lambda d: d.update(profile="yolo"),
    lambda d: d.update(id="GATE-1"),
    lambda d: d["criteria"][0].update(extra=1),
    lambda d: d["criteria"][0].update(justification="not allowed with MET"),
    lambda d: d["criteria"][0].update(outcome="UNKNOWN"),
    lambda d: d["criteria"][1].pop("justification"),
    lambda d: d["criteria"][0].pop("detail"),
    lambda d: d["criteria"][0].update(detail=None),
    lambda d: d.update(heads={"S-" + "0" * 26: {"seq": 1, "hash": TREE, "extra": 1}}),
])
def test_the_event_schema_is_closed(break_it):
    data = _evaluated()
    break_it(data)
    with pytest.raises(QaError) as caught:
        schema.validate(data, EVALUATED)
    assert caught.value.code == "schema_invalid"


# --------------------------------------------------------------------------- T16-R12: propose and accept

def test_propose_records_and_returns_the_event(trusted, actor):
    e = gate.propose(trusted, actor, "flaky_rate_max", {"params": {"max_ratio": 0.05}}, "a slow CI")
    assert e["type"] == "gate.policy_proposed" and e["id"].startswith("EVT-")
    assert e["data"] == {"criterion": "flaky_rate_max", "override": {"params": {"max_ratio": 0.05}}, "rationale": "a slow CI"}
    assert state.load(trusted)["config"]["history"][-1] == {"event": e["id"], "kind": "gate_policy_proposed", "criterion": "flaky_rate_max",
                                                            "override": {"params": {"max_ratio": 0.05}}, "rationale": "a slow CI"}


@pytest.mark.parametrize("criterion_id", ["no_unresolved_findings", "nope", "", None, 3])  # the first is a type: overrides are keyed by id
def test_propose_unknown_criterion(trusted, actor, criterion_id):
    _refused("unknown_criterion", trusted, gate.propose, trusted, actor, criterion_id, {"blocking": False}, "why")


@pytest.mark.parametrize("override", [
    {}, {"params": {}}, None, [], "blocking", {"accepted_in": EVT}, {"blocking": False, "accepted_in": EVT}, {"nope": 1},
    {"enabled": "yes"}, {"enabled": 1}, {"blocking": "no"}, {"blocking": 0}, {"params": []}, {"params": "x"}, {"not_applicable": 3},
    {"not_applicable": None}, {"not_applicable": ""}, {"not_applicable": "  \n\t"},
    {"params": {"max_ratio": 0.5}},  # another criterion's parameter
    {"params": {"made_up": 1}},
    {"params": {"scope": "everything"}}, {"params": {"scope": 1}}, {"params": {"clean_tree": "no"}}, {"params": {"require_passed": 1}},
], ids=repr)
def test_propose_invalid_override(trusted, actor, override):
    _refused("invalid_override", trusted, gate.propose, trusted, actor, "suite_executed", override, "why")


@pytest.mark.parametrize("criterion_id,override", [
    ("flaky_rate_max", {"params": {"max_ratio": "low"}}), ("flaky_rate_max", {"params": {"max_ratio": True}}), ("flaky_rate_max", {"params": {"max_ratio": 1.5}}),
    ("flaky_rate_max", {"params": {"max_ratio": -0.1}}), ("no_unresolved_high", {"params": {"min_severity": "SEVERE"}}),
    ("independence_min", {"params": {"min": "I9"}}), ("mutation_score_min", {"params": {"min_score": "high"}}), ("mutation_score_min", {"params": {"min_score": 101}}),
    ("test_integrity", {"params": {"scope": "full"}}),  # declares no parameters at all
], ids=repr)
def test_propose_refuses_a_param_value_or_key_the_criterion_cannot_use(trusted, actor, criterion_id, override):
    _refused("invalid_override", trusted, gate.propose, trusted, actor, criterion_id, override, "why")


@pytest.mark.parametrize("criterion_id,override", [
    ("suite_executed", {"enabled": False}), ("suite_executed", {"blocking": False}), ("suite_executed", {"not_applicable": "docs"}),
    ("suite_executed", {"params": {"scope": "full", "clean_tree": True, "require_passed": False}}),
    ("no_unresolved_high", {"params": {"min_severity": "CRITICAL"}}), ("flaky_rate_max", {"params": {"max_ratio": 0}}),
    ("independence_min", {"params": {"min": "I3"}}), ("mutation_score_min", {"enabled": True, "params": {"min_score": 80.5}}),
    ("rollback_validated", {"blocking": False}),  # a criterion of one profile only
], ids=repr)
def test_propose_takes_every_valid_shape(trusted, actor, criterion_id, override):
    assert gate.propose(trusted, actor, criterion_id, override, "why")["data"]["override"] == override


@pytest.mark.parametrize("rationale", ["", "   ", "\n", None, 3])
def test_propose_blank_rationale(trusted, actor, rationale):
    _refused("blank_rationale", trusted, gate.propose, trusted, actor, "suite_executed", {"blocking": False}, rationale)


def test_accept_writes_the_override_and_records_the_hash(trusted, actor):
    p = gate.propose(trusted, actor, "suite_executed", {"blocking": False}, "why")
    e = gate.accept(trusted, actor, p["id"], "yes, advisory")
    assert e["type"] == "gate.policy_accepted"
    assert without_mac(e["data"]) == {"proposal": p["id"], "approval": "yes, advisory", "config_hash": config.current_hash(trusted)}
    assert config.load_config(trusted)["gate"]["overrides"] == {"suite_executed": {"blocking": False, "accepted_in": p["id"]}}
    st = state.load(trusted)
    assert config.accepted_hash(st) == config.current_hash(trusted)
    assert st["config"]["history"][-1] == {"event": e["id"], "kind": "gate_policy_accepted", "proposal": p["id"], "hash": config.current_hash(trusted)}


def test_a_later_proposal_for_the_criterion_replaces_the_override(trusted, actor):
    _accept(trusted, actor, "suite_executed", {"blocking": False})
    p = gate.propose(trusted, actor, "suite_executed", {"enabled": False}, "why")
    gate.accept(trusted, actor, p["id"], "yes")
    assert config.load_config(trusted)["gate"]["overrides"]["suite_executed"] == {"enabled": False, "accepted_in": p["id"]}


def test_enabled_false_applies_end_to_end_and_names_the_proposal(trusted, actor):
    p = gate.propose(trusted, actor, "test_integrity", {"enabled": False}, "why")
    gate.accept(trusted, actor, p["id"], "yes")
    ti = _criterion(gate.evaluate(trusted, actor, profile="change"), "test_integrity")
    assert (ti["outcome"], ti["blocking"], ti["justification"]) == ("NOT_APPLICABLE", False, "Disabled by config.gate.overrides (%s)" % p["id"])


def test_accepting_changes_the_policy_hash(trusted, actor):
    before = gate.evaluate(trusted, actor, profile="change")["data"]["policy_hash"]
    _accept(trusted, actor, "flaky_rate_max", {"params": {"max_ratio": 0.5}})
    assert gate.evaluate(trusted, actor, profile="change")["data"]["policy_hash"] != before


@pytest.mark.parametrize("approval", ["", "  ", None])
def test_accept_needs_an_approval_quote(trusted, actor, approval):
    p = gate.propose(trusted, actor, "suite_executed", {"blocking": False}, "why")
    _refused("approval_required", trusted, gate.accept, trusted, actor, p["id"], approval)


def test_accept_refuses_on_a_drifted_file_so_a_hand_edit_is_not_laundered(trusted, actor):
    p = gate.propose(trusted, actor, "suite_executed", {"blocking": False}, "why")
    trusted.path("config.json").write_bytes(trusted.path("config.json").read_bytes() + b" ")
    _refused("config_drift", trusted, gate.accept, trusted, actor, p["id"], "yes")


@pytest.mark.parametrize("make_id", [lambda p, init: EVT, lambda p, init: init, lambda p, init: None])
def test_accept_unknown_proposal(trusted, actor, make_id):
    p = gate.propose(trusted, actor, "suite_executed", {"blocking": False}, "why")
    initialized = state.load(trusted)["config"]["history"][0]["event"]  # an event of another kind is not a proposal
    _refused("unknown_proposal", trusted, gate.accept, trusted, actor, make_id(p, initialized), "yes")


def test_accept_twice_is_already_accepted(trusted, actor):
    p = gate.propose(trusted, actor, "suite_executed", {"blocking": False}, "why")
    gate.accept(trusted, actor, p["id"], "yes")
    _refused("already_accepted", trusted, gate.accept, trusted, actor, p["id"], "yes again")


def test_accept_restores_the_file_when_the_append_fails(trusted, actor, monkeypatch):
    p = gate.propose(trusted, actor, "suite_executed", {"blocking": False}, "why")
    before = trusted.path("config.json").read_bytes()
    real, during = ledger.append, []

    def failing(ws, who, event_type, data):
        if event_type == "gate.policy_accepted":
            during.append(trusted.path("config.json").read_bytes())
            raise QaError("ledger_write_failed", "disk full")
        return real(ws, who, event_type, data)

    monkeypatch.setattr(ledger, "append", failing)
    with pytest.raises(QaError) as caught:
        gate.accept(trusted, actor, p["id"], "yes")
    assert caught.value.code == "ledger_write_failed"
    assert during[0] != before and trusted.path("config.json").read_bytes() == before


def test_accept_runs_under_the_lock(trusted, actor, monkeypatch):
    p = gate.propose(trusted, actor, "suite_executed", {"blocking": False}, "why")
    real_lock, real_load, held, seen = Workspace.lock, state.load, [], []

    @contextlib.contextmanager
    def tracking(self, *args, **kw):
        with real_lock(self, *args, **kw):
            held.append(True)
            try:
                yield
            finally:
                held.pop()

    def load(ws):
        seen.append(bool(held))
        return real_load(ws)

    monkeypatch.setattr(Workspace, "lock", tracking)
    monkeypatch.setattr(state, "load", load)
    gate.accept(trusted, actor, p["id"], "yes")
    assert seen == [True]


# --------------------------------------------------------------------------- T16-R13: the fold

def _accepted_event(ws, actor, proposal, config_hash=OTHER_TREE):
    return ledger.append(ws, actor, "gate.policy_accepted", {"proposal": proposal, "approval": "yes", "config_hash": config_hash})


def test_gate_evaluated_folds_into_gates(trusted, actor):
    e = gate.evaluate(trusted, actor, profile="change")
    assert state.load(trusted)["gates"] == {"GATE-001": e["data"]}


def test_a_repeated_gate_id_is_a_violation_and_the_first_stays(trusted, actor):
    first = gate.evaluate(trusted, actor, profile="change")["data"]
    repeat = ledger.append(trusted, actor, "gate.evaluated", dict(first, verdict="READY"))
    st = state.load(trusted)
    assert st["gates"] == {"GATE-001": first} and st["violations"] == [{"event": repeat["id"], "kind": "duplicate_id"}]


def test_an_acceptance_with_no_proposal_is_a_violation_and_changes_nothing(trusted, actor):
    before = state.load(trusted)["config"]
    e = _accepted_event(trusted, actor, EVT)
    st = state.load(trusted)
    assert st["config"] == before and st["violations"] == [{"event": e["id"], "kind": "unknown_proposal"}]


def test_an_acceptance_naming_an_event_that_is_no_proposal_is_a_violation(trusted, actor):
    before = state.load(trusted)["config"]
    e = _accepted_event(trusted, actor, before["history"][0]["event"])
    st = state.load(trusted)
    assert st["config"] == before and st["violations"] == [{"event": e["id"], "kind": "unknown_proposal"}]


def test_an_acceptance_folded_before_its_proposal_still_applies(trusted, actor, frozen_now):
    frozen_now.set(T0 + timedelta(seconds=10))
    p = gate.propose(trusted, actor, "suite_executed", {"blocking": False}, "why")
    # the clock stepped back between the proposal and its acceptance, in a ledger written before append clamped
    e = write_event_at(trusted, actor, T0, "gate.policy_accepted", {"proposal": p["id"], "approval": "yes", "config_hash": OTHER_TREE})
    assert [ev["type"] for ev in ledger.iter_all_events(trusted)][-2:] == ["gate.policy_accepted", "gate.policy_proposed"]
    st = state.load(trusted)
    assert st["violations"] == [] and config.accepted_hash(st) == OTHER_TREE
    assert {"event": e["id"], "kind": "gate_policy_accepted", "proposal": p["id"], "hash": OTHER_TREE} in st["config"]["history"]


def test_a_config_change_after_an_acceptance_stays_the_accepted_one(trusted, actor):
    _accept(trusted, actor, "suite_executed", {"blocking": False})
    config.set_value(trusted, actor, "defaults.flaky_reruns", 2, "ok")
    st = state.load(trusted)
    assert config.accepted_hash(st) == config.current_hash(trusted)
    assert config.check_drift(trusted, st, actor, "test-execution-local") is None
    assert [h["kind"] for h in st["config"]["history"]][-3:] == ["gate_policy_proposed", "gate_policy_accepted", "changed"]


def test_a_forged_acceptance_before_a_real_change_leaves_the_real_one_accepted(trusted, actor):
    _accepted_event(trusted, actor, EVT)
    config.set_value(trusted, actor, "defaults.flaky_reruns", 2, "ok")
    st = state.load(trusted)
    assert config.accepted_hash(st) == config.current_hash(trusted)
    assert "gate_policy_accepted" not in [h["kind"] for h in st["config"]["history"]]


def test_the_gate_events_replay_to_the_same_state(trusted, actor):
    _accept(trusted, actor, "suite_executed", {"blocking": False})
    gate.evaluate(trusted, actor, profile="change")
    assert state.fold(ledger.iter_all_events(trusted))["config"] == state.load(trusted)["config"]
    assert integrity.problems(trusted, state.load(trusted)) == []


# --------------------------------------------------------------------------- requirements_verified and no_blocked_critical (milestone 3)

TEST_ID = test_trace.TEST_ID


def _trace_run(ws, actor, letter, status, *, strength="structured", commit=COMMIT, minute=1, test_id=TEST_ID, execution_status="passed", **changes):
    """An execution on ``commit`` of ``strength`` holding one result of ``test_id``."""
    exe = test_trace.exe_id(letter)
    data = dict(test_compare._execution(exe), strength=strength, status=execution_status, **changes)
    data["started_at"] = data["ended_at"] = "2026-11-10T10:%02d:00.000Z" % minute
    data["git"] = {"commit": commit, "branch": None, "dirty": False, "diff_hash": TREE}
    ledger.append(ws, actor, "execution.recorded", data)
    ledger.append(ws, actor, "result.recorded", test_compare._result(minute * 100, exe, test_id, status))
    return exe


@pytest.fixture
def traced(trusted, actor):
    """REQ-001 (must, documented), AC-001.1, TC-001 verifying it and automated_by TEST_ID; no execution yet."""
    test_trace.chain(trusted, actor)
    return trusted


def _rv(ws, **overrides):
    return gate.evaluate_criterion(_crit("release", "requirements_verified", **overrides), state.load(ws), _context())


def _critical(ws, **overrides):
    return gate.evaluate_criterion(_crit("release", "no_blocked_critical", **overrides), state.load(ws), _context())


def _add_requirement(ws, actor, number, **changes):
    """REQ-<number> with its AC and a TC automated_by its own test."""
    records.add(ws, actor, "requirement", test_records.req(statement="Requirement %d holds." % number, **changes))
    records.add(ws, actor, "criterion", test_records.ac("REQ-%03d" % number))
    records.add(ws, actor, "testcase", test_records.tc(links=("AC-%03d.1" % number,)))
    test_id = "tests.test_other::test_%d" % number
    trace.link(ws, actor, "TC-%03d" % number, "automated_by", test_id)
    return test_id


def test_no_requirements_is_no_evidence(trusted):
    r = _rv(trusted)
    assert (r["outcome"], r["detail"], r["evidence"]) == ("NO_EVIDENCE", "no documented requirements", [])


def test_every_must_ac_passed_structured_is_met(traced, actor):
    _trace_run(traced, actor, "A", "passed")
    r = _rv(traced)
    assert r["outcome"] == "MET" and "REQ-001" in r["detail"]


def test_a_should_requirement_is_out_of_scope_by_default(traced, actor):
    _trace_run(traced, actor, "A", "passed")
    _add_requirement(traced, actor, 2, priority="should")   # no result for it: outside min_priority
    assert _rv(traced)["outcome"] == "MET"


def test_characterization_oracle_never_verifies(trusted, actor):
    test_trace.chain(trusted, actor, oracle="characterization")
    _trace_run(trusted, actor, "A", "passed")
    r = _rv(trusted)
    assert r["outcome"] == "NO_EVIDENCE" and "AC-001.1" in r["detail"]


def test_exit_code_strength_does_not_verify(traced, actor):
    _trace_run(traced, actor, "A", "passed", strength="exit_code")
    r = _rv(traced)
    assert r["outcome"] == "NO_EVIDENCE" and "AC-001.1" in r["detail"]


def test_a_result_on_another_commit_does_not_verify(traced, actor):
    _trace_run(traced, actor, "A", "passed", commit=OTHER_COMMIT)
    assert _rv(traced)["outcome"] == "NO_EVIDENCE"


def test_failed_latest_result_is_not_met(traced, actor):
    _trace_run(traced, actor, "A", "passed", minute=1)
    _trace_run(traced, actor, "B", "failed", minute=2)
    r = _rv(traced)
    assert r["outcome"] == "NOT_MET" and "AC-001.1" in r["detail"]


def test_the_latest_result_decides_not_the_first(traced, actor):
    _trace_run(traced, actor, "A", "failed", minute=1)
    _trace_run(traced, actor, "B", "passed", minute=2)
    assert _rv(traced)["outcome"] == "MET"


def test_unconfirmed_inferred_requirement_is_no_evidence(traced, actor):
    _trace_run(traced, actor, "A", "passed")
    other = _add_requirement(traced, actor, 2, provenance="inferred")
    _trace_run(traced, actor, "B", "passed", minute=2, test_id=other)
    r = _rv(traced)
    assert r["outcome"] == "NO_EVIDENCE" and "REQ-002" in r["detail"] and "unconfirmed" in r["detail"]
    records.confirm_requirement(traced, actor, "REQ-002", "yes, that is the rule")
    assert _rv(traced)["outcome"] == "MET"


def test_an_unconfirmed_requirement_never_makes_the_criterion_not_met_alone(trusted, actor):
    other = _add_requirement(trusted, actor, 1, provenance="assumed")
    _trace_run(trusted, actor, "A", "failed", test_id=other)   # its own test fails, but nobody confirmed the requirement
    assert _rv(trusted)["outcome"] == "NO_EVIDENCE"


def test_an_unconfirmed_requirement_does_not_hide_a_documented_failure(traced, actor):
    _trace_run(traced, actor, "A", "failed")
    _add_requirement(traced, actor, 2, provenance="inferred")
    assert _rv(traced)["outcome"] == "NOT_MET"


def test_a_stale_confirmation_replayed_after_an_edit_leaves_the_requirement_unconfirmed(trusted, actor):
    records.add(trusted, actor, "requirement", test_records.req(provenance="inferred"))
    records.confirm_requirement(trusted, actor, "REQ-001", "yes")
    old = next(e for e in ledger.iter_all_events(trusted) if e["type"] == "requirement.confirmed")["data"]
    records.add(trusted, actor, "requirement", {"id": "REQ-001", "statement": "Changed: a discount may reach 90 percent."})
    write_event_at(trusted, actor, T0, "requirement.confirmed", old, signed=False)   # the old signed line, verbatim
    folded = state.load(trusted)
    assert "confirmed" not in folded["requirements"]["REQ-001"]
    assert {"event": folded["violations"][-1]["event"], "kind": "stale_confirmation"} in folded["violations"]
    records.add(trusted, actor, "criterion", test_records.ac())
    records.add(trusted, actor, "testcase", test_records.tc())
    trace.link(trusted, actor, "TC-001", "automated_by", TEST_ID)
    _trace_run(trusted, actor, "A", "passed")
    assert _rv(trusted)["outcome"] == "NO_EVIDENCE"   # the changed statement is unconfirmed, whatever the tests say


def test_a_stale_assumption_decision_replayed_after_an_edit_does_not_decide(trusted, actor):
    records.add(trusted, actor, "assumption", {"statement": "Prices are in euros.", "impact": "totals"})
    records.decide_assumption(trusted, actor, "ASM-001", "confirmed", "yes")
    old = next(e for e in ledger.iter_all_events(trusted) if e["type"] == "assumption.decided")["data"]
    records.add(trusted, actor, "assumption", {"id": "ASM-001", "statement": "Prices are in dollars."})
    assert state.load(trusted)["assumptions"]["ASM-001"]["status"] == "open"
    write_event_at(trusted, actor, T0, "assumption.decided", old, signed=False)
    assert state.load(trusted)["assumptions"]["ASM-001"]["status"] == "open"


def test_a_confirmation_of_the_current_statement_still_counts(trusted, actor):
    records.add(trusted, actor, "requirement", test_records.req(provenance="inferred"))
    records.add(trusted, actor, "requirement", {"id": "REQ-001", "statement": "Edited first."})
    records.confirm_requirement(trusted, actor, "REQ-001", "yes")
    assert state.load(trusted)["requirements"]["REQ-001"]["confirmed"]["approval"] == "yes"


def test_a_legacy_confirmation_without_a_statement_hash_still_folds(trusted, actor):
    records.add(trusted, actor, "requirement", test_records.req(provenance="inferred"))
    write_event_at(trusted, actor, T0, "requirement.confirmed", {"id": "REQ-001", "approval": "yes"})
    assert state.load(trusted)["requirements"]["REQ-001"]["confirmed"]["approval"] == "yes"


def _critical_chain(ws, actor, **tc_changes):
    test_trace.chain(ws, actor, criticality="critical", **tc_changes)


@pytest.mark.parametrize("status,outcome", [("passed", "MET"), ("blocked", "NOT_MET"), ("inconclusive", "NOT_MET"),
                                            ("failed", "NOT_MET"), ("not_run", "NO_EVIDENCE"), (None, "NO_EVIDENCE")])
def test_no_blocked_critical_truth_table(trusted, actor, status, outcome):
    _critical_chain(trusted, actor)
    if status is not None:
        _trace_run(trusted, actor, "A", status)
    r = _critical(trusted)
    assert r["outcome"] == outcome and (outcome == "MET" or "TC-001" in r["detail"])


def test_no_critical_test_case_is_no_evidence(traced, actor):   # TC-001 here is criticality high
    r = _critical(traced)
    assert (r["outcome"], r["detail"]) == ("NO_EVIDENCE", "no test case is linked to a critical component")


def test_a_critical_test_case_without_an_automated_test_is_no_evidence(trusted, actor):
    _critical_chain(trusted, actor, automated=False)
    r = _critical(trusted)
    assert r["outcome"] == "NO_EVIDENCE" and "TC-001" in r["detail"]


def test_a_critical_result_on_another_commit_is_no_evidence(trusted, actor):
    _critical_chain(trusted, actor)
    _trace_run(trusted, actor, "A", "passed", commit=OTHER_COMMIT)
    assert _critical(trusted)["outcome"] == "NO_EVIDENCE"


def test_a_blocked_critical_outranks_a_missing_one(trusted, actor):
    _critical_chain(trusted, actor)
    records.add(trusted, actor, "testcase", test_records.tc(criticality="critical"))   # TC-002, never executed
    trace.link(trusted, actor, "TC-002", "automated_by", "tests.test_other::test_two")
    _trace_run(trusted, actor, "A", "blocked")
    r = _critical(trusted)
    assert r["outcome"] == "NOT_MET" and "TC-001" in r["detail"]


def test_require_passed_still_holds_with_requirements_met(traced, actor):
    """A failed suite is NOT_READY even when every acceptance criterion is verified."""
    suite = _trace_run(traced, actor, "A", "passed", execution_status="failed", scope="full")   # the execution failed; its one test passed
    evaluator = dataclasses.replace(actor, role="evaluator", independence="I2")
    g = gate.evaluate(traced, evaluator, profile="release", target_commit=COMMIT)["data"]
    assert _criterion({"data": g}, "requirements_verified")["outcome"] == "MET"
    assert _criterion({"data": g}, "suite_executed")["outcome"] == "NOT_MET" and suite in _criterion({"data": g}, "suite_executed")["detail"]
    assert g["verdict"] == "NOT_READY"


def test_the_gate_reads_the_target_commit_from_its_context(traced, actor):
    _trace_run(traced, actor, "A", "passed")
    r = gate.evaluate_criterion(_crit("release", "requirements_verified"), state.load(traced), _context(target_commit=None, git_limitation="no git"))
    assert r["outcome"] == "NO_EVIDENCE" and "no target commit" in r["detail"]
