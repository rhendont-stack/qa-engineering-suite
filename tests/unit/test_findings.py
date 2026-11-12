from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from helpers import REPO, WRITE_JUNIT, WRITE_SARIF, base, finishes, req
from qa import canonical, execution, findings, ingest, ledger, resource_dir, schema, state, vocab
from qa.errors import QaError

LATER = "2026-12-01T00:00:00.000Z"
SPEC = REPO / "docs" / "proposal" / "phase-2-architecture.md"


# --------------------------------------------------------------------------- the brief's tests

def test_rubric_has_28_anchors_with_spec_text():
    r = findings.load_rubric()["anchors"]
    assert len(r) == 28 and r["SEC-H1"]["severity"] == "HIGH" and r["SEC-H1"]["text"].startswith("Exploitable with low-privilege access")


def test_matrix_matches_spec():
    m = findings.load_matrix()
    expected = {"CRITICAL": ["P0", "P0", "P0", "P0"], "HIGH": ["P0", "P1", "P1", "P2"], "MEDIUM": ["P1", "P2", "P2", "P3"],
                "LOW": ["P2", "P3", "P3", "P3"], "INFORMATIONAL": ["none"] * 4}
    for sev, row in expected.items():
        assert [findings.compute_priority(sev, c, [], m) for c in ("critical", "high", "normal", "low")] == row


@pytest.mark.parametrize("sev,crit,adj,want", [("HIGH", "normal", ["outside_release_scope"], "P2"), ("LOW", "low", ["outside_release_scope"], "P3"),
                                               ("HIGH", "normal", ["public_exploit"], "P0"), ("INFORMATIONAL", "critical", ["public_exploit"], "none")])
def test_priority_adjustments(sev, crit, adj, want):
    assert findings.compute_priority(sev, crit, adj, findings.load_matrix()) == want


def test_severity_modifiers_clamp_and_once():
    r = findings.load_rubric()
    assert findings.compute_severity("SEC-C1", ["sensitive_data"], r) == "CRITICAL"
    assert findings.compute_severity("SEC-L1", ["unreachable", "dev_only"], r) == "INFORMATIONAL"
    with pytest.raises(QaError):
        findings.compute_severity("SEC-H1", ["unreachable", "unreachable"], r)


def test_appendix_a3_example_is_p0(ws, actor):
    e, created = findings.add(ws, actor, base())
    assert created and (e["data"]["id"], e["data"]["severity"], e["data"]["priority"]) == ("FND-001", "HIGH", "P0")


@pytest.mark.parametrize("over,needle", [
    (dict(verification_status="confirmed", impact="x", recommendation="y"), "confirmed"),
    (dict(probable_cause="the handler forgets the owner check"), "probable_cause"),
    (dict(verification_status="probable"), "impact"),
    (dict(severity="CRITICAL"), "severity"),
])
def test_validation_rules(ws, over, needle):
    assert any(needle in m for m in findings.validate_new(state.load(ws), base(**over)))


def test_default_criticality_is_recorded(ws, actor):
    e, _ = findings.add(ws, actor, base(component={"path": "src/x.py"}))
    assert e["data"]["component"] == {"path": "src/x.py", "criticality": "normal", "criticality_source": "default"}


def test_tool_provisional_keeps_the_tool_severity(ws):
    f = base(severity="CRITICAL", severity_rationale={"anchor": "TOOL-PROVISIONAL", "modifiers": []},
             source={"skill": "s", "method": "static", "tool": "scanner"})
    assert not [m for m in findings.validate_new(state.load(ws), f) if "severity" in m]


def test_same_fingerprint_updates_instead_of_duplicating(ws, actor):
    findings.add(ws, actor, base())
    e, created = findings.add(ws, actor, base(title="same issue, new wording"))
    assert not created and e["type"] == "finding.updated" and len(state.load(ws)["findings"]) == 1


def test_transitions(ws, actor):
    findings.add(ws, actor, base())
    with pytest.raises(QaError) as e:
        findings.change_state(ws, actor, "FND-001", "fix_submitted", reason="patched")
    assert e.value.code == "set_by_fix_and_revalidation"
    with pytest.raises(QaError):
        findings.change_state(ws, actor, "FND-001", "accepted_risk", reason="later")
    findings.change_state(ws, actor, "FND-001", "accepted_risk", reason="low traffic", accepted_by="user", expires_at=LATER)
    assert state.load(ws)["findings"]["FND-001"]["state"] == "accepted_risk"


def test_wont_fix_duplicate_and_back_to_open(ws, actor):
    findings.add(ws, actor, base())
    findings.add(ws, actor, base(component={"path": "app/other.py", "criticality": "normal"}))

    def refused(code, new, **kw):
        with pytest.raises(QaError) as e:
            findings.change_state(ws, actor, "FND-001", new, **kw)
        assert e.value.code == code
    refused("missing_field", "wont_fix", reason="")
    findings.change_state(ws, actor, "FND-001", "wont_fix", reason="endpoint removed next release")
    refused("invalid_transition", "duplicate", reason="same", duplicate_of="FND-002")   # wont_fix → duplicate
    findings.change_state(ws, actor, "FND-001", "open", reason="still served")
    refused("missing_field", "duplicate", reason="same root cause")
    findings.change_state(ws, actor, "FND-001", "duplicate", reason="same root cause", duplicate_of="FND-002")
    findings.change_state(ws, actor, "FND-001", "open", reason="different root cause")
    findings.change_state(ws, actor, "FND-001", "accepted_risk", reason="low traffic", accepted_by="user", expires_at=LATER)
    findings.change_state(ws, actor, "FND-001", "open", reason="traffic grew")
    refused("invalid_transition", "open", reason="again")   # open → open
    assert state.load(ws)["findings"]["FND-001"]["state"] == "open"


def test_update_revalidates(ws, actor):
    findings.add(ws, actor, base())
    assert findings.update(ws, actor, "FND-001", {"title": "IDOR on GET /orders/{id}"})["type"] == "finding.updated"
    assert state.load(ws)["findings"]["FND-001"]["title"] == "IDOR on GET /orders/{id}"
    with pytest.raises(QaError) as e:
        findings.update(ws, actor, "FND-001", {"verification_status": "confirmed"})   # no cited execution, no impact
    assert e.value.code == "finding_invalid"


def test_sarif_alerts_become_suspected_findings_once(trusted, actor, py):
    req = execution.RunRequest(command=py.argv(WRITE_SARIF, "{evidence}/r.sarif"), reports=(("sarif", "{evidence}/r.sarif"),),
                               activity="static-analysis", category="security")
    r1 = execution.execute(trusted, actor, req)
    r2 = execution.execute(trusted, actor, req)
    f = state.load(trusted)["findings"]["FND-001"]
    assert (r1["status"], r1["findings"], r2["findings"]) == ("failed", {"opened": 1, "updated": 0}, {"opened": 0, "updated": 1})
    assert (f["verification_status"], f["severity"], f["severity_rationale"]["anchor"]) == ("suspected", "HIGH", "TOOL-PROVISIONAL")


# --------------------------------------------------------------------------- T15-R1, R2, R3: the policy files

