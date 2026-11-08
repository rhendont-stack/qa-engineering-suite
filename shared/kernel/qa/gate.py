"""The release gate: profiles, criteria and the verdict (§11).

``load_policy`` applies the overrides the user accepted to a profile file (§11.1). ``evaluate_criterion`` gives one
criterion one of §11.2's four outcomes, and ``verdict`` follows from them mechanically. ``evaluate`` does both against
a fresh fold and records the result with what recomputing it needs: the policy hash, the state hash, and the time,
because an accepted risk's expiry depends on it.

Never MET by default: a type this kernel has nothing to read for is NO_EVIDENCE and names what it needs (deviation 17).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from . import KERNEL_VERSION, config, gitinfo, ids, integrity, ledger, resource_dir, schema, state, vocab
from .canonical import hash_obj, loads
from .errors import QaError
from .findings import SEVERITIES
from .workspace import Workspace, write_atomic

# QaError codes raised here: unknown_profile, invalid_target, config_invalid, config_drift, unknown_criterion,
# invalid_override, blank_rationale, approval_required, unknown_proposal, already_accepted; plus ledger.append's.

_POLICY_SCHEMA = "gate-policy.schema.json"
_PARAMS = {"$ref": _POLICY_SCHEMA + "#/$defs/params"}
_OVERRIDE = {"$ref": "events/gate.policy_proposed.schema.json#/$defs/override"}
_COMMIT = re.compile(r"[0-9a-f]{40}")

_MET, _NOT_MET, _NO_EVIDENCE, _NOT_APPLICABLE = "MET", "NOT_MET", "NO_EVIDENCE", "NOT_APPLICABLE"
_NOT_OPTED_IN = "Project has not opted in (config.gate.overrides)"
_DISABLED = "Disabled by config.gate.overrides (%s)"
_SOURCE_CHECKOUT = "kernel manifest applies to vendored copies (milestone 2)"

_STRENGTHS = ("claimed", "attested", "exit_code", "structured")
_INDEPENDENCE = ("I0", "I1", "I2", "I3")
_SUITE_ACTIVITY = "test-execution-local"
_UNRESOLVED_STATES = ("open", "reopened", "fix_submitted")
_NEEDS = {
    "requirements_verified": ("traceability", 3), "no_blocked_critical": ("traceability", 3),
    "mutation_score_min": ("mutation ingestion", 3),
    "security_scans_executed": ("scan and performance skills", 4),
    "performance_objectives_met": ("scan and performance skills", 4),
    "migrations_validated": ("database skills", 4), "rollback_validated": ("database skills", 4),
}

_Outcome = Tuple[str, str, List[str]]  # outcome, detail, evidence


def _blank(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def _in_id_order(names: Iterable[str]) -> List[str]:
    """By number: FND-1000 comes after FND-999, as with the gate anchors (T14-R17)."""
    return sorted(names, key=lambda name: (len(name), name))


# --------------------------------------------------------------------------- policy

def _read_policy(profile: str) -> dict:
    doc = loads((resource_dir("policies") / ("gate.%s.json" % profile)).read_bytes())
    schema.validate(doc, _POLICY_SCHEMA)
    return doc


def _off_justification(criterion: dict, override: dict) -> Optional[str]:
    """Why the criterion does not evaluate at all, or None when it does."""
    if override.get("enabled") is False:
        return _DISABLED % override.get("accepted_in")
    if criterion["opt_in"] and override.get("enabled") is not True:
        return _NOT_OPTED_IN
    return None


def _effective_criterion(criterion: dict, override: dict) -> dict:
    """``criterion`` with ``override`` applied. A config accepted as it stands can hold what ``propose`` refuses."""
    params = {**criterion["params"], **override.get("params", {})}
    errors = schema.errors({name: params[name] for name in criterion["params"]}, _PARAMS)
    if "not_applicable" in override and _blank(override["not_applicable"]):
        errors.append("not_applicable is blank: §11.2 requires a recorded justification")
    if errors:
        raise QaError("config_invalid", "an accepted gate override cannot be applied", criterion=criterion["id"], errors=errors)
    effective = dict(criterion, params=params, blocking=override.get("blocking", criterion["blocking"]))
    if override:
        effective["accepted_in"] = override.get("accepted_in")
    off = _off_justification(criterion, override)
    if off is not None:
        effective.update(blocking=False, not_applicable=off)
    elif "not_applicable" in override:
        effective["not_applicable"] = override["not_applicable"]
    return effective


def load_policy(profile: str, cfg: dict) -> Tuple[dict, str]:
    """The profile with the accepted overrides applied, and the hash of that. No criterion is ever dropped (§11.2)."""
    if profile not in vocab.PROFILES:
        raise QaError("unknown_profile", "no such gate profile", profile=profile)
    overrides = cfg["gate"]["overrides"]
    policy = _read_policy(profile)
    effective = {"profile": policy["profile"],
                 "criteria": [_effective_criterion(c, overrides.get(c["id"], {})) for c in policy["criteria"]]}
    return effective, hash_obj(effective)


# --------------------------------------------------------------------------- criteria

@dataclass(frozen=True)
class GateContext:
    """What a criterion reads besides the state."""
    target_commit: Optional[str]
    independence: str
    integrity_problems: list
    kernel: dict
    now: datetime
    diff_hash: Optional[str] = None  # set only when the working tree itself is what is evaluated (T16-R5)
    git_limitation: Optional[str] = None  # why there is no target commit (T16-R4)


def _rank(order: Tuple[str, ...], value: Optional[str]) -> int:
    return order.index(value) if value in order else -1


def _expired(finding: dict, now: datetime) -> bool:
    """An acceptance with no readable expiry has none to honor (§10.6), so it counts as expired."""
    try:
        return ids.parse_ts(finding.get("expires_at")) <= now
    except QaError:
        return True


def _unresolved(finding: dict, now: datetime) -> bool:
    """The one definition of "unresolved" (§10.6, §11.1). A ``suspected`` finding is unresolved, not ignored."""
    if finding["verification_status"] == "false_positive":
        return False
    return finding["state"] in _UNRESOLVED_STATES or (finding["state"] == "accepted_risk" and _expired(finding, now))


def _no_unresolved_findings(c: dict, st: dict, ctx: GateContext) -> _Outcome:
    minimum = c["params"]["min_severity"]
    floor = SEVERITIES.index(minimum)
    unresolved = _in_id_order(fid for fid, finding in st["findings"].items()
                              if SEVERITIES.index(finding["severity"]) >= floor and _unresolved(finding, ctx.now))
    if not unresolved:
        return _MET, "no unresolved finding at %s or above" % minimum, []

    def entry(fid: str) -> str:
        finding = st["findings"][fid]
        where = "accepted_risk expired" if finding["state"] == "accepted_risk" else finding["state"]
        return "%s (%s, %s)" % (fid, finding["severity"], where)

    return _NOT_MET, "; ".join(entry(fid) for fid in unresolved), unresolved


def _is_suite_run(record: dict) -> bool:
    """A clean scan is not a suite run, and a dirty scan does not fail one."""
    return record["activity"] == _SUITE_ACTIVITY and (record["report"] is None or record["report"]["kind"] != "alerts")


def _on_target(record: dict, target: Optional[str]) -> bool:
    return record["git"] is not None and record["git"]["commit"] == target


def _on_this_tree(record: dict, ctx: GateContext) -> bool:
    """A run followed by another uncommitted edit is evidence about different code (T16-R5)."""
    return ctx.diff_hash is None or record["git"]["diff_hash"] == ctx.diff_hash


def _about_target(record: dict, ctx: GateContext) -> bool:
    return _is_suite_run(record) and _on_target(record, ctx.target_commit) and _on_this_tree(record, ctx)


def _strong_enough(record: dict, minimum: Optional[str]) -> bool:
    if minimum is None or record["status"] not in ("passed", "failed"):
        return True
    return _rank(_STRENGTHS, record["strength"]) >= _rank(_STRENGTHS, minimum)


def _described(record: dict) -> str:
    reason = " (%s)" % record["reason"] if record["reason"] else ""
    return "%s %s%s" % (record["id"], record["status"], reason)


def _none_qualified(c: dict, ctx: GateContext) -> str:
    wanted = ["scope %s" % c["params"]["scope"]]
    if c["min_strength"] is not None:
        wanted.append("strength >= %s" % c["min_strength"])
    if c["params"].get("clean_tree"):
        wanted.append("clean tree")
    return "no execution qualified for commit %s (%s)" % (ctx.target_commit, ", ".join(wanted))


def _suite_executed(c: dict, st: dict, ctx: GateContext) -> _Outcome:
    if ctx.target_commit is None:
        return _NO_EVIDENCE, "no target commit (%s)" % ctx.git_limitation, []
    params = c["params"]
    # A rerun has no say: §8.3 keeps a failed test failed for gating purposes.
    runs = [record for record in st["executions"].values()
            if _is_suite_run(record) and record["attempt_of"] is None and _on_target(record, ctx.target_commit)
            and not (params.get("clean_tree") and record["git"]["dirty"])
            and (params["scope"] == "targeted" or record["scope"] == "full")
            and _strong_enough(record, c["min_strength"])]
    qualifying = [record for record in runs if _on_this_tree(record, ctx)]
    if not qualifying:
        if runs:
            return _NO_EVIDENCE, "the working tree changed since the execution %s" % runs[-1]["id"], []
        return _NO_EVIDENCE, _none_qualified(c, ctx), []
    latest = qualifying[-1]
    detail, evidence = _described(latest), [latest["id"]]
    if latest["status"] == "passed":
        return _MET, detail, evidence
    if latest["status"] != "failed":
        return _NO_EVIDENCE, detail, evidence
    if params.get("require_passed", True):
        return _NOT_MET, detail, evidence
    return _MET, detail + "; a pass is not required", evidence


def _flaky_rate_max(c: dict, st: dict, ctx: GateContext) -> _Outcome:
    limit = c["params"]["max_ratio"]
    executed = flaky = 0
    roots: Set[str] = set()
    for chains in st["tests"].values():
        counted = {root: entry for root, entry in chains.items()
                   if entry["attempts"] > 0 and _about_target(st["executions"][root], ctx)}
        flaky_roots = [root for root, entry in counted.items() if entry["flaky"]]
        executed += bool(counted)
        flaky += bool(flaky_roots)
        roots.update(flaky_roots)
    if not executed:
        return _NO_EVIDENCE, "no executed tests on the target commit", []
    ratio = flaky / executed
    outcome, compared = (_MET, "<=") if ratio <= limit else (_NOT_MET, ">")
    return outcome, "%d/%d flaky (%g %s %g)" % (flaky, executed, ratio, compared, limit), sorted(roots)


def _test_integrity(c: dict, st: dict, ctx: GateContext) -> _Outcome:
    kinds = Counter(problem.kind for problem in ctx.integrity_problems)
    findings = _in_id_order(fid for fid, finding in st["findings"].items()
                            if finding["category"] == "test-quality" and _unresolved(finding, ctx.now))
    status = ctx.kernel["status"]
    source_checkout = status == "absent" and ctx.kernel["source_checkout"]
    failures = []
    if kinds:
        failures.append("integrity problems: " + ", ".join("%d %s" % (kinds[kind], kind) for kind in sorted(kinds)))
    if findings:
        failures.append("unresolved test-quality findings: " + ", ".join(findings))
    if status != "ok" and not source_checkout:
        failures.append("kernel %s" % status)
    if failures:
        return _NOT_MET, "; ".join(failures), findings
    if source_checkout:
        return _MET, _SOURCE_CHECKOUT, []
    return _MET, "no integrity problems, no unresolved test-quality findings, kernel ok", []


def _independence_min(c: dict, st: dict, ctx: GateContext) -> _Outcome:
    minimum = c["params"]["min"]
    if _rank(_INDEPENDENCE, ctx.independence) >= _rank(_INDEPENDENCE, minimum):
        return _MET, "%s >= %s" % (ctx.independence, minimum), []
    return _NOT_MET, "%s < %s" % (ctx.independence, minimum), []


_EVALUATORS: Dict[str, Callable[[dict, dict, GateContext], _Outcome]] = {
    "no_unresolved_findings": _no_unresolved_findings, "suite_executed": _suite_executed, "flaky_rate_max": _flaky_rate_max,
    "test_integrity": _test_integrity, "independence_min": _independence_min,
}


def evaluate_criterion(c: dict, st: dict, ctx: GateContext) -> dict:
    result = {"id": c["id"], "type": c["type"], "blocking": c["blocking"]}
    if "not_applicable" in c:  # never inferred: load_policy put an accepted or a stated justification here
        return dict(result, outcome=_NOT_APPLICABLE, detail=c["not_applicable"], evidence=[], justification=c["not_applicable"])
    evaluator = _EVALUATORS.get(c["type"])
    if evaluator is None:
        capability, milestone = _NEEDS[c["type"]]
        detail = "not evaluable by kernel %s (needs %s, milestone %d)" % (KERNEL_VERSION, capability, milestone)
        return dict(result, outcome=_NO_EVIDENCE, detail=detail, evidence=[])
    outcome, detail, evidence = evaluator(c, st, ctx)
    return dict(result, outcome=outcome, detail=detail, evidence=evidence)


def verdict(criteria: List[dict], accepted_risks: List[str]) -> str:
    """§11.2 rules 1-4."""
    blocking = [c["outcome"] for c in criteria if c["blocking"]]
    if _NOT_MET in blocking:
        return "NOT_READY"
    if _NO_EVIDENCE in blocking:
        return "INSUFFICIENT_EVIDENCE"
    if accepted_risks or any(c["outcome"] == _NOT_MET for c in criteria):
        return "READY_WITH_CONDITIONS"
    return "READY"


# --------------------------------------------------------------------------- evaluating

def _accepted_risks(st: dict, now: datetime) -> List[str]:
    return _in_id_order(fid for fid, finding in st["findings"].items()
                        if finding["state"] == "accepted_risk" and not _expired(finding, now)
                        and finding["verification_status"] != "false_positive")


def _conditions(criteria: List[dict], risks: List[str], st: dict) -> List[str]:
    advisory = ["%s: %s" % (c["id"], c["detail"]) for c in criteria if not c["blocking"] and c["outcome"] == _NOT_MET]
    return advisory + ["%s accepted_risk until %s" % (fid, st["findings"][fid]["expires_at"]) for fid in risks]


def _refuse_drift(ws: Workspace, st: dict) -> None:
    """§11.1: an override applies only after you accept it, so nothing goes on from a file edited by hand."""
    if config.current_hash(ws) != config.accepted_hash(st):
        raise QaError("config_drift", "config.json was edited outside the CLI; accept it first")


def _context(ws: Workspace, st: dict, actor: ledger.Actor, target_commit: Optional[str]) -> GateContext:
    diff_hash = limitation = None
    if target_commit is None:
        facts, limitation = gitinfo.git_info(ws.root)
        if facts is not None:
            target_commit, diff_hash = facts["commit"], facts["diff_hash"]
    return GateContext(target_commit, actor.independence, integrity.problems(ws, st), integrity.kernel_integrity(),
                       ids.now_utc(), diff_hash, limitation)


def evaluate(ws: Workspace, actor: ledger.Actor, *, profile: Optional[str] = None, target_commit: Optional[str] = None) -> dict:
    """Evaluate a profile against the ledger as it is now, record ``gate.evaluated`` and return that event."""
    if target_commit is not None and not (isinstance(target_commit, str) and _COMMIT.fullmatch(target_commit)):
        raise QaError("invalid_target", "a target commit is the full 40-character hash", target=target_commit)
    with ws.lock():
        st = state.load(ws)
        cfg = config.load_config(ws)
        policy, policy_hash = load_policy(cfg["gate"]["profile"] if profile is None else profile, cfg)
        _refuse_drift(ws, st)
        ctx = _context(ws, st, actor, target_commit)
        criteria = [evaluate_criterion(c, st, ctx) for c in policy["criteria"]]
        risks = _accepted_risks(st, ctx.now)
        return ledger.append(ws, actor, "gate.evaluated", {
            "id": ledger.next_human_id(ws, "GATE"), "profile": policy["profile"], "policy_hash": policy_hash,
            "state_hash": state.state_hash(st), "target": {"commit": ctx.target_commit}, "criteria": criteria,
            "independence": actor.independence, "verdict": verdict(criteria, risks),
            "conditions": _conditions(criteria, risks, st), "heads": ledger.heads(ws), "evaluated_at": ids.format_ts(ctx.now),
        })


# --------------------------------------------------------------------------- overrides

def _declared_params(criterion_id: object) -> Set[str]:
    """The parameters the criterion declares in any profile; refuses an ID in no profile."""
    found = [c["params"] for profile in vocab.PROFILES for c in _read_policy(profile)["criteria"] if c["id"] == criterion_id]
    if not found:
        raise QaError("unknown_criterion", "no profile has such a criterion", criterion=criterion_id)
    return set().union(*found)


def propose(ws: Workspace, actor: ledger.Actor, criterion_id: str, override: dict, rationale: str) -> dict:
    """Record a proposed override. It changes nothing until ``accept`` (§11.1)."""
    declared = _declared_params(criterion_id)
    errors = schema.errors(override, _OVERRIDE)
    if not errors:
        undeclared = sorted(set(override.get("params", {})) - declared)
        errors = ["params.%s is not a parameter of %s" % (name, criterion_id) for name in undeclared]
    if errors:
        raise QaError("invalid_override", "the override cannot be proposed", criterion=criterion_id, errors=errors)
    if _blank(rationale):
        raise QaError("blank_rationale", "a proposed override needs a rationale", criterion=criterion_id)
    return ledger.append(ws, actor, "gate.policy_proposed",
                         {"criterion": criterion_id, "override": override, "rationale": rationale})


def accept(ws: Workspace, actor: ledger.Actor, proposal_event_id: str, approval: str) -> dict:
    """Write a proposed override into config.json and record the user's approval, so that edit is not drift."""
    if _blank(approval):
        raise QaError("approval_required", "accepting a gate override needs the user's approval quote")
    with ws.lock():
        st = state.load(ws)
        _refuse_drift(ws, st)
        history = st["config"]["history"]
        proposal = next((h for h in history if h["kind"] == "gate_policy_proposed" and h["event"] == proposal_event_id), None)
        if proposal is None:
            raise QaError("unknown_proposal", "no such gate policy proposal", proposal=proposal_event_id)
        if any(h["kind"] == "gate_policy_accepted" and h["proposal"] == proposal_event_id for h in history):
            raise QaError("already_accepted", "this proposal was already accepted", proposal=proposal_event_id)
        cfg = config.load_config(ws)
        before = ws.path("config.json").read_bytes()
        cfg["gate"]["overrides"][proposal["criterion"]] = dict(proposal["override"], accepted_in=proposal_event_id)
        new_hash = config.write_config(ws, cfg)
        try:
            return ledger.append(ws, actor, "gate.policy_accepted",
                                 {"proposal": proposal_event_id, "approval": approval, "config_hash": new_hash})
        except BaseException:
            write_atomic(ws.path("config.json"), before)
            raise
