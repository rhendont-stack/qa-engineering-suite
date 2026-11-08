from __future__ import annotations

import copy
from typing import Callable, Dict, Iterable, Optional, Set

from . import ledger, schema
from .canonical import hash_obj
from .errors import QaError
from .status import can_reclassify
from .vocab import TRANSITIONS
from .workspace import Workspace, config_bytes, write_atomic

# State keys. Dict keys are keyed by ID; blocked_commands and violations are lists.
_KEYS = ("executions", "results", "tests", "findings", "authorizations", "config",
         "blocked_commands", "gates", "reports", "violations", "heads")
_LISTS = ("blocked_commands", "violations")
# Derived from evaluation or rendering, so excluded from state_hash.
_UNHASHED = ("gates", "reports", "heads")


def empty_state() -> dict:
    state: dict = {k: ([] if k in _LISTS else {}) for k in _KEYS}
    state["config"] = {"accepted_hash": None, "history": [], "drift": []}
    return state


def _workspace_initialized(state: dict, event: dict) -> None:
    config_hash = event["data"]["config_hash"]
    state["config"]["accepted_hash"] = config_hash
    state["config"]["history"].append({"event": event["id"], "kind": "initialized", "hash": config_hash})


def _config_approved(kind: str) -> Callable[[dict, dict], None]:
    def reduce(state: dict, event: dict) -> None:
        config_hash = event["data"]["hash"]
        state["config"]["accepted_hash"] = config_hash
        state["config"]["history"].append({"event": event["id"], "kind": kind, "hash": config_hash})
    return reduce


def _config_drift_detected(state: dict, event: dict) -> None:
    data = event["data"]
    state["config"]["drift"].append({"event": event["id"], "hash": data["hash"],
                                     "accepted_hash": data["accepted_hash"]})


# Fold-internal: {AUTH id: [(revocation event, its unknown_authorization violation), ...]} for
# revocations folded before any grant of their id. The fold sorts by ts first, so a revocation can
# come first: one in the same millisecond from a session id that sorts first, or one recorded after
# the clock stepped back. The grant settles them when it folds; fold drops the key before it returns.
_EARLY_REVOCATIONS = "_early_revocations"


def _revoke(grant: dict, revocation: dict) -> None:
    if grant["revoked_at"] is None:  # a repeated revocation keeps the first
        grant["revoked_at"] = revocation["ts"]
        grant["revoke_reason"] = revocation["data"]["reason"]


def _authorization_granted(state: dict, event: dict) -> None:
    data = event["data"]
    grants = state["authorizations"]
    if data["id"] in grants:  # the first grant stays, so a revoked one never becomes valid again
        state["violations"].append({"event": event["id"], "kind": "duplicate_id"})
        return
    grant = grants[data["id"]] = dict(copy.deepcopy(data), revoked_at=None, revoke_reason=None)
    for revocation, violation in state.get(_EARLY_REVOCATIONS, {}).pop(data["id"], []):
        state["violations"].remove(violation)  # the ledger holds a grant of this id after all
        _revoke(grant, revocation)


def _authorization_revoked(state: dict, event: dict) -> None:
    auth_id = event["data"]["id"]
    grant = state["authorizations"].get(auth_id)
    if grant is not None:
        _revoke(grant, event)
        return
    violation = {"event": event["id"], "kind": "unknown_authorization"}  # unless a grant of the id folds later
    state["violations"].append(violation)
    state.setdefault(_EARLY_REVOCATIONS, {}).setdefault(auth_id, []).append((event, violation))


# Fold-internal: events whose effect depends on a record that may fold after them. The fold sorts by
# ts first, so after a clock step or in the same millisecond an event can precede what it refers to,
# and a lost downgrade would leave a result passed. _settle applies them; its queue never leaves fold.
_SETTLE = "_settle"


def _violation(state: dict, event: dict, kind: str) -> None:
    state["violations"].append({"event": event["id"], "kind": kind})


def _queue(state: dict, event: dict) -> None:
    state.setdefault(_SETTLE, []).append(event)


def _execution_recorded(state: dict, event: dict) -> None:
    data = event["data"]
    if data["id"] in state["executions"]:  # the first record stays
        _violation(state, event, "duplicate_id")
        return
    state["executions"][data["id"]] = dict(copy.deepcopy(data), ingested=[])


def _result_recorded(state: dict, event: dict) -> None:
    data = event["data"]
    if data["id"] in state["results"]:  # the first record stays
        _violation(state, event, "duplicate_id")
        return
    state["results"][data["id"]] = dict(copy.deepcopy(data), reclassifications=[])
    _queue(state, event)