def _spec_table():
    """{(severity, column): [clause, ...]} read from §10.4's table, independently of the generator."""
    lines = SPEC.read_text(encoding="utf-8").split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith("### 10.4"))
    table = {}
    for line in lines[start + 1:start + 15]:
        if not line.startswith("| **"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        for column, cell in zip(("FUNC", "SEC", "PERF", "A11Y"), cells[1:]):
            if cell != "—":
                table[(cells[0].strip("*"), column)] = [clause.strip() for clause in cell.split(";")]
    return table


@pytest.mark.skipif(not SPEC.is_file(), reason="the design documents are not published")
def test_every_anchor_matches_the_spec_table():
    anchors = findings.load_rubric()["anchors"]
    letter = {"CRITICAL": "C", "HIGH": "H", "MEDIUM": "M", "LOW": "L", "INFORMATIONAL": "I"}
    expected = {"%s-%s%d" % (column, letter[sev], n): (sev, column, clause)
                for (sev, column), clauses in _spec_table().items() for n, clause in enumerate(clauses, 1)}
    assert len(expected) == 27
    assert {k: (v["severity"], v["column"], v["text"]) for k, v in anchors.items() if k != "TOOL-PROVISIONAL"} == expected
    assert anchors["TOOL-PROVISIONAL"]["severity"] is None and anchors["TOOL-PROVISIONAL"]["column"] is None


def test_rubric_shape_and_columns_cover_every_category():
    rubric = findings.load_rubric()
    categories = schema.load_schema("defs.schema.json")["$defs"]["category"]["enum"]
    assert set(rubric) == {"version", "anchors", "modifiers", "columns"} and rubric["version"] == 1
    assert set(rubric["columns"]) == set(categories)
    assert {c: rubric["columns"][c] for c in ("functional", "data-integrity", "security", "performance", "reliability", "accessibility")} == {
        "functional": "FUNC", "data-integrity": "FUNC", "security": "SEC", "performance": "PERF", "reliability": "PERF", "accessibility": "A11Y"}
    assert {rubric["columns"][c] for c in categories if c not in ("security", "performance", "reliability", "accessibility")} == {"FUNC"}
    assert rubric["modifiers"] == {"unreachable": -1, "dev_only": -1, "sensitive_data": 1}


def test_matrix_shape():
    m = findings.load_matrix()
    assert set(m) == {"version", "criticalities", "rows", "adjustments"}
    assert m["criticalities"] == ["critical", "high", "normal", "low"]
    assert m["adjustments"] == {"outside_release_scope": -1, "public_exploit": 1}


@pytest.mark.parametrize("call,code", [
    (lambda r: findings.compute_severity("SEC-Z9", [], r), "unknown_anchor"),
    (lambda r: findings.compute_severity("SEC-H1", ["probably"], r), "unknown_modifier"),
    (lambda r: findings.compute_severity("TOOL-PROVISIONAL", [], r), "tool_provisional"),
    (lambda r: findings.compute_severity("SEC-H1", ["dev_only", "dev_only"], r), "duplicate_modifier"),
])
def test_compute_severity_codes(call, code):
    with pytest.raises(QaError) as e:
        call(findings.load_rubric())
    assert e.value.code == code


@pytest.mark.parametrize("args,code", [
    (("EXTREME", "normal", []), "unknown_severity"),
    (("HIGH", "tiny", []), "unknown_criticality"),
    (("HIGH", "normal", ["luck"]), "unknown_adjustment"),
    (("HIGH", "normal", ["public_exploit", "public_exploit"]), "duplicate_adjustment"),
])
def test_compute_priority_codes(args, code):
    with pytest.raises(QaError) as e:
        findings.compute_priority(*args, findings.load_matrix())
    assert e.value.code == code


def test_priority_adjustments_add_up_and_clamp():
    m = findings.load_matrix()
    both = ["outside_release_scope", "public_exploit"]
    assert findings.compute_priority("MEDIUM", "normal", both, m) == "P2"          # -1 and +1 cancel
    assert findings.compute_priority("CRITICAL", "critical", ["public_exploit"], m) == "P0"
    assert findings.compute_priority("LOW", "normal", ["outside_release_scope"], m) == "P3"


# --------------------------------------------------------------------------- T15-R4: validate_new

def _problems(ws, **over):
    return findings.validate_new(state.load(ws), base(**over))


def _has(problems, *needles):
    return any(all(n in p for n in needles) for p in problems)


@pytest.mark.parametrize("over,needles", [
    (dict(title=""), ("title",)),
    (dict(title="   "), ("title", "blank")),
    (dict(description=" "), ("description", "blank")),
    (dict(category="vibes"), ("category",)),
    (dict(verification_status="maybe"), ("verification_status",)),
    (dict(component={"path": "a.py", "criticality": "huge"}), ("criticality",)),
    (dict(severity_rationale=None), ("severity_rationale",)),
    (dict(severity_rationale={"anchor": "SEC-H1"}), ("severity_rationale",)),
    (dict(severity_rationale={"anchor": "SEC-H1", "modifiers": "dev_only"}), ("severity_rationale",)),
    (dict(severity_rationale={"anchor": "SEC-NOPE", "modifiers": []}), ("severity_rationale", "unknown_anchor")),
    (dict(severity_rationale={"anchor": "SEC-H1", "modifiers": ["who"]}), ("severity_rationale", "unknown_modifier")),
    (dict(evidence="EXE-1"), ("evidence",)),
    (dict(reproduction_steps="run it"), ("reproduction_steps",)),
    (dict(refs={"cwe": ["CWE-639"]}), ("refs.rule",)),
    (dict(refs={"rule": " "}), ("refs.rule",)),
    (dict(refs={"rule": "r", "location": "/etc/app.py"}), ("refs.location", "absolute")),
    (dict(refs={"rule": "r", "location": "C:\\app\\a.py"}), ("refs.location", "absolute")),
    (dict(refs={"rule": "r", "location": "../app.py"}), ("refs.location", "..")),
    (dict(refs={"rule": "r", "location": "app/../../x.py"}), ("refs.location", "..")),
    (dict(refs={"rule": "r", "location": "app/views.py:42"}), ("refs.location", "line number")),
    (dict(refs={"rule": "r", "location": ""}), ("refs.location", "blank")),
    (dict(id="FND-009"), ("id", "kernel")),
    (dict(state="accepted_risk"), ("state", "kernel")),
    (dict(priority="none"), ("priority", "kernel")),
    (dict(fingerprint="sha256:" + "0" * 64), ("fingerprint", "kernel")),
    (dict(owner="me"), ("owner",)),
    (dict(evidence=["EXE-" + "0" * 26]), ("EXE-", "resolves to no")),
    (dict(evidence=["EV-0123456789ab"]), ("EV-0123456789ab", "resolves to no")),
    (dict(verification_status="probable", impact="x"), ("recommendation",)),
    (dict(verification_status="probable", recommendation="x"), ("impact",)),
    (dict(severity="LOW"), ("severity", "HIGH")),
    (dict(severity_rationale={"anchor": "FUNC-H1", "modifiers": []}), ("severity_rationale.anchor", "FUNC", "SEC")),
    (dict(category="functional"), ("severity_rationale.anchor", "SEC", "FUNC")),
    (dict(category="data-integrity", severity_rationale={"anchor": "PERF-C1", "modifiers": []}), ("PERF", "FUNC")),
    (dict(severity_rationale={"anchor": "TOOL-PROVISIONAL", "modifiers": []}), ("severity",)),
    (dict(severity="SEVERE", severity_rationale={"anchor": "TOOL-PROVISIONAL", "modifiers": []}), ("severity",)),
    (dict(key_attributes=[]), ("key_attributes",)),
    (dict(source={"skill": "x", "method": "psychic"}), ("method",)),
])
def test_validate_new_reports(ws, over, needles):
    assert _has(_problems(ws, **over), *needles)


def test_validate_new_accepts_the_valid_cases(ws):
    for over in (dict(), dict(severity="HIGH"), dict(probable_cause="app/orders/views.py:42 loads it without a filter"),
                 dict(verification_status="probable", impact="i", recommendation="r"),
                 dict(refs={"rule": "r", "location": "app/orders/views.py::OrderView.get"}),
                 dict(component={"endpoint": "GET /x"}), dict(refs={"rule": "r", "wcag": ["1.4.3"]}),
                 dict(category="accessibility", severity_rationale={"anchor": "A11Y-M1", "modifiers": []})):
        assert findings.validate_new(state.load(ws), base(**over)) == [], over


@pytest.mark.parametrize("cause", ["see the views", "version 3 of it", "EV-short", "line:abc"])
def test_probable_cause_needs_a_citation(ws, cause):
    assert _has(_problems(ws, probable_cause=cause), "probable_cause")


def test_probable_cause_accepts_each_citation(ws, trusted, actor, py):
    r = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))
    for cause in ("fails at src/app.py:7", "Makefile:10 has it", "see " + r["execution"], "see EV-0123456789ab"):
        assert not _has(findings.validate_new(state.load(trusted), base(probable_cause=cause)), "probable_cause"), cause


def test_validate_new_never_raises_or_changes_its_input(ws):
    st = state.load(ws)
    for junk in (None, 5, "finding", [], {}, {"component": 5, "refs": 5, "evidence": 5, "severity_rationale": 5, "category": []},
                 base(severity_rationale={"anchor": [], "modifiers": [[]]}), base(refs={"rule": 5, "location": 5}),
                 base(evidence=[5, None, {"a": 1}]), base(component=[]), base(title=5, description=None)):
        before = copy.deepcopy(junk)
        assert findings.validate_new(st, junk)
        assert junk == before


def test_add_refuses_with_every_message(ws, actor):
    with pytest.raises(QaError) as e:
        findings.add(ws, actor, base(priority="none", owner="x", refs={"rule": "r", "location": "/abs.py"}))
    assert e.value.code == "finding_invalid" and len(e.value.details["errors"]) == 3
    assert state.load(ws)["findings"] == {}


# --------------------------------------------------------------------------- T15-R14: what confirms a finding

def _confirmed(evidence):
    return base(verification_status="confirmed", impact="i", recommendation="r", evidence=evidence)


def test_a_structured_execution_confirms_by_exe_and_by_ev(trusted, actor, py):
    r = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))
    st = state.load(trusted)
    ev = next(iter(st["executions"][r["execution"]]["evidence_manifest"]["ids"].values()))
    assert r["strength"] == "structured"
    for cited in (r["execution"], ev):
        assert findings.validate_new(st, _confirmed([cited])) == []


