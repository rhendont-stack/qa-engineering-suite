from __future__ import annotations

import json
import shutil

import pytest

from qa import authority, ledger, records, state, workspace
from qa.errors import QaError


def req(**over):
    data = {"statement": "A discount never exceeds 50 percent of the order total.", "type": "functional",
            "source": {"path": "docs/spec.md", "section": "3.2"}, "provenance": "documented", "priority": "must"}
    data.update(over)
    return data


def ac(requirement="REQ-001", **over):
    data = {"requirement": requirement, "given": "an order of 100", "when": "a 60 percent code is applied",
            "then": "the discount is 50", "verifiable": True}
    data.update(over)
    return data


def tc(links=("AC-001.1",), **over):
    data = {"title": "Discount is capped", "level": "unit", "technique": "boundary-value-analysis",
            "technique_reason": "the cap is a boundary", "oracle": "requirement", "priority": "P1",
            "criticality": "high", "links": list(links), "automation": ["tests.test_discount::test_cap"]}
    data.update(over)
    return data


def rsk(mitigations=("TC-001",), **over):
    data = {"title": "Cap bypassed by stacking codes", "likelihood": 3, "impact": 4, "mitigations": list(mitigations)}
    data.update(over)
    return data


def refused(code, call, *args):
    with pytest.raises(QaError) as caught:
        call(*args)
    assert caught.value.code == code
    return caught.value


def chain(ws, actor):
    """REQ-001, AC-001.1, TC-001, RSK-001, ASM-001, in creation order."""
    records.add(ws, actor, "requirement", req())
    records.add(ws, actor, "criterion", ac())
    records.add(ws, actor, "testcase", tc())
    records.add(ws, actor, "risk", rsk())
    records.add(ws, actor, "assumption", {"statement": "Codes are case-insensitive.", "impact": "A test may be wrong."})


def test_ids_are_sequential_and_ac_carries_its_requirement(ws, actor):
    assert records.add(ws, actor, "requirement", req())["id"] == "REQ-001"
    assert records.add(ws, actor, "requirement", req(statement="Second."))["id"] == "REQ-002"
    assert records.add(ws, actor, "criterion", ac("REQ-001"))["id"] == "AC-001.1"
    assert records.add(ws, actor, "criterion", ac("REQ-001"))["id"] == "AC-001.2"
    assert records.add(ws, actor, "criterion", ac("REQ-002"))["id"] == "AC-002.1"
    assert records.add(ws, actor, "assumption", {"statement": "s", "impact": "i"})["id"] == "ASM-001"


def test_ids_grow_past_999(ws, actor):
    for number in (999, 1000):
        ledger.append(ws, actor, "requirement.upserted", dict(req(), id="REQ-%03d" % number, quality_issues=[]))
    assert records.add(ws, actor, "requirement", req())["id"] == "REQ-1001"
    assert records.add(ws, actor, "criterion", ac("REQ-1001"))["id"] == "AC-1001.1"


def test_requirement_needs_a_cited_source(ws, actor):
    for source in ({"section": "3.2"}, {"path": "docs/spec.md"}, {}):
        refused("records_invalid", records.add, ws, actor, "requirement", req(source=source))
    bad = req()
    del bad["source"]
    refused("records_invalid", records.add, ws, actor, "requirement", bad)
    assert records.add(ws, actor, "requirement", req(source={"url": "https://example.test/spec"}))["id"] == "REQ-001"
    assert records.add(ws, actor, "requirement", req(source={"path": "a.md", "line": 4}))["id"] == "REQ-002"


def test_requirement_type_is_functional_or_an_iso_25010_characteristic(ws, actor):
    refused("records_invalid", records.add, ws, actor, "requirement", req(type="nice"))
    records.add(ws, actor, "requirement", req(type="security"))


def test_inferred_requirement_is_unconfirmed_until_the_user_confirms(ws, actor):
    records.add(ws, actor, "requirement", req(provenance="inferred"))
    assert "confirmed" not in state.load(ws)["requirements"]["REQ-001"]
    done = records.confirm_requirement(ws, actor, "REQ-001", "yes, that is the rule")
    assert done["id"] == "REQ-001"
    assert state.load(ws)["requirements"]["REQ-001"]["confirmed"]["approval"] == "yes, that is the rule"
    refused("unknown_record", records.confirm_requirement, ws, actor, "REQ-009", "ok")
    refused("blank_approval", records.confirm_requirement, ws, actor, "REQ-001", "  ")


