from __future__ import annotations

import copy
from typing import Callable, Dict, Iterable

from . import ledger, schema
from .canonical import hash_obj
from .errors import QaError
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


# Later tasks add their event types here.
REDUCERS: Dict[str, Callable[[dict, dict], None]] = {
    "workspace.initialized": _workspace_initialized,
    "config.changed": _config_approved("changed"),
    "config.accepted": _config_approved("accepted"),
    "config.drift_detected": _config_drift_detected,
    "authorization.granted": _authorization_granted,
    "authorization.revoked": _authorization_revoked,
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