def test_a_claimed_or_blocked_execution_does_not_confirm(trusted, actor, py):
    blocked = execution.execute(trusted, actor, execution.RunRequest(command=py.argv("pass"), activity="deploy"))
    assert blocked["strength"] is None
    assert _has(findings.validate_new(state.load(trusted), _confirmed([blocked["execution"]])), "confirmed")


def test_an_attested_ingest_does_not_confirm_even_when_its_execution_is_structured(trusted, actor, py, tmp_path):
    r = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))
    foreign = tmp_path / "foreign.xml"
    foreign.write_text('<testsuite><testcase classname="c" name="other"/></testsuite>')
    out = execution.ingest_file(trusted, actor, "junit", foreign, from_execution=r["execution"])
    assert out["strength"] == "attested"
    ev = state.load(trusted)["executions"][r["execution"]]["ingested"][0]["ev_id"]
    problems = findings.validate_new(state.load(trusted), _confirmed([ev]))
    assert _has(problems, "confirmed") and not _has(problems, "resolves to no")


# --------------------------------------------------------------------------- T15-R5, R6, R15: the record and its fingerprint

A3_KEYS = {"id", "title", "description", "category", "severity", "severity_rationale", "priority", "component", "evidence",
           "reproduction_steps", "impact", "probable_cause", "recommendation", "verification_status", "state", "refs", "links",
           "source", "fingerprint", "commit", "key_attributes"}


def test_the_record_has_every_a3_key(ws, actor):
    e, _ = findings.add(ws, actor, base())
    d = e["data"]
    assert set(d) == A3_KEYS
    assert (d["state"], d["commit"], d["links"], d["key_attributes"], d["impact"]) == ("open", None, {}, {}, None)
    assert d["component"] == {"path": "app/orders/views.py", "endpoint": "GET /orders/{id}", "criticality": "critical"}
    assert d["source"] == {"skill": actor.skill, "method": "manual"}
    assert state.load(ws)["findings"]["FND-001"]["last_seen"] == e["ts"]


def test_the_fingerprint_is_recomputable_from_the_record(ws, actor):
    e, _ = findings.add(ws, actor, base(key_attributes={"param": "id"}))
    d = e["data"]
    assert d["key_attributes"] == {"param": "id"}
    assert d["fingerprint"] == canonical.hash_obj(["security", "manual", "app/orders/views.py", "app/orders/views.py", {"param": "id"}])
    assert d["fingerprint"] == findings.fingerprint("security", "manual", "app/orders/views.py", "app/orders/views.py", {"param": "id"})


def test_the_fingerprint_has_no_separator_collisions():
    assert findings.fingerprint("a", "b|c", "d", "e", {}) != findings.fingerprint("a", "b", "c|d", "e", {})
    assert findings.fingerprint("a", "b", "", "", {}) != findings.fingerprint("a", "b", "", "x", {})


def test_a_finding_without_a_path_uses_its_endpoint(ws, actor):
    e, _ = findings.add(ws, actor, base(component={"endpoint": "GET /x"}, refs={"rule": "manual"}))
    assert e["data"]["fingerprint"] == findings.fingerprint("security", "manual", "GET /x", "", {})


def test_a_declared_criticality_has_no_source_and_a_priority_follows_it(ws, actor):
    e, _ = findings.add(ws, actor, base(component={"path": "a.py", "criticality": "low"}))
    assert e["data"]["component"] == {"path": "a.py", "criticality": "low"} and e["data"]["priority"] == "P2"


def test_modifiers_change_the_computed_severity_and_priority(ws, actor):
    e, _ = findings.add(ws, actor, base(severity_rationale={"anchor": "SEC-H1", "modifiers": ["dev_only"]}))
    assert (e["data"]["severity"], e["data"]["priority"]) == ("MEDIUM", "P1")


def test_the_fingerprint_separates_what_it_names(ws, actor):
    for over in (dict(category="functional", severity_rationale={"anchor": "FUNC-H1", "modifiers": []}),
                 dict(refs={"rule": "other", "location": "app/orders/views.py"}), dict(key_attributes={"x": 1}),
                 dict(component={"path": "app/y.py", "criticality": "critical"}),
                 dict(refs={"rule": "manual", "location": "app/orders/views.py::get"})):
        findings.add(ws, actor, base())
        assert findings.add(ws, actor, base(**over))[1], over


# --------------------------------------------------------------------------- T15-R7: dedupe

def test_a_rescan_of_an_accepted_risk_updates_it_and_leaves_its_state(ws, actor):
    findings.add(ws, actor, base())
    findings.change_state(ws, actor, "FND-001", "accepted_risk", reason="low traffic", accepted_by="user", expires_at=LATER)
    e, created = findings.add(ws, actor, base(source={"skill": "qa-security", "method": "dynamic", "tool": "hurl"}))
    f = state.load(ws)["findings"]["FND-001"]
    assert not created and e["type"] == "finding.updated" and set(e["data"]) == {"id", "last_seen", "source"}
    assert (f["state"], f["expires_at"], f["source"]["tool"], len(state.load(ws)["findings"])) == ("accepted_risk", LATER, "hurl", 1)
    assert f["last_seen"] == e["data"]["last_seen"]


def test_a_wont_fix_finding_is_updated_too(ws, actor):
    findings.add(ws, actor, base())
    findings.change_state(ws, actor, "FND-001", "wont_fix", reason="removed next release")
    assert findings.add(ws, actor, base())[1] is False
    assert state.load(ws)["findings"]["FND-001"]["state"] == "wont_fix"


def test_a_verified_fixed_finding_does_not_absorb_a_new_one(ws, actor):
    findings.add(ws, actor, base())
    ledger.append(ws, actor, "finding.state_changed",
                  {"id": "FND-001", "from": "open", "to": "verified_fixed", "reason": "x", "accepted_by": None, "expires_at": None, "duplicate_of": None})
    # the fold refuses that step, so the finding stays open and still absorbs; the rule is exercised with the state itself
    st = state.load(ws)
    assert st["findings"]["FND-001"]["state"] == "open"
    st["findings"]["FND-001"]["state"] = "verified_fixed"
    e, created = findings._add(ws, actor, base(), st, lambda: "FND-002")
    assert created and e["data"]["id"] == "FND-002"


# --------------------------------------------------------------------------- T15-R9: update

def _updated(ws):
    return state.load(ws)["findings"]["FND-001"]


def test_update_of_an_unknown_finding(ws, actor):
    with pytest.raises(QaError) as e:
        findings.update(ws, actor, "FND-404", {"title": "x"})
    assert e.value.code == "unknown_finding"


@pytest.mark.parametrize("changes,named", [
    ({"id": "FND-009"}, "id"), ({"state": "wont_fix"}, "state"), ({"priority": "P3"}, "priority"),
    ({"fingerprint": "sha256:" + "1" * 64}, "fingerprint"), ({"category": "functional"}, "category"),
    ({"key_attributes": {"a": 1}}, "key_attributes"), ({"refs": {"rule": "other"}}, "refs.rule"),
    ({"refs": {"location": "b.py"}}, "refs.location"), ({"component": {"path": "b.py"}}, "component.path"),
    ({"component": {"endpoint": "GET /y"}}, "component.endpoint"), ({"last_seen": LATER}, "last_seen"),
])
def test_update_refuses_the_findings_identity(ws, actor, changes, named):
    findings.add(ws, actor, base())
    before = _updated(ws)
    with pytest.raises(QaError) as e:
        findings.update(ws, actor, "FND-001", changes)
    assert e.value.code == "finding_invalid" and any(named in m for m in e.value.details["errors"])
    assert _updated(ws) == before


def test_update_refuses_what_validation_refuses(ws, actor):
    findings.add(ws, actor, base())
    for changes in ({"owner": "me"}, {"title": " "}, {"severity": "LOW"}, {"evidence": ["EXE-" + "0" * 26]},
                    {"severity_rationale": {"anchor": "NOPE", "modifiers": []}}, {"component": {"criticality": "huge"}}, 5):
        with pytest.raises(QaError) as e:
            findings.update(ws, actor, "FND-001", changes)
        assert e.value.code == "finding_invalid", changes


def test_update_recomputes_severity_from_the_rationale(ws, actor):
    findings.add(ws, actor, base())
    e = findings.update(ws, actor, "FND-001", {"severity_rationale": {"anchor": "SEC-H1", "modifiers": ["dev_only"]}})
    assert e["data"]["changes"] == {"severity_rationale": {"anchor": "SEC-H1", "modifiers": ["dev_only"]},
                                    "severity": "MEDIUM", "priority": "P1"}
    assert (_updated(ws)["severity"], _updated(ws)["priority"]) == ("MEDIUM", "P1")


def test_update_refuses_a_declared_severity_the_rubric_does_not_give(ws, actor):
    findings.add(ws, actor, base())
    with pytest.raises(QaError) as e:
        findings.update(ws, actor, "FND-001", {"severity_rationale": {"anchor": "SEC-H1", "modifiers": ["dev_only"]}, "severity": "HIGH"})
    assert e.value.code == "finding_invalid"