def test_confirmation_without_a_valid_mac_counts_for_nothing(ws, actor, tmp_path):
    records.add(ws, actor, "requirement", req(provenance="inferred"))
    records.add(ws, actor, "assumption", {"statement": "s", "impact": "i"})
    records.confirm_requirement(ws, actor, "REQ-001", "yes")
    records.decide_assumption(ws, actor, "ASM-001", "confirmed", "yes")
    root = tmp_path / "other"
    root.mkdir()
    shutil.copytree(str(ws.qa_dir), str(root / ".qa"))
    (root / ".qa" / "authority.key").write_bytes(b"k" * 32)
    copy = workspace.find_workspace(root, root)
    folded = state.load(copy)
    assert "confirmed" not in folded["requirements"]["REQ-001"]
    assert folded["assumptions"]["ASM-001"]["status"] == "open"
    assert state.load(ws)["assumptions"]["ASM-001"]["status"] == "confirmed"


def test_the_two_decision_events_are_signed():
    assert {"requirement.confirmed", "assumption.decided"} <= authority.EVENTS <= authority.CONFERRING | {"authorization.revoked"}
    assert {"requirement.confirmed", "assumption.decided"} <= authority.CONFERRING


def test_editing_a_confirmed_statement_clears_the_confirmation(ws, actor):
    records.add(ws, actor, "requirement", req())
    records.confirm_requirement(ws, actor, "REQ-001", "yes")
    records.add(ws, actor, "requirement", {"id": "REQ-001", "priority": "should"})
    kept = state.load(ws)["requirements"]["REQ-001"]
    assert kept["priority"] == "should" and "confirmed" in kept
    done = records.add(ws, actor, "requirement", {"id": "REQ-001", "statement": "Changed."})
    assert done["id"] == "REQ-001" and done["created"] is False
    cleared = state.load(ws)["requirements"]["REQ-001"]
    assert cleared["statement"] == "Changed." and "confirmed" not in cleared


def test_an_update_of_an_unknown_record_is_refused(ws, actor):
    refused("unknown_record", records.add, ws, actor, "requirement", {"id": "REQ-007", "priority": "could"})
    refused("records_invalid", records.add, ws, actor, "requirement", {"id": "AC-001.1", "priority": "could"})


def test_decisions_cannot_be_written_through_add(ws, actor):
    refused("records_invalid", records.add, ws, actor, "requirement", req(confirmed={"approval": "x"}))
    refused("records_invalid", records.add, ws, actor, "assumption", {"statement": "s", "impact": "i", "status": "confirmed"})


def test_assumption_is_decided_once_signed_and_a_new_statement_reopens_it(ws, actor):
    records.add(ws, actor, "assumption", {"statement": "s", "impact": "i"})
    assert state.load(ws)["assumptions"]["ASM-001"]["status"] == "open"
    records.decide_assumption(ws, actor, "ASM-001", "rejected", "no")
    done = state.load(ws)["assumptions"]["ASM-001"]
    assert done["status"] == "rejected" and done["confirmed_by"] == "user" and "decided_by" not in done
    refused("records_invalid", records.decide_assumption, ws, actor, "ASM-001", "open", "x")
    refused("unknown_record", records.decide_assumption, ws, actor, "ASM-009", "confirmed", "x")
    records.add(ws, actor, "assumption", {"id": "ASM-001", "statement": "new"})
    assert state.load(ws)["assumptions"]["ASM-001"]["status"] == "open"


def test_ac_for_an_unknown_requirement_is_refused(ws, actor):
    refused("unknown_record", records.add, ws, actor, "criterion", ac("REQ-042"))
    records.add(ws, actor, "requirement", req())
    refused("records_invalid", records.add, ws, actor, "criterion", ac(verifiable="yes"))


