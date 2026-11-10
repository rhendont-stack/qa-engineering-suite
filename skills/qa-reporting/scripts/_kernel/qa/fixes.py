"""Fix records and their revalidation (§6.4 G, §9.1): the only way a finding reaches ``fix_submitted``, ``verified_fixed`` or ``reopened``.

``add`` records ``fix.recorded`` (``FIX-nnn``: the findings, the commit the defect was seen at, the fixing commit, a
description) and one ``addressed_by`` link per finding. Each finding that is open or reopened becomes ``fix_submitted``.

``revalidate`` takes a FAIL_TO_PASS pair and a PASS_TO_PASS pair of executions (base, then head) and the defect tests, and
records ``revalidation.recorded`` (``RVL-nnn``) with the outcome ``regression.derive`` computes, plus a ``validated_by``
link. ``passed`` moves the fix's findings to ``verified_fixed``, ``failed`` to ``reopened``, and ``inconclusive`` leaves them
``fix_submitted``. Trust: the fold derives the outcome again from the state and uses its own answer, so the recorded one is
a claim, never the source (see ``state._settle_revalidation_recorded``). Nothing here is signed: a revalidation holds
no approval, only a computation over recorded runs.

Executions at a commit other than the fix's, or in an attempt chain that crosses commits, are refused and nothing is recorded.
A run on a dirty tree gives ``inconclusive``.
"""
from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

from . import ledger, regression, schema, state
from .errors import QaError
from .vocab import FIX_TRANSITIONS
from .workspace import Workspace

# QaError codes raised here: fix_invalid, unknown_finding, invalid_transition, unknown_fix, unknown_execution,
# revalidation_invalid, revalidation_refused; plus ledger.append's.

_FIX_FIELDS = ("findings", "base_commit", "fix_commit", "description", "ref")


def _link(ws: Workspace, actor: ledger.Actor, source: str, relation: str, target: str) -> dict:
    return ledger.append(ws, actor, "trace.linked", {"source": source, "relation": relation, "target": target})


def add(ws: Workspace, actor: ledger.Actor, data: dict) -> dict:
    """Record a fix and return its data with ``event``. Refused, with nothing recorded, unless every finding is open or reopened."""
    if not isinstance(data, dict):
        raise QaError("fix_invalid", "a fix is a JSON object")
    unknown = sorted(set(data) - set(_FIX_FIELDS))
    if unknown:
        raise QaError("fix_invalid", "the kernel allocates the id; these fields are not part of a fix", fields=unknown)
    candidate = dict(data, id="FIX-000")
    problems = schema.errors(candidate, schema.load_schema("events/fix.recorded.schema.json"), base="events/fix.recorded.schema.json")
    description = data.get("description")
    if not problems and not description.strip():
        problems = ["description: must not be blank"]
    if problems:
        raise QaError("fix_invalid", "the fix does not fit its schema", errors=problems)
    with ws.lock():
        st = state.load(ws)
        for fid in data["findings"]:
            found = st["findings"].get(fid)
            if found is None:
                raise QaError("unknown_finding", "no such finding", id=fid)
            if "fix_submitted" not in FIX_TRANSITIONS.get(found["state"], ()):
                raise QaError("invalid_transition", "only an open or reopened finding can take a fix", id=fid, state=found["state"])
        record = dict(data, id=ledger.next_human_id(ws, "FIX"))
        event = ledger.append(ws, actor, "fix.recorded", record)
        for fid in record["findings"]:
            _link(ws, actor, fid, "addressed_by", record["id"])
    return dict(record, event=event["id"])


def _pair(value: object, name: str) -> Tuple[str, str]:
    if (not isinstance(value, (tuple, list)) or len(value) != 2 or not all(isinstance(v, str) and v for v in value)):
        raise QaError("revalidation_invalid", "%s is a pair of execution ids, base then head" % name, value=value)
    return value[0], value[1]


def revalidate(ws: Workspace, actor: ledger.Actor, fix_id: str, *, f2p: Sequence[str], p2p: Sequence[str],
               defect_tests: List[str]) -> dict:
    """Record the revalidation of ``fix_id`` and return ``{id, fix, outcome, reason, findings, event}``.

    ``findings`` maps each finding of the fix to its state afterwards.
    """
    pair_f2p, pair_p2p = _pair(f2p, "f2p"), _pair(p2p, "p2p")
    if (not isinstance(defect_tests, (list, tuple)) or not defect_tests or not all(isinstance(t, str) and t for t in defect_tests)
            or len(set(defect_tests)) != len(defect_tests)):
        raise QaError("revalidation_invalid", "name at least one defect test, each once", defect_tests=defect_tests)
    tests = list(defect_tests)
    with ws.lock():
        st = state.load(ws)
        fix = st["fixes"].get(fix_id)
        if fix is None:
            raise QaError("unknown_fix", "no such fix", id=fix_id)
        try:
            regression.check_inputs(st, fix, pair_f2p, pair_p2p)
        except regression.Refused as refused:
            if refused.code == "unknown_execution":
                raise QaError("unknown_execution", str(refused), id=fix_id)
            raise QaError("revalidation_refused", str(refused), why=refused.why, fix=fix_id)
        derived = regression.derive(st, fix, pair_f2p, pair_p2p, tests)
        record = {"id": ledger.next_human_id(ws, "RVL"), "fix": fix_id, "f2p": {"base": pair_f2p[0], "head": pair_f2p[1]},
                  "p2p": {"base": pair_p2p[0], "head": pair_p2p[1]}, "defect_tests": tests,
                  "outcome": derived["outcome"], "reason": derived["reason"]}
        event = ledger.append(ws, actor, "revalidation.recorded", record)
        _link(ws, actor, fix_id, "validated_by", record["id"])
        after = state.load(ws)
    states: Dict[str, str] = {fid: after["findings"][fid]["state"] for fid in fix["findings"] if fid in after["findings"]}
    return {"id": record["id"], "fix": fix_id, "outcome": derived["outcome"], "reason": derived["reason"], "findings": states,
            "event": event["id"]}