def test_update_cannot_move_a_finding_into_tool_provisional(ws, actor):
    findings.add(ws, actor, base(severity_rationale={"anchor": "SEC-C1", "modifiers": []}))
    for changes in ({"severity_rationale": {"anchor": "TOOL-PROVISIONAL", "modifiers": []}, "severity": "INFORMATIONAL"},
                    {"severity_rationale": {"anchor": "TOOL-PROVISIONAL", "modifiers": []},
                     "source": {"skill": "s", "method": "static", "tool": "t"}}):
        with pytest.raises(QaError) as e:
            findings.update(ws, actor, "FND-001", changes)
        assert e.value.code == "finding_invalid"
    assert (_updated(ws)["severity"], _updated(ws)["priority"]) == ("CRITICAL", "P0")


def _alert_finding(ws, actor, run_id):
    findings.ingest_alerts(ws, actor, _summary(_alert(security_severity=9.1)), execution_id=run_id, category="security")


def test_update_cannot_change_a_provisional_severity_by_hand(trusted, actor, run_id):
    _alert_finding(trusted, actor, run_id)
    with pytest.raises(QaError) as e:
        findings.update(trusted, actor, "FND-001", {"severity": "INFORMATIONAL"})
    assert e.value.code == "finding_invalid" and _updated(trusted)["severity"] == "CRITICAL"
    findings.update(trusted, actor, "FND-001", {"severity": "CRITICAL", "title": "same severity is fine"})


def test_triage_moves_out_of_tool_provisional_and_may_change_the_status(trusted, actor, run_id):
    _alert_finding(trusted, actor, run_id)
    findings.update(trusted, actor, "FND-001", {"severity_rationale": {"anchor": "SEC-M1", "modifiers": []},
                                                "verification_status": "probable", "impact": "i", "recommendation": "r"})
    assert (_updated(trusted)["severity"], _updated(trusted)["verification_status"]) == ("MEDIUM", "probable")


@pytest.mark.parametrize("over,needle", [
    (dict(source={"skill": "s", "method": "manual"}), "source.tool"),
    (dict(source={"skill": "s", "method": "static", "tool": " "}), "source.tool"),
    (dict(severity_rationale={"anchor": "TOOL-PROVISIONAL", "modifiers": ["dev_only"]}), "modifiers"),
    (dict(verification_status="probable", impact="i", recommendation="r"), "verification_status"),
])
def test_tool_provisional_is_only_for_untriaged_alerts(ws, over, needle):
    f = base(severity="INFORMATIONAL", severity_rationale={"anchor": "TOOL-PROVISIONAL", "modifiers": []},
             source={"skill": "s", "method": "static", "tool": "scanner"})
    f.update(over)
    problems = findings.validate_new(state.load(ws), f)
    assert any(needle in m for m in problems)
    assert not any("severity" in m for m in problems if needle == "source.tool")


def test_update_recomputes_priority_when_criticality_changes(ws, actor):
    opened, _ = findings.add(ws, actor, base())
    e = findings.update(ws, actor, "FND-001", {"component": {"criticality": "low"}})
    assert e["data"]["changes"] == {"component": {"path": "app/orders/views.py", "endpoint": "GET /orders/{id}", "criticality": "low"},
                                    "priority": "P2"}
    assert _updated(ws)["priority"] == "P2" and _updated(ws)["fingerprint"] == opened["data"]["fingerprint"]


def test_update_of_a_defaulted_criticality_drops_its_source(ws, actor):
    findings.add(ws, actor, base(component={"path": "a.py"}))
    findings.update(ws, actor, "FND-001", {"component": {"criticality": "high"}})
    assert _updated(ws)["component"] == {"path": "a.py", "criticality": "high"} and _updated(ws)["priority"] == "P1"


def test_update_of_other_fields_keeps_the_rest(ws, actor):
    findings.add(ws, actor, base(component={"path": "a.py"}))
    e = findings.update(ws, actor, "FND-001", {"impact": "reads other users' orders", "refs": {"cwe": ["CWE-285"]}})
    assert set(e["data"]["changes"]) == {"impact", "refs"}
    f = _updated(ws)
    assert f["refs"] == {"cwe": ["CWE-285"], "owasp_api_2023": ["API1:2023"], "rule": "manual", "location": "app/orders/views.py"}
    assert f["component"]["criticality_source"] == "default" and f["title"] == base()["title"]


def test_update_can_confirm_with_evidence(trusted, actor, py):
    r = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))
    findings.add(trusted, actor, base())
    findings.update(trusted, actor, "FND-001", {"verification_status": "confirmed", "impact": "i", "recommendation": "r", "evidence": [r["execution"]]})
    assert state.load(trusted)["findings"]["FND-001"]["verification_status"] == "confirmed"


# --------------------------------------------------------------------------- T15-R10: change_state

def _refused(ws, actor, code, fid, new, **kw):
    with pytest.raises(QaError) as e:
        findings.change_state(ws, actor, fid, new, **kw)
    assert e.value.code == code


@pytest.mark.parametrize("new", ["fix_submitted", "verified_fixed", "reopened"])
def test_fix_lifecycle_states_come_before_every_other_check(ws, actor, new):
    _refused(ws, actor, "set_by_fix_and_revalidation", "FND-404", new, reason="")


def test_state_change_checks(ws, actor):
    findings.add(ws, actor, base())
    _refused(ws, actor, "unknown_finding", "FND-404", "wont_fix", reason="x")
    _refused(ws, actor, "invalid_transition", "FND-001", "nonsense", reason="x")
    _refused(ws, actor, "missing_field", "FND-001", "wont_fix", reason="  ")
    _refused(ws, actor, "missing_field", "FND-001", "wont_fix", reason=None)
    _refused(ws, actor, "missing_field", "FND-001", "accepted_risk", reason="r", expires_at=LATER)
    _refused(ws, actor, "missing_field", "FND-001", "accepted_risk", reason="r", accepted_by=" ", expires_at=LATER)
    _refused(ws, actor, "missing_field", "FND-001", "accepted_risk", reason="r", accepted_by="me")
    _refused(ws, actor, "missing_field", "FND-001", "accepted_risk", reason="r", accepted_by="me", expires_at="tomorrow")
    _refused(ws, actor, "missing_field", "FND-001", "accepted_risk", reason="", accepted_by="me", expires_at=LATER)
    _refused(ws, actor, "unknown_finding", "FND-001", "duplicate", reason="r", duplicate_of="FND-404")
    _refused(ws, actor, "unknown_finding", "FND-001", "duplicate", reason="r", duplicate_of="FND-001")
    assert _updated(ws)["state"] == "open"


def test_every_transition_needs_a_reason(ws, actor):
    findings.add(ws, actor, base())
    findings.change_state(ws, actor, "FND-001", "wont_fix", reason="r")
    _refused(ws, actor, "missing_field", "FND-001", "open", reason="")


def test_an_acceptance_may_already_have_expired(ws, actor):
    findings.add(ws, actor, base())
    findings.change_state(ws, actor, "FND-001", "accepted_risk", reason="r", accepted_by="me", expires_at="2020-01-01T00:00:00.000Z")
    assert _updated(ws)["expires_at"] == "2020-01-01T00:00:00.000Z"


def test_state_changed_event_data(ws, actor):
    findings.add(ws, actor, base())
    findings.add(ws, actor, base(component={"path": "app/other.py"}))
    e = findings.change_state(ws, actor, "FND-001", "wont_fix", reason="r", accepted_by="ignored", duplicate_of="FND-002")
    assert e["type"] == "finding.state_changed"
    assert e["data"] == {"id": "FND-001", "from": "open", "to": "wont_fix", "reason": "r", "accepted_by": None, "expires_at": None, "duplicate_of": None}
    findings.change_state(ws, actor, "FND-001", "open", reason="again")
    e = findings.change_state(ws, actor, "FND-001", "duplicate", reason="same", duplicate_of="FND-002", expires_at=LATER)
    assert (e["data"]["duplicate_of"], e["data"]["expires_at"], e["data"]["accepted_by"]) == ("FND-002", None, None)
    f = _updated(ws)
    assert (f["state"], f["duplicate_of"], f["state_reason"]) == ("duplicate", "FND-002", "same")
    e = findings.change_state(ws, actor, "FND-001", "open", reason="different")
    f = _updated(ws)
    assert (f["state"], f["duplicate_of"], f["expires_at"], f["accepted_by"]) == ("open", None, None, None)


def test_transitions_table_lives_in_vocab():
    assert findings.TRANSITIONS is vocab.TRANSITIONS
    assert set(vocab.TRANSITIONS["open"]) == {"accepted_risk", "wont_fix", "duplicate"}
    assert all(vocab.TRANSITIONS[s] == {"open": ("reason",)} for s in ("accepted_risk", "wont_fix", "duplicate"))
    assert not any(set(vocab.KERNEL_STATES) & set(steps) for steps in vocab.TRANSITIONS.values())  # no hand path into a kernel state
    assert set(vocab.TRANSITIONS["fix_submitted"]) == set(vocab.TRANSITIONS["reopened"]) == {"accepted_risk", "wont_fix"}


