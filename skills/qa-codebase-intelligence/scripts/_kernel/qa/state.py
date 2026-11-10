from __future__ import annotations

import copy
from typing import Callable, Dict, Iterable, Optional, Set

from . import authority, ledger, regression, schema
from .canonical import hash_obj
from .errors import QaError
from .status import can_reclassify
from .vocab import FIX_TRANSITIONS, TRANSITIONS
from .workspace import Workspace, config_bytes, write_atomic

# State keys. Dict keys are keyed by ID; blocked_commands and violations are lists.
_KEYS = ("executions", "results", "tests", "findings", "authorizations", "config",
         "blocked_commands", "gates", "reports", "plans", "bundles", "violations", "heads", "worktrees",
         "requirements", "criteria", "assumptions", "risks", "testcases", "links", "fixes", "revalidations")
_LISTS = ("blocked_commands", "violations", "links")
# Derived from evaluation or rendering, or about the work rather than the evidence, so excluded from state_hash.
_UNHASHED = ("gates", "reports", "plans", "heads")
# Keys added after milestone 2. While one is empty it is left out of state_hash, so a ledger that predates the key keeps the hash
# it had (a bundle and a gate.evaluated record that hash). Once it holds something it counts like any other key.
_UNHASHED_WHEN_EMPTY = ("worktrees", "requirements", "criteria", "assumptions", "risks", "testcases", "links",
                         "fixes", "revalidations")


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
        data = event["data"]
        config_hash = data["hash"]
        state["config"]["accepted_hash"] = config_hash
        entry = {"event": event["id"], "kind": kind, "hash": config_hash}
        if data.get("key") == "container.engine":  # the only record that can back the engine: config.container_engine
            entry["container_engine"] = data["new"]
        state["config"]["history"].append(entry)
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
    grant.pop(authority.MAC_FIELD, None)  # a statement about the event, not part of the grant
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


def _worktree_created(state: dict, event: dict) -> None:
    data = event["data"]
    known = state["worktrees"].get(data["path"])
    if known is not None and known["removed"] is None:  # the first record of an open worktree stays
        _violation(state, event, "duplicate_path")
        return
    state["worktrees"][data["path"]] = {"commit": data["commit"], "event": event["id"], "removed": None}


def _worktree_removed(state: dict, event: dict) -> None:
    known = state["worktrees"].get(event["data"]["path"])
    if known is None or known["removed"] is not None:
        _violation(state, event, "unknown_worktree")
        return
    known["removed"] = event["id"]


# Analysis records (§9.1). Each upsert replaces the whole record under its id. What a person decides is kept beside the record,
# never inside what its author wrote: a requirement's ``confirmed`` and an assumption's ``status``, ``confirmed_by`` and
# ``approval``. Those come from signed events, which fold only when their MAC verifies (authority.CONFERRING), and a changed
# statement drops them, so an approval never outlives the words it approved.

def statement_hash(statement: str) -> str:
    """What a signed decision binds: the words approved. A replayed line cannot approve different words."""
    return hash_obj(statement)


def _requirement_upserted(state: dict, event: dict) -> None:
    data = event["data"]
    record = copy.deepcopy(data)
    prior = state["requirements"].get(data["id"])
    if prior is not None and "confirmed" in prior and prior["statement"] == data["statement"]:
        record["confirmed"] = prior["confirmed"]
    state["requirements"][data["id"]] = record


def _requirement_confirmed(state: dict, event: dict) -> None:
    data = event["data"]
    record = state["requirements"].get(data["id"])
    if record is None:
        _violation(state, event, "unknown_requirement")
    elif data.get("statement_hash") != statement_hash(record["statement"]):
        _violation(state, event, "stale_confirmation")  # signed for words the requirement no longer has
    else:
        record["confirmed"] = {"approval": data["approval"], "event": event["id"]}


def _criterion_upserted(state: dict, event: dict) -> None:
    data = event["data"]
    if data["requirement"] not in state["requirements"]:
        _violation(state, event, "unknown_requirement")
        return
    state["criteria"][data["id"]] = copy.deepcopy(data)


def _assumption_upserted(state: dict, event: dict) -> None:
    data = event["data"]
    record = dict(copy.deepcopy(data), status="open")
    prior = state["assumptions"].get(data["id"])
    if prior is not None and prior["statement"] == data["statement"]:
        record.update({name: prior[name] for name in ("status", "confirmed_by", "approval") if name in prior})
    state["assumptions"][data["id"]] = record


def _assumption_decided(state: dict, event: dict) -> None:
    data = event["data"]
    record = state["assumptions"].get(data["id"])
    if record is None:
        _violation(state, event, "unknown_assumption")
    elif data.get("statement_hash") != statement_hash(record["statement"]):
        _violation(state, event, "stale_decision")
    else:
        record.update(status=data["status"], confirmed_by=data["confirmed_by"], approval=data["approval"])


def _stored(key: str) -> Callable[[dict, dict], None]:
    def reduce(state: dict, event: dict) -> None:
        state[key][event["data"]["id"]] = copy.deepcopy(event["data"])
    return reduce