def test_tc_oracle_and_criticality_are_closed_vocabularies(ws, actor):
    records.add(ws, actor, "requirement", req())
    records.add(ws, actor, "criterion", ac())
    for field, value in (("oracle", "gut-feeling"), ("criticality", "urgent"), ("priority", "P4")):
        refused("records_invalid", records.add, ws, actor, "testcase", tc(**{field: value}))
    for oracle in ("requirement", "specification", "property", "differential", "characterization"):
        records.add(ws, actor, "testcase", tc(oracle=oracle))


def test_tc_links_go_to_criteria_that_exist_and_become_trace_events_only(ws, actor):
    records.add(ws, actor, "requirement", req())
    records.add(ws, actor, "criterion", ac())
    refused("unknown_record", records.add, ws, actor, "testcase", tc(links=["AC-001.9"]))
    refused("records_invalid", records.add, ws, actor, "testcase", tc(links=["RSK-001"]))
    assert state.load(ws)["testcases"] == {}  # a refused add records nothing
    records.add(ws, actor, "testcase", tc())
    st = state.load(ws)
    assert "links" not in st["testcases"]["TC-001"] and st["testcases"]["TC-001"]["automation"] == ["tests.test_discount::test_cap"]
    assert [(l["source"], l["relation"], l["target"]) for l in st["links"]] == [("AC-001.1", "verified_by", "TC-001")]
    records.add(ws, actor, "testcase", {"id": "TC-001", "links": ["AC-001.1"]})  # the same link again adds nothing
    assert len(state.load(ws)["links"]) == 1


def test_risk_needs_mitigations_or_an_accepted_gap_with_a_name(ws, actor):
    records.add(ws, actor, "requirement", req())
    records.add(ws, actor, "criterion", ac())
    records.add(ws, actor, "testcase", tc())
    refused("records_invalid", records.add, ws, actor, "risk", rsk(mitigations=[]))
    refused("records_invalid", records.add, ws, actor, "risk", rsk(mitigations=[], accepted_gap={"reason": "cost"}))
    refused("records_invalid", records.add, ws, actor, "risk", rsk(mitigations=[], accepted_gap={"reason": "cost", "accepted_by": ""}))
    refused("unknown_record", records.add, ws, actor, "risk", rsk(mitigations=["TC-009"]))
    refused("records_invalid", records.add, ws, actor, "risk", rsk(likelihood=6))
    assert records.add(ws, actor, "risk", rsk())["id"] == "RSK-001"
    assert records.add(ws, actor, "risk", rsk(mitigations=[], accepted_gap={"reason": "cost", "accepted_by": "dana"}))["id"] == "RSK-002"
    st = state.load(ws)
    assert st["risks"]["RSK-001"]["scale"] == "1-5" and "mitigations" not in st["risks"]["RSK-001"]
    assert [(l["source"], l["relation"], l["target"]) for l in st["links"] if l["relation"] == "mitigated_by"] == [("RSK-001", "mitigated_by", "TC-001")]


def test_import_is_all_or_nothing(ws, actor):
    good = [req(), req(statement="Second.")]
    refused("records_invalid", records.add_many, ws, actor, "requirement", good + [req(priority="urgent")])
    assert state.load(ws)["requirements"] == {}
    done = records.add_many(ws, actor, "requirement", good)
    assert [item["id"] for item in done] == ["REQ-001", "REQ-002"]


def test_import_of_test_cases_sees_earlier_entries_of_the_batch(ws, actor):
    records.add(ws, actor, "requirement", req())
    records.add(ws, actor, "criterion", ac())
    done = records.add_many(ws, actor, "testcase", [tc(), tc(title="Second")])
    assert [item["id"] for item in done] == ["TC-001", "TC-002"]
    assert len(state.load(ws)["links"]) == 2


def test_list_records_is_ordered_by_id(ws, actor):
    chain(ws, actor)
    st = state.load(ws)
    assert [r["id"] for r in records.list_records(st, "requirement")] == ["REQ-001"]
    assert [r["id"] for r in records.list_records(st, "criterion")] == ["AC-001.1"]
    refused("usage", records.list_records, st, "widget")