# --------------------------------------------------------------------------- T15-R11, R12: events and the fold

def _event(type_, data, ts, n=1):
    return {"id": "EVT-%02d" % n, "type": type_, "ts": ts, "data": data, "actor": {"role": "verifier", "skill": None, "session": "S-" + "0" * 26},
            "seq": n}


def _opened(**over):
    d = canonical.loads(canonical.dumps(dict(base(), id="FND-001", state="open", priority="P0", severity="HIGH", fingerprint="sha256:" + "a" * 64,
                                             impact=None, probable_cause=None, recommendation=None, links={}, source={"skill": None, "method": "manual"},
                                             commit=None, key_attributes={})))
    d["component"] = {"path": "a.py", "criticality": "critical"}
    d.update(over)
    return d


TS1, TS2, TS3 = "2026-01-01T00:00:01.000Z", "2026-01-01T00:00:02.000Z", "2026-01-01T00:00:03.000Z"


def _changed(to, ts, n, **over):
    d = {"id": "FND-001", "from": "open", "to": to, "reason": "r", "accepted_by": None, "expires_at": None, "duplicate_of": None}
    d.update(over)
    return _event("finding.state_changed", d, ts, n)


def test_opened_stores_the_data_and_last_seen():
    st = state.fold([_event("finding.opened", _opened(), TS1)])
    assert st["findings"]["FND-001"] == dict(_opened(), last_seen=TS1) and st["violations"] == []


def test_an_update_folds_after_every_finding_whatever_the_order():
    events = [_event("finding.updated", {"id": "FND-001", "changes": {"title": "later title"}}, TS1, 1),
              _event("finding.opened", _opened(), TS2, 2)]
    st = state.fold(events)
    assert st["findings"]["FND-001"]["title"] == "later title" and st["violations"] == []


def test_a_sighting_sets_last_seen_and_source():
    src = {"skill": "s", "method": "static", "tool": "t"}
    st = state.fold([_event("finding.updated", {"id": "FND-001", "last_seen": TS3, "source": src}, TS1, 1),
                     _event("finding.opened", _opened(), TS2, 2)])
    assert (st["findings"]["FND-001"]["last_seen"], st["findings"]["FND-001"]["source"]) == (TS3, src)


def test_a_state_change_folds_after_the_opening_whatever_the_order():
    st = state.fold([_changed("wont_fix", TS1, 1), _event("finding.opened", _opened(), TS2, 2)])
    assert st["findings"]["FND-001"]["state"] == "wont_fix" and st["violations"] == []


def test_reducer_violations():
    opened = _event("finding.opened", _opened(), TS1, 1)
    again = _event("finding.opened", _opened(title="second"), TS2, 2)
    st = state.fold([opened, again])
    assert st["violations"] == [{"event": "EVT-02", "kind": "duplicate_id"}] and st["findings"]["FND-001"]["title"] == base()["title"]
    st = state.fold([_event("finding.updated", {"id": "FND-009", "changes": {"title": "x"}}, TS1, 1),
                     _changed("wont_fix", TS1, 2, id="FND-008")])
    assert st["violations"] == [{"event": "EVT-01", "kind": "unknown_finding"}, {"event": "EVT-02", "kind": "unknown_finding"}]
    for to in ("verified_fixed", "fix_submitted", "reopened", "open"):  # none is a step from open
        st = state.fold([opened, _changed(to, TS2, 2)])
        assert st["violations"] == [{"event": "EVT-02", "kind": "invalid_transition"}] and st["findings"]["FND-001"]["state"] == "open"


def test_the_fold_checks_the_current_state_not_the_events_from():
    st = state.fold([_event("finding.opened", _opened(), TS1, 1), _changed("wont_fix", TS2, 2),
                     _changed("duplicate", TS3, 3, **{"from": "open", "duplicate_of": "FND-002"})])
    assert st["findings"]["FND-001"]["state"] == "wont_fix"
    assert st["violations"] == [{"event": "EVT-03", "kind": "invalid_transition"}]


def test_event_data_must_fit_its_schema():
    for type_, data in (("finding.opened", dict(_opened(), extra=1)), ("finding.opened", dict(_opened(), state="bogus")),
                        ("finding.updated", {"id": "FND-001"}), ("finding.updated", {"id": "FND-001", "changes": {"state": "open"}}),
                        ("finding.updated", {"id": "FND-001", "changes": {"fingerprint": "sha256:" + "1" * 64}}),
                        ("finding.updated", {"id": "FND-001", "last_seen": TS1, "source": {"skill": None, "method": "manual"}, "changes": {}}),
                        ("finding.state_changed", {"id": "FND-001", "to": "open"})):
        assert state.fold([_event(type_, data, TS1)])["violations"] == [{"event": "EVT-01", "kind": "invalid_data"}], (type_, data)


def test_hand_written_event_cannot_skip_the_transition_table(ws, actor):
    findings.add(ws, actor, base())
    ledger.append(ws, actor, "finding.state_changed",
                  {"id": "FND-001", "from": "open", "to": "verified_fixed", "reason": "x", "accepted_by": None, "expires_at": None, "duplicate_of": None})
    st = state.load(ws)
    assert st["findings"]["FND-001"]["state"] == "open" and st["violations"][-1]["kind"] == "invalid_transition"


def test_known_references_is_public_and_reclassify_uses_it(trusted, actor, py):
    r = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "fail"))
    st = state.load(trusted)
    assert r["execution"] in state.known_references(st)
    assert not hasattr(execution, "_known_references")


# --------------------------------------------------------------------------- T15-R8: alerts

def _alert(**over):
    a = dict(tool="scanner", rule_id="R1", level="error", security_severity=None, message="m", path="a.py", start_line=3,
             partial_fingerprints={}, tags=())
    a.update(over)
    return ingest.Alert(**a)


def _summary(*alerts):
    return ingest.ReportSummary(format="sarif", kind="alerts", cases=(), alerts=tuple(alerts), runs=1, anomalies=())


@pytest.fixture
def run_id(trusted, actor, py):
    return execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))["execution"]


def _ingest(ws, actor, run_id, *alerts, category="security"):
    out = findings.ingest_alerts(ws, actor, _summary(*alerts), execution_id=run_id, category=category)
    return out, state.load(ws)["findings"]


@pytest.mark.parametrize("alert,want", [
    (dict(security_severity=9.0), "CRITICAL"), (dict(security_severity=9.8), "CRITICAL"), (dict(security_severity=8.9), "HIGH"),
    (dict(security_severity=7.0), "HIGH"), (dict(security_severity=6.9), "MEDIUM"), (dict(security_severity=4.0), "MEDIUM"),
    (dict(security_severity=3.9), "LOW"), (dict(security_severity=0.1), "LOW"), (dict(security_severity=0), "INFORMATIONAL"),
    (dict(security_severity=9.5, level="note"), "CRITICAL"),
    (dict(level="error"), "HIGH"), (dict(level="warning"), "MEDIUM"), (dict(level="note"), "LOW"), (dict(level="none"), "INFORMATIONAL"),
    (dict(level="fatal"), "HIGH"), (dict(level=""), "HIGH"), (dict(level="ERROR"), "HIGH"),
    (dict(security_severity=-1.0, level="note"), "LOW"), (dict(security_severity=float("nan"), level="warning"), "MEDIUM"),
])
def test_alert_severity(trusted, actor, run_id, alert, want):
    _, found = _ingest(trusted, actor, run_id, _alert(**alert))
    assert found["FND-001"]["severity"] == want and found["FND-001"]["severity_rationale"] == {"anchor": "TOOL-PROVISIONAL", "modifiers": []}


def test_alert_finding_record(trusted, actor, run_id):
    out, found = _ingest(trusted, actor, run_id, _alert(security_severity=7.5, message="SQL injection\nsecond line", tags=("security",)))
    f = found["FND-001"]
    assert out == {"opened": 1, "updated": 0}
    assert (f["title"], f["description"]) == ("SQL injection", "SQL injection\nsecond line")
    assert (f["category"], f["verification_status"], f["state"], f["evidence"]) == ("security", "suspected", "open", [run_id])
    assert f["refs"] == {"rule": "R1", "location": "a.py"} and f["component"] == {"path": "a.py", "criticality": "normal", "criticality_source": "default"}
    assert f["source"] == {"skill": actor.skill, "method": "static", "tool": "scanner", "level": "error", "security_severity": 7.5, "start_line": 3}
    assert (f["priority"], f["commit"], f["reproduction_steps"], f["impact"]) == ("P1", None, [], None)
    assert f["fingerprint"] == findings.fingerprint("security", "R1", "a.py", "a.py", {})


