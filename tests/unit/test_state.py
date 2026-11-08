from __future__ import annotations

import json

from qa import ledger, state

INIT = {"kernel_version": "0.1.0-dev", "schema_version": "1", "config_hash": "sha256:" + "0" * 64}


def test_skeleton():
    assert set(state.fold([])) == {"executions", "results", "tests", "findings", "authorizations", "config",
                                   "blocked_commands", "gates", "reports", "violations", "heads"}


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
