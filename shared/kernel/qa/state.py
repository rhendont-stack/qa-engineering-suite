from __future__ import annotations

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


# Later tasks add their event types here.
REDUCERS: Dict[str, Callable[[dict, dict], None]] = {
    "workspace.initialized": _workspace_initialized,
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
        except QaError:
            state["violations"].append({"event": event["id"], "kind": "invalid_data"})
            continue
        reducer(state, event)
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