def test_alert_category_sets_the_finding_category(trusted, actor, run_id):
    _, found = _ingest(trusted, actor, run_id, _alert(), category="maintainability")
    assert found["FND-001"]["category"] == "maintainability"


def test_alert_title_and_rule_fallbacks(trusted, actor, run_id):
    _, found = _ingest(trusted, actor, run_id, _alert(message="x" * 300, rule_id="A"), _alert(message="", rule_id="B"),
                       _alert(message="  \n ", rule_id=""), _alert(message="\nsecond", rule_id="D"))
    titles = [(f["title"], f["description"][:5], f["refs"]["rule"]) for f in found.values()]
    assert titles == [("x" * 120, "xxxxx", "A"), ("B", "B", "B"), ("unknown", "unkno", "unknown"), ("second", "secon", "D")]


def test_alert_commit_comes_from_the_execution(trusted, actor, run_id):
    st = state.load(trusted)
    st_git = st["executions"][run_id]["git"]
    _, found = _ingest(trusted, actor, run_id, _alert())
    assert found["FND-001"]["commit"] == (st_git["commit"] if st_git else None)


@pytest.mark.parametrize("raw,path,uri", [
    ("a.py", "a.py", None), ("src/app/a.py", "src/app/a.py", None), ("./src//a.py", "src/a.py", None),
    ("src/./a.py", "src/a.py", None), ("src/b/../a.py", None, "src/b/../a.py"), ("../a.py", None, "../a.py"),
    ("src\\a.py", None, "src\\a.py"), ("C:\\src\\a.py", None, "C:\\src\\a.py"), ("C:/src/a.py", None, "C:/src/a.py"),
    ("https://example.com/a.py", None, "https://example.com/a.py"), ("file://server/share/a.py", None, "file://server/share/a.py"),
    ("a.py:12", None, "a.py:12"), (".", None, "."), ("/", None, "/"),
    ("/outside/root/a.py", None, "/outside/root/a.py"), ("file:///outside/root/a.py", None, "file:///outside/root/a.py"),
    (None, None, None), ("", None, None), ("   ", None, None),
    ("https://[oops/a.py", None, "https://[oops/a.py"), ("file://[oops/a.py", None, "file://[oops/a.py"),
])
def test_alert_path_forms(trusted, actor, run_id, raw, path, uri):
    _, found = _ingest(trusted, actor, run_id, _alert(path=raw))
    f = found["FND-001"]
    assert f["component"].get("path") == path and f["refs"].get("location") == path and f["source"].get("uri") == uri
    assert ("path" in f["component"]) == (path is not None)


def test_alert_with_a_blank_rule_is_unknown(trusted, actor, run_id):
    _, found = _ingest(trusted, actor, run_id, _alert(rule_id="   "))
    assert found["FND-001"]["refs"]["rule"] == "unknown"


def _sarif_run(py, results, driver="{'name': 's'}", **kw):
    code = ("import json, sys, os; json.dump({'version': '2.1.0', 'runs': [{'tool': {'driver': " + driver + "}, 'results': "
            + results + "}]}, open(sys.argv[1], 'w')); sys.exit(1)")
    return execution.RunRequest(command=py.argv(code, "{evidence}/r.sarif"), reports=(("sarif", "{evidence}/r.sarif"),),
                                activity="static-analysis", category="security", **kw)


def test_execute_survives_alerts_it_cannot_place(trusted, actor, py):
    results = ("[{'ruleId': 'R%d' % n, 'level': 'error', 'message': {'text': 'm'}, 'locations': [{'physicalLocation': "
               "{'artifactLocation': {'uri': u}}}]} for n, u in enumerate(['a.py', 'https:' + '//[oops/b.py', '   ', 'c.py'])] "
               "+ [{'ruleId': '  ', 'level': 'error', 'message': {'text': 'm'}}]")
    r = execution.execute(trusted, actor, _sarif_run(py, results))
    assert r["status"] == "failed" and r["findings"] == {"opened": 5, "updated": 0}
    assert len(state.load(trusted)["executions"]) == 1


@pytest.mark.parametrize("driver", ["{}", "{'name': 5}", "{'name': '   '}", "{'name': ''}"])
def test_execute_survives_a_scanner_with_no_name(trusted, actor, py, driver):
    results = ("[{'ruleId': 'R%d' % n, 'level': 'error', 'message': {'text': 'm'}} for n in range(3)]")
    r = execution.execute(trusted, actor, _sarif_run(py, results, driver))
    assert r["status"] == "failed" and r["findings"] == {"opened": 3, "updated": 0}
    assert {f["source"]["tool"] for f in state.load(trusted)["findings"].values()} == {"unknown"}


def test_execute_survives_odd_alert_values(trusted, actor, py):
    results = ("[{'ruleId': 'R1', 'level': 'x' * 5000, 'message': {'text': 'lone'}, "
               "'locations': [{'physicalLocation': {'artifactLocation': {'uri': 'a\\u0000b.py'}, 'region': {'startLine': -4}}}]}, "
               "{'ruleId': 'R2', 'level': 7, 'message': {'text': ' ' * 300 + 'x'}, 'properties': {'security-severity': '1e999'}}, "
               "{'ruleId': 'R' * 3000, 'message': 5, 'locations': [{}]}, "
               "{'ruleId': 'R4', 'properties': {'security-severity': 99999}, 'locations': [{'physicalLocation': {'artifactLocation': {'uri': 5}}}]}]")
    r = execution.execute(trusted, actor, _sarif_run(py, results))
    assert r["status"] == "failed" and r["findings"] == {"opened": 4, "updated": 0}


def test_alert_strings_are_redacted_with_the_runs_own_environment(trusted, actor, py):
    secret = 'Zx8"kQ02pLm7Qw3Er5Ty'
    results = ("[{'ruleId': 'R1', 'level': 'error', 'message': {'text': 'leaked ' + os.environ['MY_API_TOKEN'] + ' here'}, "
               "'locations': [{'physicalLocation': {'artifactLocation': {'uri': 'a.py'}}}]}]")
    environ = dict(os.environ, MY_API_TOKEN=secret)
    execution.execute(trusted, actor, _sarif_run(py, results), environ=environ)
    ledger_text = "".join(p.read_text(encoding="utf-8") for p in trusted.path("ledger").iterdir())
    assert secret not in ledger_text and "Zx8" not in ledger_text
    assert "leaked" in state.load(trusted)["findings"]["FND-001"]["description"]


def test_evidence_resolution_uses_known_references(ws, monkeypatch):
    f = base(evidence=["EV-0123456789ab"])
    assert _has(findings.validate_new(state.load(ws), f), "resolves to no")
    monkeypatch.setattr(state, "known_references", lambda st: {"EV-0123456789ab"})
    assert not _has(findings.validate_new(state.load(ws), f), "resolves to no")


def test_relative_alert_paths_are_joined_with_the_runs_directory(trusted, actor, py):
    for pkg in ("a", "b"):
        (trusted.root / "packages" / pkg).mkdir(parents=True)
    out = [execution.execute(trusted, actor, _sarif_run(py, "[{'ruleId': 'R', 'level': 'error', 'message': {'text': 'm'}, "
           "'locations': [{'physicalLocation': {'artifactLocation': {'uri': 'src/index.ts'}}}]}]", cwd=Path("packages") / pkg))
           for pkg in ("a", "b")]
    assert [o["findings"] for o in out] == [{"opened": 1, "updated": 0}] * 2
    assert [f["component"]["path"] for f in state.load(trusted)["findings"].values()] == ["packages/a/src/index.ts", "packages/b/src/index.ts"]


def test_unnormalizable_paths_stay_different_findings(trusted, actor, run_id):
    out, found = _ingest(trusted, actor, run_id, *[_alert(path=p) for p in ("../a.py", "../b.py", "C:\\c.py")], _alert(path="  "), _alert(path=None))
    assert out == {"opened": 4, "updated": 1}
    assert [f["key_attributes"] for f in found.values()] == [{"uri": "../a.py"}, {"uri": "../b.py"}, {"uri": "C:\\c.py"}, {}]
    assert "uri" not in found["FND-004"]["source"]


def test_alert_absolute_and_file_paths_under_the_root(trusted, actor, run_id):
    root = str(trusted.root)
    cases = [(root + "/src/a.py", "src/a.py"), ("file://" + root + "/src/b.py", "src/b.py"),
             ("file://localhost" + root + "/src/c.py", "src/c.py"), ("file://" + root + "/src/my%20file.py", "src/my file.py"),
             (root + "/src/../lib/d.py", "lib/d.py"), ("file:///" + root.lstrip("/") + "/%73rc/e.py", "src/e.py"),
             ("file:src/f.py", "src/f.py")]
    _, found = _ingest(trusted, actor, run_id, *[_alert(path=raw, rule_id="R%d" % n) for n, (raw, _w) in enumerate(cases)])
    assert [f["component"]["path"] for f in found.values()] == [want for _r, want in cases]
    escape = _ingest(trusted, actor, run_id, _alert(path=root + "/../outside.py", rule_id="ESC"))[1]
    assert "path" not in escape["FND-%03d" % (len(cases) + 1)]["component"]
    assert escape["FND-%03d" % (len(cases) + 1)]["source"]["uri"] == root + "/../outside.py"