def _trace_linked(state: dict, event: dict) -> None:
    """Append the link, once. Whether source and target exist is not judged here: the commands that record a link check it
    when they record it, and ``trace`` (milestone 3) reports a link whose end is missing as a gap."""
    data = event["data"]
    if not any(link["source"] == data["source"] and link["relation"] == data["relation"] and link["target"] == data["target"]
               for link in state["links"]):
        state["links"].append(dict(copy.deepcopy(data), event=event["id"]))


def _fix_recorded(state: dict, event: dict) -> None:
    data = event["data"]
    if data["id"] in state["fixes"]:  # the first record stays
        _violation(state, event, "duplicate_id")
        return
    if data["base_commit"] == data["fix_commit"]:  # no change was made, so nothing can be shown to have fixed anything
        _violation(state, event, "same_commit")
        return
    if not data["description"].strip():
        _violation(state, event, "invalid_data")
        return
    state["fixes"][data["id"]] = copy.deepcopy(data)
    _queue(state, event)


def _settle_fix_recorded(state: dict, event: dict) -> None:
    """Each finding that can take a fix becomes fix_submitted; the finding remembers which fix, so only that fix's revalidation moves it."""
    data = event["data"]
    for fid in data["findings"]:
        finding = state["findings"].get(fid)
        if finding is None:
            _violation(state, event, "unknown_finding")
        elif "fix_submitted" not in FIX_TRANSITIONS.get(finding["state"], ()):
            _violation(state, event, "invalid_transition")
        else:
            finding.update(state="fix_submitted", state_reason="fix " + data["id"] + " submitted", fix=data["id"])


# Fold-internal: {revalidation event id: what the state held at that event's position says}, decided when the event reduces and
# applied when it settles. fold drops the key before it returns.
_REVALIDATIONS = "_revalidations"


def _revalidation_recorded(state: dict, event: dict) -> None:
    """Trust: the outcome is derived from the executions and results the state holds at this event's position in the fold,
    never from the final state, so a later reclassification or result cannot rewrite a past revalidation. The recorded outcome
    is only a claim: one that differs is a violation and the derived one is used, so writing ``passed`` verifies nothing."""
    data = event["data"]
    fix = state["fixes"].get(data["fix"])
    if fix is None:
        verdict: tuple = ("violation", "unknown_fix")
    else:
        f2p, p2p = (data["f2p"]["base"], data["f2p"]["head"]), (data["p2p"]["base"], data["p2p"]["head"])
        used = set(f2p) | set(p2p)
        view = {"executions": dict(state["executions"]),
                "results": {rid: dict(r) for rid, r in state["results"].items() if r["execution"] in used}}
        for pending in state.get(_SETTLE, []):  # reclassifications recorded before this event, not yet settled
            if pending["type"] == "result.reclassified":
                result = view["results"].get(pending["data"]["result"])
                if result is not None and can_reclassify(result["status"], pending["data"]["new_status"]):
                    result["status"] = pending["data"]["new_status"]
        try:
            regression.check_inputs(view, fix, f2p, p2p)
            verdict = ("derived", regression.derive(view, fix, f2p, p2p, data["defect_tests"]))
        except regression.Refused as refused:
            verdict = ("violation", refused.code)
    state.setdefault(_REVALIDATIONS, {})[event["id"]] = verdict
    _queue(state, event)


def _settle_revalidation_recorded(state: dict, event: dict) -> None:
    data = event["data"]
    if data["id"] in state["revalidations"]:  # also the same line twice: one verdict is kept per event id, fold drops them
        _violation(state, event, "duplicate_id")
        return
    kind, payload = state[_REVALIDATIONS][event["id"]]
    if kind == "violation":
        _violation(state, event, payload)
        return
    fix = state["fixes"][data["fix"]]
    if payload["outcome"] != data["outcome"]:
        _violation(state, event, "outcome_mismatch")
    state["revalidations"][data["id"]] = dict(copy.deepcopy(data), outcome=payload["outcome"], reason=payload["reason"],
                                              fix_commit=fix["fix_commit"], base_commit=fix["base_commit"], event=event["id"])
    new = {"passed": "verified_fixed", "failed": "reopened"}.get(payload["outcome"])
    for fid in fix["findings"]:
        finding = state["findings"].get(fid)
        if new is not None and finding is not None and finding["state"] == "fix_submitted" and finding.get("fix") == fix["id"]:
            finding.update(state=new, state_reason="revalidation %s %s" % (data["id"], payload["outcome"]))


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


def _settle_finding_deduplicated(state: dict, event: dict) -> None:
    data = event["data"]
    dup, keep = state["findings"].get(data["id"]), state["findings"].get(data["duplicate_of"])
    if dup is None or keep is None or data["id"] == data["duplicate_of"]:
        _violation(state, event, "unknown_finding")
    elif "duplicate" not in TRANSITIONS.get(dup["state"], {}) or "duplicate" in (keep["state"],):
        _violation(state, event, "invalid_transition")
    else:
        dup.update(state="duplicate", state_reason="same file, same CWE and overlapping lines as " + data["duplicate_of"],
                   accepted_by=None, expires_at=None, duplicate_of=data["duplicate_of"])
        keep["evidence"] = list(keep["evidence"]) + [e for e in data["evidence_added"] if e not in keep["evidence"]]
        links = keep.setdefault("links", {})
        for name, value in (("duplicates", data["id"]), ("duplicate_sources", data["source_label"])):
            links[name] = list(links.get(name, [])) + [value]


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


