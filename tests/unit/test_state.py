from __future__ import annotations

import json
from pathlib import Path

from qa import ledger, state

INIT = {"kernel_version": "0.1.0-dev", "schema_version": "1", "config_hash": "sha256:" + "0" * 64}


def test_skeleton():
    assert set(state.fold([])) == {"executions", "results", "tests", "findings", "authorizations", "config",
                                   "blocked_commands", "gates", "reports", "plans", "bundles", "violations", "heads", "worktrees",
                                   "requirements", "criteria", "assumptions", "risks", "testcases", "links", "fixes", "revalidations"}


def test_unknown_type_is_violation():
    s = state.fold([{"id": "EVT-X", "type": "made.up", "data": {}}])
    assert s["violations"] == [{"event": "EVT-X", "kind": "unknown_type"}]


def test_invalid_data_is_violation_and_not_reduced():
    s = state.fold([{"id": "EVT-Y", "type": "workspace.initialized", "data": {"x": 1}}])
    assert s["violations"] == [{"event": "EVT-Y", "kind": "invalid_data"}]
    assert s["config"] == {"accepted_hash": None, "history": [], "drift": []}


def test_initialized_sets_accepted_hash(ws, actor):
    ledger.append(ws, actor, "workspace.initialized", INIT)
    s = state.load(ws)
    assert s["config"]["accepted_hash"] == INIT["config_hash"]
    assert s["config"]["history"][0]["kind"] == "initialized"
    assert s["heads"][actor.session]["seq"] == 1


def test_rebuild_is_byte_identical(ws, actor):
    ledger.append(ws, actor, "workspace.initialized", INIT)
    state.rebuild(ws)
    first = {p.name: p.read_bytes() for p in ws.path("state").iterdir()}
    state.rebuild(ws)
    assert first == {p.name: p.read_bytes() for p in ws.path("state").iterdir()}


def test_rebuild_writes_each_key_and_summary(ws, actor):
    ledger.append(ws, actor, "workspace.initialized", INIT)
    state.rebuild(ws)
    names = {p.name for p in ws.path("state").iterdir()}
    assert names == {k + ".json" for k in state.empty_state()} | {"summary.json"}
    summary = json.loads(ws.path("state", "summary.json").read_text(encoding="utf-8"))
    assert summary["config"] == 1 and summary["heads"] == 1 and summary["executions"] == 0


def test_state_hash_ignores_gates_reports_heads():
    a, b = state.empty_state(), state.empty_state()
    b["gates"]["GATE-001"] = {}
    b["reports"]["RPT-001"] = {}
    b["heads"]["S-x"] = {}
    assert state.state_hash(a) == state.state_hash(b)
    b["findings"]["F-1"] = {}
    assert state.state_hash(a) != state.state_hash(b)


def test_every_reducer_has_an_event_schema():
    from qa import resource_dir

    for type_ in state.REDUCERS:
        assert (resource_dir("schemas") / "events" / (type_ + ".schema.json")).is_file(), type_


def test_fold_reraises_non_schema_errors(monkeypatch):
    import pytest
    from qa.errors import QaError

    def boom(*a, **k):
        raise QaError("resource_missing", "x")

    monkeypatch.setattr(state.schema, "validate", boom)
    with pytest.raises(QaError) as e:
        state.fold([{"id": "EVT-Z", "type": "workspace.initialized", "data": INIT}])
    assert e.value.code == "resource_missing"


# The hash of tests/unit/milestone2-events.json as the milestone 2 kernel (a8460cc) computed it: that kernel folded the ledger and
# hashed the state without the worktrees key. The file is a milestone 2 ledger (one passing run), so no event is new.
MILESTONE_2_HASH = "sha256:fda6837b1cb743887acd09ec943d29273b3a5cb89ce754739526daf45f452dd4"


def test_state_hash_of_a_milestone_2_ledger_is_unchanged():
    events = json.loads((Path(__file__).parent / "milestone2-events.json").read_text(encoding="utf-8"))
    folded = state.fold(events)
    assert folded["violations"] == [] and len(folded["executions"]) == 1 and folded["worktrees"] == {}
    assert state.state_hash(folded) == MILESTONE_2_HASH


def test_a_new_key_counts_once_it_holds_something():
    base = state.fold([])
    held = dict(base, worktrees={".qa/work/wt-0": {"commit": "0" * 40, "event": "EVT-1", "removed": None}})
    assert state.state_hash(held) != state.state_hash(base)


def test_new_record_keys_are_left_out_of_the_hash_while_empty():
    base = state.fold([])
    for key in ("requirements", "criteria", "assumptions", "risks", "testcases", "links"):
        assert key in base and not base[key]
    stripped = {k: v for k, v in base.items() if k not in ("requirements", "criteria", "assumptions", "risks", "testcases", "links")}
    assert state.state_hash(base) == state.state_hash(stripped)
    held = dict(base, links=[{"source": "AC-001.1", "relation": "verified_by", "target": "TC-001", "event": "EVT-1"}])
    assert state.state_hash(held) != state.state_hash(base)