def _command_blocked(state: dict, event: dict) -> None:
    state["blocked_commands"].append(dict(copy.deepcopy(event["data"]), event=event["id"]))


def _settle_result(state: dict, event: dict) -> None:
    if event["data"]["execution"] not in state["executions"]:
        _violation(state, event, "unknown_execution")


def _settle_reclassification(state: dict, event: dict) -> None:
    data = event["data"]
    result = state["results"].get(data["result"])
    if result is None:
        _violation(state, event, "unknown_result")
    elif not can_reclassify(result["status"], data["new_status"]):
        _violation(state, event, "reclassify_not_allowed")
    else:
        result["reclassifications"].append({"event": event["id"], "from": result["status"], "to": data["new_status"],
                                            "reason": data["reason"], "evidence": list(data["evidence"])})
        result["status"] = data["new_status"]


def _settle_ingest(state: dict, event: dict) -> None:
    execution = state["executions"].get(event["data"]["execution"])
    if execution is None:
        _violation(state, event, "unknown_execution")
    else:
        execution["ingested"].append(dict(copy.deepcopy(event["data"]), event=event["id"]))


def _finding_opened(state: dict, event: dict) -> None:
    data = event["data"]
    if data["id"] in state["findings"]:  # the first record stays
        _violation(state, event, "duplicate_id")
        return
    state["findings"][data["id"]] = dict(copy.deepcopy(data), last_seen=event["ts"])


def _settle_finding_updated(state: dict, event: dict) -> None:
    data = event["data"]
    finding = state["findings"].get(data["id"])
    if finding is None:
        _violation(state, event, "unknown_finding")
    elif "changes" in data:
        finding.update(copy.deepcopy(data["changes"]))
    else:
        finding["last_seen"] = data["last_seen"]
        finding["source"] = copy.deepcopy(data["source"])


def _settle_finding_state_changed(state: dict, event: dict) -> None:
    data = event["data"]
    finding = state["findings"].get(data["id"])
    if finding is None:
        _violation(state, event, "unknown_finding")
    elif data["to"] not in TRANSITIONS.get(finding["state"], {}):  # the current state decides, not the event's "from"
        _violation(state, event, "invalid_transition")
    else:
        finding.update(state=data["to"], state_reason=data["reason"], accepted_by=data["accepted_by"],
                       expires_at=data["expires_at"], duplicate_of=data["duplicate_of"])


def _gate_evaluated(state: dict, event: dict) -> None:
    data = event["data"]
    if data["id"] in state["gates"]:  # the first record stays
        _violation(state, event, "duplicate_id")
        return
    state["gates"][data["id"]] = copy.deepcopy(data)


def _report_rendered(state: dict, event: dict) -> None:
    data = event["data"]
    if data["id"] in state["reports"]:  # the first record stays
        _violation(state, event, "duplicate_id")
        return
    state["reports"][data["id"]] = copy.deepcopy(data)


def _gate_policy_proposed(state: dict, event: dict) -> None:
    data = event["data"]
    state["config"]["history"].append({"event": event["id"], "kind": "gate_policy_proposed", "criterion": data["criterion"],
                                       "override": copy.deepcopy(data["override"]), "rationale": data["rationale"]})


def _gate_policy_accepted(state: dict, event: dict) -> None:
    # Takes effect in fold order, or a config change folded after it would stop being the accepted one; settling undoes
    # it when the proposal never appears (a proposal can fold after its acceptance when clocks step back).
    data = event["data"]
    state["config"]["accepted_hash"] = data["config_hash"]
    state["config"]["history"].append({"event": event["id"], "kind": "gate_policy_accepted", "proposal": data["proposal"],
                                       "hash": data["config_hash"]})
    _queue(state, event)


def _settle_gate_policy_accepted(state: dict, event: dict) -> None:
    config, proposal = state["config"], event["data"]["proposal"]
    if any(entry["kind"] == "gate_policy_proposed" and entry["event"] == proposal for entry in config["history"]):
        return
    _violation(state, event, "unknown_proposal")
    config["history"] = [entry for entry in config["history"] if entry["event"] != event["id"]]
    config["accepted_hash"] = next((entry["hash"] for entry in reversed(config["history"]) if "hash" in entry), None)


_SETTLERS = {"result.recorded": _settle_result, "result.reclassified": _settle_reclassification,
             "report.ingested": _settle_ingest, "finding.updated": _settle_finding_updated,
             "finding.state_changed": _settle_finding_state_changed,
             "gate.policy_accepted": _settle_gate_policy_accepted}