def test_same_rule_and_file_is_one_finding_seen_twice(trusted, actor, run_id):
    out, found = _ingest(trusted, actor, run_id, _alert(start_line=1), _alert(start_line=9), _alert(rule_id="R2"), _alert(path="b.py"))
    assert out == {"opened": 3, "updated": 1} and len(found) == 3
    assert found["FND-001"]["source"]["start_line"] == 9
    assert [f["id"] for f in found.values()] == ["FND-001", "FND-002", "FND-003"]


def test_alert_findings_continue_the_numbering(trusted, actor, run_id):
    findings.add(trusted, actor, base())
    out, found = _ingest(trusted, actor, run_id, _alert(), _alert(rule_id="R2"))
    assert out == {"opened": 2, "updated": 0} and list(found) == ["FND-001", "FND-002", "FND-003"]


def test_alert_with_no_alerts_records_nothing(trusted, actor, run_id):
    out, found = _ingest(trusted, actor, run_id)
    assert out == {"opened": 0, "updated": 0} and found == {}


def test_a_clean_scan_reports_no_findings(trusted, actor, py):
    code = "import json, sys; json.dump({'version': '2.1.0', 'runs': [{'tool': {'driver': {'name': 's'}}, 'results': []}]}, open(sys.argv[1], 'w'))"
    r = execution.execute(trusted, actor, execution.RunRequest(command=py.argv(code, "{evidence}/r.sarif"), reports=(("sarif", "{evidence}/r.sarif"),),
                                                              activity="static-analysis", category="security"))
    assert r["findings"] == {"opened": 0, "updated": 0} and state.load(trusted)["findings"] == {}


def test_a_timed_out_scan_still_records_its_alerts(trusted, actor, py):
    code = WRITE_SARIF.replace("sys.exit(1)", "sys.stdout.flush(); import time; time.sleep(30)")
    request = execution.RunRequest(command=py.argv(code, "{evidence}/r.sarif"), reports=(("sarif", "{evidence}/r.sarif"),),
                                   activity="static-analysis", category="security", timeout_s=2)
    r = execution.execute(trusted, actor, request, grace_s=1)
    assert r["reason"] == "timeout" and r["findings"] == {"opened": 1, "updated": 0}
    assert state.load(trusted)["findings"]["FND-001"]["source"]["tool"] == "scanner"


def test_a_blocked_or_test_run_reports_no_findings(trusted, actor, py):
    blocked = execution.execute(trusted, actor, execution.RunRequest(command=py.argv("pass"), activity="deploy"))
    ok = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))
    assert blocked["findings"] == ok["findings"] == {"opened": 0, "updated": 0}


def test_alert_findings_are_unresolved_until_triaged(trusted, actor, run_id):
    _, found = _ingest(trusted, actor, run_id, _alert(security_severity=9.1))
    f = found["FND-001"]
    assert (f["severity"], f["verification_status"], f["state"]) == ("CRITICAL", "suspected", "open")
    findings.update(trusted, actor, "FND-001", {"severity_rationale": {"anchor": "SEC-C1", "modifiers": ["unreachable"]}})
    assert state.load(trusted)["findings"]["FND-001"]["severity"] == "HIGH"


# --------------------------------------------------------------------------- schemas and files

def test_policy_files_validate_against_their_schemas():
    for name in ("severity-rubric", "priority-matrix"):
        doc = json.loads((resource_dir("policies") / (name + ".json")).read_text(encoding="utf-8"))
        schema.validate(doc, name + ".schema.json")


def test_policy_schemas_refuse_a_broken_policy():
    rubric = json.loads((resource_dir("policies") / "severity-rubric.json").read_text(encoding="utf-8"))
    matrix = json.loads((resource_dir("policies") / "priority-matrix.json").read_text(encoding="utf-8"))
    for name, doc, edit in (
            ("severity-rubric", rubric, lambda d: d["anchors"].update({"SEC-H9": {"severity": "HIGH", "column": "SEC"}})),
            ("severity-rubric", rubric, lambda d: d["anchors"]["SEC-H1"].update(severity="SEVERE")),
            ("severity-rubric", rubric, lambda d: d["columns"].pop("pipeline")),
            ("severity-rubric", rubric, lambda d: d["modifiers"].update(unreachable=-2)),
            ("priority-matrix", matrix, lambda d: d["rows"]["HIGH"].pop()),
            ("priority-matrix", matrix, lambda d: d["rows"].pop("LOW")),
            ("priority-matrix", matrix, lambda d: d["adjustments"].update(public_exploit=2))):
        broken = copy.deepcopy(doc)
        edit(broken)
        with pytest.raises(QaError):
            schema.validate(broken, name + ".schema.json")


def test_the_opened_schema_names_exactly_the_record():
    props = schema.load_schema("events/finding.opened.schema.json")
    assert set(props["properties"]) == A3_KEYS | {"justification"} and set(props["required"]) == A3_KEYS and props["additionalProperties"] is False


# --------------------------------------------------------------------------- final review: I5, M1

def _dismissal(**extra):
    return dict({"verification_status": "false_positive", "impact": "none", "recommendation": "none"}, **extra)


@pytest.mark.parametrize("extra", [{}, {"justification": ""}, {"justification": "   "}], ids=["absent", "empty", "blank"])
def test_dismissing_a_finding_needs_a_justification(ws, actor, extra):  # I5, §10.6
    e, _ = findings.add(ws, actor, base())
    with pytest.raises(QaError) as err:
        findings.update(ws, actor, e["data"]["id"], _dismissal(**extra))
    assert err.value.code == "finding_invalid"
    assert state.load(ws)["findings"][e["data"]["id"]]["verification_status"] == "suspected"


def test_a_justification_is_recorded_in_the_event_and_the_state(ws, actor):  # I5
    e, _ = findings.add(ws, actor, base())
    event = findings.update(ws, actor, e["data"]["id"], _dismissal(justification="the route checks ownership at app/orders/views.py:42"))
    assert event["data"]["changes"]["justification"].startswith("the route checks")
    assert state.load(ws)["findings"][e["data"]["id"]]["justification"].startswith("the route checks")


def test_a_finding_can_be_opened_as_a_false_positive_only_with_a_justification(ws, actor):  # I5
    assert any("justification" in p for p in findings.validate_new(state.load(ws), base(**_dismissal())))
    assert findings.validate_new(state.load(ws), base(**_dismissal(justification="disproved"))) == []


def test_other_statuses_do_not_ask_for_a_justification(ws, actor):  # I5: and record no empty field
    e, _ = findings.add(ws, actor, base())
    assert "justification" not in e["data"]


def test_a_long_run_with_no_citation_is_searched_in_linear_time():  # M1: this runs under the ledger lock
    run = "a" * 200000
    assert finishes(lambda: findings._CAUSE_CITATION.search(run), seconds=5) is None


@pytest.mark.parametrize("cause", ["see app/views.py:42 for it", "(views.py:7)", "1:a.py:3"])
def test_the_citation_is_still_found_with_the_lookbehind(cause):  # M1
    assert findings._CAUSE_CITATION.search(cause) is not None


# --------------------------------------------------------------------------- deduplication (task 14)

def _twin(tool, line, end=None, rule="R", evidence=(), cwe=("CWE-639",), path="app/orders/views.py"):
    source = {"skill": "qa-test-execution", "method": "static", "tool": tool, "start_line": line}
    if end is not None:
        source["end_line"] = end
    return base(component={"path": path, "criticality": "normal"}, source=source, evidence=list(evidence),
                refs={"cwe": list(cwe), "rule": rule, "location": path})


def _two_runs(trusted, actor, py):
    return [execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))["execution"] for _ in range(2)]


def test_dedupe_proposes_but_records_nothing_until_confirmed(trusted, actor):
    findings.add(trusted, actor, _twin("alpha", 10, 12, rule="A1"))
    findings.add(trusted, actor, _twin("beta", 11, rule="B1"))
    before = len(list(ledger.iter_all_events(trusted)))
    result = findings.dedupe(trusted, actor)
    assert [(p["keep"], p["duplicate"]) for p in result["proposals"]] == [("FND-001", "FND-002")]
    assert result["proposals"][0]["basis"] == {"path": "app/orders/views.py", "cwe": ["CWE-639"], "lines": [10, 12]}
    assert result["linked"] == [] and len(list(ledger.iter_all_events(trusted))) == before