def test_a_hand_written_record_that_breaks_the_schema_folds_to_nothing(ws, actor):
    with pytest.raises(QaError):
        ledger.append(ws, actor, "requirement.upserted", {"id": "REQ-001"})
    folded = state.fold([{"id": "EVT-1", "type": "requirement.upserted", "data": {"id": "REQ-001"}}])
    assert folded["requirements"] == {} and folded["violations"] == [{"event": "EVT-1", "kind": "invalid_data"}]


def test_state_keys_stay_out_of_the_hash_while_empty(ws, actor):
    empty = state.fold([])
    assert state.state_hash(empty) == state.state_hash(dict(empty, links=[], requirements={}, criteria={}, assumptions={},
                                                            risks={}, testcases={}))
    chain(ws, actor)
    assert state.state_hash(state.load(ws)) != state.state_hash(empty)


def test_cli_req_add_prints_one_json_object(ws, qa_cli, tmp_path):
    path = tmp_path / "req.json"
    path.write_text(json.dumps(req()), encoding="utf-8")
    code, out = qa_cli("req", "add", "--json", str(path))
    assert code == 0 and out["id"] == "REQ-001" and out["created"] is True and out["command"] == "req add"
    code, out = qa_cli("req", "confirm", "REQ-001", "--approval", "yes")
    assert code == 0 and out["id"] == "REQ-001"
    code, out = qa_cli("req", "list")
    assert code == 0 and [r["id"] for r in out["requirements"]] == ["REQ-001"] and out["requirements"][0]["confirmed"]


def test_cli_walks_the_creation_order(ws, qa_cli, tmp_path):
    def put(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    assert qa_cli("req", "import", "--json", put("r.json", [req(), req(statement="Second.")]))[1]["ids"] == ["REQ-001", "REQ-002"]
    assert qa_cli("ac", "add", "--json", put("a.json", ac()))[1]["id"] == "AC-001.1"
    assert qa_cli("tc", "add", "--json", put("t.json", tc()))[1]["id"] == "TC-001"
    assert qa_cli("tc", "import", "--json", put("ts.json", [tc(title="B")]))[1]["ids"] == ["TC-002"]
    assert qa_cli("risk", "add", "--json", put("k.json", rsk()))[1]["id"] == "RSK-001"
    assert qa_cli("asm", "add", "--json", put("s.json", {"statement": "s", "impact": "i"}))[1]["id"] == "ASM-001"
    code, out = qa_cli("asm", "decide", "ASM-001", "confirmed", "--approval", "yes")
    assert code == 0 and out["status"] == "confirmed"
    code, out = qa_cli("req", "import", "--json", put("obj.json", req()))
    assert code == 2 and out["error"]["code"] == "invalid_json"
    code, out = qa_cli("asm", "decide", "ASM-001", "maybe", "--approval", "yes")
    assert code == 2 and out["error"]["code"] == "records_invalid"


def test_provenance_of_a_recorded_requirement_cannot_be_changed(ws, actor):
    records.add(ws, actor, "requirement", req(provenance="inferred"))
    before = state.load(ws)
    err = refused("records_invalid", records.add, ws, actor, "requirement", {"id": "REQ-001", "provenance": "documented"})
    assert "provenance" in str(err.details) or "provenance" in err.message
    assert state.load(ws) == before
    records.add(ws, actor, "requirement", {"id": "REQ-001", "provenance": "inferred", "priority": "should"})   # unchanged value is fine
    assert state.load(ws)["requirements"]["REQ-001"]["provenance"] == "inferred"


def test_provenance_cannot_be_changed_through_req_import(ws, actor, qa_cli, tmp_path):
    records.add(ws, actor, "requirement", req(provenance="assumed"))
    path = tmp_path / "r.json"
    path.write_text(json.dumps([req(statement="Second."), {"id": "REQ-001", "provenance": "documented"}]), encoding="utf-8")
    code, out = qa_cli("req", "import", "--json", str(path))
    assert code != 0 and out["error"]["code"] == "records_invalid"
    st = state.load(ws)
    assert list(st["requirements"]) == ["REQ-001"] and st["requirements"]["REQ-001"]["provenance"] == "assumed"