def known_references(st: dict) -> Set[str]:
    """Every ``EXE-`` and ``EV-`` id the state can resolve."""
    known: Set[str] = set(st["executions"])
    for record in st["executions"].values():
        if record["evidence_manifest"] is not None:
            known.update(record["evidence_manifest"]["ids"].values())
        known.update(item["ev_id"] for item in record["ingested"])
    return known


def chain_root(st: dict, execution_id: str) -> str:
    """Follow ``attempt_of`` while the parent exists; a repeated id ends the walk, so a hand-made cycle ends."""
    seen = {execution_id}
    current = execution_id
    while True:
        parent = st["executions"].get(current, {}).get("attempt_of")
        if parent is None or parent in seen or parent not in st["executions"]:
            return current
        seen.add(parent)
        current = parent


def _worst(current: Optional[str], status: str) -> str:
    """A test repeated in one execution counts once: any failure wins, then any pass."""
    if "failed" in (current, status):
        return "failed"
    if "passed" in (current, status):
        return "passed"
    return status


def _test_entry(chain: dict) -> dict:
    counted = [outcome for outcome in chain["outcomes"].values() if outcome in ("passed", "failed")]
    attempts, passes = len(counted), counted.count("passed")
    flaky = 0 < passes < attempts or chain["flaky"]
    latest = chain["latest"]
    if flaky or (attempts > 0 and passes == 0):
        status = "failed"
    elif attempts > 0:
        status = "passed"
    else:
        status = latest["status"]
    return {"result": latest["id"], "execution": latest["execution"], "attempts": attempts, "passes": passes,
            "ratio": passes / attempts if attempts else None, "flaky": flaky, "status": status,
            "reproducible": attempts >= 2 and passes == 0}


def _build_tests(state: dict) -> None:
    chains: Dict[str, Dict[str, dict]] = {}
    for result in state["results"].values():
        execution = result["execution"]
        if execution not in state["executions"]:
            continue
        chain = chains.setdefault(result["test_id"], {}).setdefault(
            chain_root(state, execution), {"outcomes": {}, "flaky": False, "latest": result})
        chain["latest"] = result
        chain["flaky"] = chain["flaky"] or result["flaky"]
        chain["outcomes"][execution] = _worst(chain["outcomes"].get(execution), result["status"])
    state["tests"] = {test_id: {root: _test_entry(chain) for root, chain in by_root.items()}
                      for test_id, by_root in chains.items()}


def _settle(state: dict) -> None:
    for event in state.pop(_SETTLE, []):
        _SETTLERS[event["type"]](state, event)
    _build_tests(state)


# Later tasks add their event types here.
REDUCERS: Dict[str, Callable[[dict, dict], None]] = {
    "workspace.initialized": _workspace_initialized,
    "config.changed": _config_approved("changed"),
    "config.accepted": _config_approved("accepted"),
    "config.drift_detected": _config_drift_detected,
    "authorization.granted": _authorization_granted,
    "authorization.revoked": _authorization_revoked,
    "execution.recorded": _execution_recorded,
    "result.recorded": _result_recorded,
    "result.reclassified": _queue,
    "command.blocked": _command_blocked,
    "report.ingested": _queue,
    "finding.opened": _finding_opened,
    "finding.updated": _queue,
    "finding.state_changed": _queue,
    "gate.evaluated": _gate_evaluated,
    "report.rendered": _report_rendered,
    "gate.policy_proposed": _gate_policy_proposed,
    "gate.policy_accepted": _gate_policy_accepted,
}


def fold(events: Iterable[dict]) -> dict:
    state = empty_state()
    for event in events:
        reducer = REDUCERS.get(event["type"])
        if reducer is None:
            state["violations"].append({"event": event["id"], "kind": "unknown_type"})
            continue
        try:
            schema.validate(event["data"], "events/" + event["type"] + ".schema.json")
        except QaError as exc:
            if exc.code != "schema_invalid":
                raise
            state["violations"].append({"event": event["id"], "kind": "invalid_data"})
            continue
        reducer(state, event)
    state.pop(_EARLY_REVOCATIONS, None)  # its violations stay: no grant in the ledger holds those ids
    _settle(state)
    return state


def load(ws: Workspace) -> dict:
    state = fold(ledger.iter_all_events(ws))
    state["heads"] = ledger.heads(ws)
    return state


def rebuild(ws: Workspace) -> dict:
    with ws.lock():
        state = load(ws)
        summary = {k: len(state["config"]["history"]) if k == "config" else len(state[k]) for k in _KEYS}
        for key in _KEYS:
            write_atomic(ws.path("state", key + ".json"), config_bytes(state[key]))
        write_atomic(ws.path("state", "summary.json"), config_bytes(summary))
    return state


def state_hash(state: dict) -> str:
    return hash_obj({k: v for k, v in state.items() if k not in _UNHASHED})