def test_dedupe_links_duplicates_and_keeps_both_evidence(trusted, actor, py):
    a, b = _two_runs(trusted, actor, py)
    findings.add(trusted, actor, _twin("alpha", 10, 14, rule="A1", evidence=[a]))
    findings.add(trusted, actor, _twin("beta", 12, rule="B1", evidence=[b]))
    result = findings.dedupe(trusted, actor, confirm=[("FND-001", "FND-002")])
    assert [(l["keep"], l["duplicate"]) for l in result["linked"]] == [("FND-001", "FND-002")]
    st = state.load(trusted)
    keep, dup = st["findings"]["FND-001"], st["findings"]["FND-002"]
    assert (dup["state"], dup["duplicate_of"]) == ("duplicate", "FND-001")
    assert keep["state"] == "open" and keep["evidence"] == [a, b]
    assert keep["links"]["duplicates"] == ["FND-002"] and keep["links"]["duplicate_sources"] == ["beta"]
    assert dup["evidence"] == [b] and st["violations"] == []  # the duplicate keeps its own record too


def test_dedupe_is_idempotent(trusted, actor, py):
    a, b = _two_runs(trusted, actor, py)
    findings.add(trusted, actor, _twin("alpha", 10, rule="A1", evidence=[a]))
    findings.add(trusted, actor, _twin("beta", 10, rule="B1", evidence=[b]))
    findings.dedupe(trusted, actor, confirm=[("FND-001", "FND-002")])
    once = state.state_hash(state.load(trusted))
    events = len(list(ledger.iter_all_events(trusted)))
    again = findings.dedupe(trusted, actor, confirm=[("FND-001", "FND-002")])
    assert again["linked"] == [] and again["already_linked"] == [{"keep": "FND-001", "duplicate": "FND-002"}]
    assert again["proposals"] == [] and findings.dedupe(trusted, actor)["proposals"] == []
    assert len(list(ledger.iter_all_events(trusted))) == events and state.state_hash(state.load(trusted)) == once


@pytest.mark.parametrize("second", [
    dict(path="app/other.py"),                 # another file
    dict(cwe=("CWE-79",)),                     # another CWE
    dict(line=40),                             # lines that do not overlap
])
def test_dedupe_proposes_only_same_file_same_cwe_overlapping_lines(trusted, actor, second):
    findings.add(trusted, actor, _twin("alpha", 10, 12, rule="A1"))
    findings.add(trusted, actor, _twin("beta", second.pop("line", 11), rule="B1", **second))
    assert findings.dedupe(trusted, actor)["proposals"] == []


def test_dedupe_refuses_a_pair_it_did_not_propose_and_changes_nothing(trusted, actor):
    findings.add(trusted, actor, _twin("alpha", 10, rule="A1"))
    findings.add(trusted, actor, _twin("beta", 90, rule="B1"))
    before = len(list(ledger.iter_all_events(trusted)))
    with pytest.raises(QaError) as e:
        findings.dedupe(trusted, actor, confirm=[("FND-001", "FND-002")])
    assert e.value.code == "not_a_duplicate" and len(list(ledger.iter_all_events(trusted))) == before


def test_dedupe_proposals_are_bounded_on_a_crowded_file(trusted, actor):
    st = state.empty_state()
    for n in range(1, 400):
        st["findings"]["FND-%03d" % n] = {"state": "open", "component": {"path": "a.py"}, "refs": {"cwe": ["CWE-1"]},
                                          "source": {"start_line": 1, "end_line": 9, "method": "static", "skill": None}, "evidence": []}
    assert len(findings.propose_duplicates(st)) == findings.MAX_PROPOSALS


def test_a_deduplicated_event_for_a_missing_finding_is_a_violation():
    event = {"id": "EV1", "type": "finding.deduplicated", "ts": "2026-01-01T00:00:00.000Z",
             "data": {"id": "FND-002", "duplicate_of": "FND-001", "basis": {"path": "a", "cwe": ["CWE-1"], "lines": [1, 1]},
                      "evidence_added": [], "source_label": "x"}}
    st = state.fold([event])
    assert st["violations"] == [{"event": "EV1", "kind": "unknown_finding"}]


# --------------------------------------------------------------------------- deduplication, fix round 1

def _three_twins(trusted, actor, py):
    runs = [execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))["execution"] for _ in range(3)]
    for n, tool in enumerate(("alpha", "beta", "gamma")):
        findings.add(trusted, actor, _twin(tool, 10, rule="R%d" % n, evidence=[runs[n]]))
    return runs


@pytest.mark.parametrize("closed", ["wont_fix", "accepted_risk"])
def test_dedupe_never_proposes_or_links_a_closed_finding_as_the_duplicate(trusted, actor, py, closed):
    _three_twins(trusted, actor, py)
    extra = dict(accepted_by="user", expires_at="2099-01-01T00:00:00.000Z") if closed == "accepted_risk" else {}
    findings.change_state(trusted, actor, "FND-002", closed, reason="decided", **extra)
    proposals = findings.dedupe(trusted, actor)["proposals"]
    assert all(p["duplicate"] != "FND-002" for p in proposals)
    assert ("FND-001", "FND-003") in [(p["keep"], p["duplicate"]) for p in proposals]
    before = len(list(ledger.iter_all_events(trusted)))
    result = findings.dedupe(trusted, actor, confirm=[("FND-001", "FND-002")])
    assert result["linked"] == [] and [(r["keep"], r["duplicate"]) for r in result["refused"]] == [("FND-001", "FND-002")]
    assert result["refused"][0]["reason"] and len(list(ledger.iter_all_events(trusted))) == before
    assert state.load(trusted)["violations"] == []


def test_a_closed_finding_can_still_be_the_keep_side(trusted, actor, py):
    a, b, _ = _three_twins(trusted, actor, py)
    findings.change_state(trusted, actor, "FND-001", "wont_fix", reason="decided")
    result = findings.dedupe(trusted, actor, confirm=[("FND-001", "FND-002")])
    assert [(l["keep"], l["duplicate"]) for l in result["linked"]] == [("FND-001", "FND-002")]
    st = state.load(trusted)
    assert st["violations"] == [] and st["findings"]["FND-001"]["evidence"] == [a, b]


def test_dedupe_refuses_a_chain_inside_one_batch_and_loses_no_evidence(trusted, actor, py):
    a, b, c = _three_twins(trusted, actor, py)
    before = len(list(ledger.iter_all_events(trusted)))
    result = findings.dedupe(trusted, actor, confirm=[("FND-002", "FND-003"), ("FND-001", "FND-002")])
    assert [(l["keep"], l["duplicate"]) for l in result["linked"]] == [("FND-002", "FND-003")]
    assert [(r["keep"], r["duplicate"]) for r in result["refused"]] == [("FND-001", "FND-002")]
    assert "chain" in result["refused"][0]["reason"] and len(list(ledger.iter_all_events(trusted))) == before + 1
    st = state.load(trusted)
    assert st["violations"] == [] and st["findings"]["FND-002"]["evidence"] == [b, c]
    assert st["findings"]["FND-001"]["evidence"] == [a] and not st["findings"]["FND-001"].get("links", {}).get("duplicates")


def test_dedupe_refuses_the_other_chain_order_and_the_reverse(trusted, actor, py):
    _three_twins(trusted, actor, py)
    result = findings.dedupe(trusted, actor, confirm=[("FND-001", "FND-002"), ("FND-002", "FND-003"), ("FND-003", "FND-001")])
    assert [(l["keep"], l["duplicate"]) for l in result["linked"]] == [("FND-001", "FND-002")]
    assert [(r["keep"], r["duplicate"]) for r in result["refused"]] == [("FND-002", "FND-003"), ("FND-003", "FND-001")]


def test_dedupe_refuses_a_chain_against_an_existing_link(trusted, actor, py):
    a, b, c = _three_twins(trusted, actor, py)
    findings.dedupe(trusted, actor, confirm=[("FND-002", "FND-003")])
    result = findings.dedupe(trusted, actor, confirm=[("FND-001", "FND-002"), ("FND-003", "FND-001")])
    assert result["linked"] == [] and len(result["refused"]) == 2
    assert state.load(trusted)["violations"] == []


def test_every_pair_of_a_batch_has_one_outcome(trusted, actor, py):
    _three_twins(trusted, actor, py)
    pairs = [("FND-001", "FND-002"), ("FND-001", "FND-002"), ("FND-002", "FND-003"), ("FND-001", "FND-003")]
    result = findings.dedupe(trusted, actor, confirm=pairs)
    seen = ([(l["keep"], l["duplicate"]) for l in result["linked"]] + [(l["keep"], l["duplicate"]) for l in result["already_linked"]]
            + [(l["keep"], l["duplicate"]) for l in result["refused"]])
    assert sorted(seen) == [("FND-001", "FND-002"), ("FND-001", "FND-002"), ("FND-001", "FND-003"), ("FND-002", "FND-003")]
    assert len(seen) == len(pairs)
    assert [(l["keep"], l["duplicate"]) for l in result["linked"]] == [("FND-001", "FND-002"), ("FND-001", "FND-003")]