def _plan_created(state: dict, event: dict) -> None:
    data = event["data"]
    if data["id"] in state["plans"]:  # the first record stays
        _violation(state, event, "duplicate_id")
        return
    plan = state["plans"][data["id"]] = copy.deepcopy(data)
    for step in plan["steps"]:
        step["hold"] = None  # {"status", "reason", "event"} while the step is blocked or not_run; done is never stored


def _settle_plan_step_updated(state: dict, event: dict) -> None:
    data = event["data"]
    plan = state["plans"].get(data["plan"])
    if plan is None:
        _violation(state, event, "unknown_plan")
        return
    step = next((s for s in plan["steps"] if s["id"] == data["step"]), None)
    if step is None:
        _violation(state, event, "unknown_step")
    elif data["status"] == "pending":
        step["hold"] = None
    else:
        step["hold"] = {"status": data["status"], "reason": data["reason"], "event": event["id"]}


def _bundle_ingested(state: dict, event: dict) -> None:
    data = event["data"]
    if data["id"] in state["bundles"]:  # the first record stays
        _violation(state, event, "duplicate_id")
        return
    state["bundles"][data["id"]] = dict(copy.deepcopy(data), comparisons=[])


def _settle_bundle_compared(state: dict, event: dict) -> None:
    data = event["data"]
    bundle = state["bundles"].get(data["bundle"])
    if bundle is None:
        _violation(state, event, "unknown_bundle")
    elif data["local"] not in state["executions"] or data["remote"] not in bundle["executions"]:
        _violation(state, event, "unknown_execution")
    else:
        bundle["comparisons"].append({"event": event["id"], "local": data["local"], "remote": data["remote"],
                                      "commit": data["commit"], "matching": data["matching"],
                                      "differences": copy.deepcopy(data["differences"])})


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
             "finding.deduplicated": _settle_finding_deduplicated,
             "gate.policy_accepted": _settle_gate_policy_accepted, "plan.step_updated": _settle_plan_step_updated,
             "bundle.compared": _settle_bundle_compared, "fix.recorded": _settle_fix_recorded,
             "revalidation.recorded": _settle_revalidation_recorded}


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
    "finding.deduplicated": _queue,
    "gate.evaluated": _gate_evaluated,
    "report.rendered": _report_rendered,
    "gate.policy_proposed": _gate_policy_proposed,
    "gate.policy_accepted": _gate_policy_accepted,
    "plan.created": _plan_created,
    "plan.step_updated": _queue,
    "bundle.ingested": _bundle_ingested,
    "bundle.compared": _queue,
    "worktree.created": _worktree_created,
    "worktree.removed": _worktree_removed,
    "requirement.upserted": _requirement_upserted,
    "requirement.confirmed": _requirement_confirmed,
    "criterion.upserted": _criterion_upserted,
    "assumption.upserted": _assumption_upserted,
    "assumption.decided": _assumption_decided,
    "risk.upserted": _stored("risks"),
    "testcase.upserted": _stored("testcases"),
    "trace.linked": _trace_linked,
    "fix.recorded": _fix_recorded,
    "revalidation.recorded": _revalidation_recorded,
}


def _forget_revocations(state: dict, grant_ids: Set[str]) -> None:
    """A revocation of a grant the fold withheld names a real grant, so it is no ``unknown_authorization``."""
    for grant_id in grant_ids:
        for _revocation, violation in state.get(_EARLY_REVOCATIONS, {}).pop(grant_id, []):
            state["violations"].remove(violation)


def fold(events: Iterable[dict], backed: Optional[Callable[[dict], bool]] = None) -> dict:
    """Reduce ``events`` to the state. ``backed`` (see ``authority``) decides which approvals count.

    With it, as in ``load``, an event that would confer authority and is not ``backed`` does nothing. Without it, as for an
    imported bundle, every event counts, so the result and its hash do not depend on this machine's key.
    """
    state = empty_state()
    withheld_grants: Set[str] = set()
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
        if backed is not None and event["type"] in authority.CONFERRING and not backed(event):
            if event["type"] == "authorization.granted":
                withheld_grants.add(event["data"]["id"])
            continue
        reducer(state, event)
    _forget_revocations(state, withheld_grants)
    state.pop(_EARLY_REVOCATIONS, None)  # its violations stay: no grant in the ledger holds those ids
    _settle(state)
    state.pop(_REVALIDATIONS, None)
    return state


def load(ws: Workspace) -> dict:
    state = fold(ledger.iter_all_events(ws), backed=authority.event_verifier(ws))
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
    return hash_obj({k: v for k, v in state.items()
                     if k not in _UNHASHED and not (k in _UNHASHED_WHEN_EMPTY and not v)})
