"""Requirements, acceptance criteria, assumptions, risks and test cases (§9.1), recorded in the ledger.

``add`` records one record, new (no ``id``: the kernel allocates it under the workspace lock) or changed (an ``id`` that
exists: the given fields replace the stored ones). ``add_many`` records a list, all or nothing. Every record is validated
against its event schema (``requirement.upserted`` and the like, over the definitions in ``records.schema.json``) before anything is written.

Links are trace events and nothing else (Phase 2 §9.2). A record holds no list of what it is linked to:

* a test case's ``links`` (the criteria it verifies) is input only. Each becomes one ``trace.linked`` event
  ``{source: AC, relation: verified_by, target: TC}``. Only criteria are accepted.
* a risk's ``mitigations`` (test cases) is input only. Each becomes ``{source: RSK, relation: mitigated_by, target: TC}``.

The targets must exist when the link is recorded, so the creation order is requirement, criterion, test case, risk. A risk
needs ``mitigations`` (or links it already has) or an ``accepted_gap`` that names who accepted it. A link already in the
state is not recorded again. Later links, and the scan and matrix over them, belong to ``trace``.

What a person decides is not a field of the record. ``confirm_requirement`` and ``decide_assumption`` record signed
events (``authority.EVENTS``): without a MAC from this machine's key they fold to nothing. Editing a requirement's or an
assumption's ``statement`` drops the decision (see ``state``).
"""
from __future__ import annotations

import copy
from typing import Dict, List, Set, Tuple

from . import ids, ledger, schema, state
from .errors import QaError
from .workspace import Workspace

# QaError codes raised here: records_invalid, unknown_record, blank_approval, usage; plus ledger.append's.

# kind: (state key, event type, ID prefix, unused)
_KINDS = {
    "requirement": ("requirements", "requirement.upserted", "REQ", "requirement"),
    "criterion": ("criteria", "criterion.upserted", "AC", "criterion"),
    "assumption": ("assumptions", "assumption.upserted", "ASM", "assumption"),
    "risk": ("risks", "risk.upserted", "RSK", "risk"),
    "testcase": ("testcases", "testcase.upserted", "TC", "testcase"),
}
# What the state adds to a record that its author did not write.
_DERIVED = {"requirement": ("confirmed",), "assumption": ("status", "confirmed_by", "approval")}
# Input that is not stored on the record: it becomes trace.linked events.
_LINK_INPUT = {"testcase": "links", "risk": "mitigations"}
_DEFAULTS = {"requirement": {"quality_issues": []}, "risk": {"scale": "1-5"}, "testcase": {"automation": []}}
DECISIONS = ("confirmed", "rejected")


def _invalid(message: str, **details: object) -> QaError:
    return QaError("records_invalid", message, **details)


def _kind(kind: str) -> Tuple[str, str, str, str]:
    if kind not in _KINDS:
        raise QaError("usage", "no such kind of record", kind=kind, kinds=sorted(_KINDS))
    return _KINDS[kind]


def _number(identifier: str) -> int:
    return int(identifier.rsplit("-", 1)[1].split(".")[0])


def _order(identifier: str) -> Tuple[int, int, str]:
    split = ids.split_ac_id(identifier)
    return (_number(identifier), split[1] if split else 0, identifier)


def list_records(st: dict, kind: str) -> List[dict]:
    """The records of one kind, ordered by ID, as the state holds them (with what the state adds to them)."""
    key = _kind(kind)[0]
    return [copy.deepcopy(st[key][i]) for i in sorted(st[key], key=_order)]


# --------------------------------------------------------------------------- preparing

class _Shadow:
    """What the state holds, plus what an unwritten batch would add, so a batch is judged as one."""

    def __init__(self, st: dict) -> None:
        self.records: Dict[str, Dict[str, dict]] = {kind: copy.deepcopy(st[spec[0]]) for kind, spec in _KINDS.items()}
        self.links: Set[Tuple[str, str, str]] = {(l["source"], l["relation"], l["target"]) for l in st["links"]}
        self.counters: Dict[str, int] = {}

    def allocate(self, ws: Workspace, kind: str, data: dict) -> str:
        prefix = _KINDS[kind][2]
        if kind == "criterion":
            requirement = data["requirement"]
            ordinals = [ids.split_ac_id(i)[1] for i in self.records["criterion"] if ids.split_ac_id(i)[0] == requirement]
            return ids.ac_id(requirement, max(ordinals, default=0) + 1)
        if prefix not in self.counters:
            self.counters[prefix] = _number(ledger.next_human_id(ws, prefix))
        number = self.counters[prefix]
        self.counters[prefix] += 1
        return ledger.format_human_id(prefix, number)


def _refuse_derived(kind: str, data: dict) -> None:
    named = [name for name in _DERIVED.get(kind, ()) if name in data]
    if named:
        raise _invalid("these fields come from a recorded decision, not from the record", fields=named)


def _links(shadow: _Shadow, kind: str, identifier: str, given: object) -> List[Tuple[str, str, str]]:
    """The links ``given`` asks for, each checked against the records that exist, minus those already recorded."""
    if given is None:
        return []
    if not isinstance(given, list) or not all(isinstance(item, str) for item in given):
        raise _invalid("%s is a list of IDs" % _LINK_INPUT[kind])
    wanted: List[Tuple[str, str, str]] = []
    for target in given:
        if kind == "testcase":  # a test case verifies criteria: the link runs from the criterion
            if target.startswith("RSK-"):
                raise _invalid("a test case links to criteria only; the risk names the test case in its mitigations", target=target)
            if target not in shadow.records["criterion"]:
                raise QaError("unknown_record", "no such acceptance criterion; record it first", id=target)
            link = (target, "verified_by", identifier)
        else:
            if target not in shadow.records["testcase"]:
                raise QaError("unknown_record", "no such test case; record it first", id=target)
            link = (identifier, "mitigated_by", target)
        if link not in shadow.links and link not in wanted:
            wanted.append(link)
    return wanted


def _prepare(ws: Workspace, shadow: _Shadow, kind: str, data: object) -> Tuple[str, str, dict, List[Tuple[str, str, str]]]:
    """``(id, event type, record, links)`` for one input, applied to the shadow; raises without having written anything."""
    if not isinstance(data, dict):
        raise _invalid("a record is a JSON object")
    _, event_type, prefix, _ = _KINDS[kind]
    _refuse_derived(kind, data)
    given = dict(data)
    link_field = _LINK_INPUT.get(kind)
    link_input = given.pop(link_field, None) if link_field else None
    identifier = given.pop("id", None)
    if identifier is None:
        record = dict(_DEFAULTS.get(kind, {}), **given)
        if kind == "criterion" and not isinstance(record.get("requirement"), str):
            raise _invalid("an acceptance criterion names its requirement")
        if kind == "criterion" and record["requirement"] not in shadow.records["requirement"]:
            raise QaError("unknown_record", "no such requirement; record it first", id=record["requirement"])
        identifier = shadow.allocate(ws, kind, record)
        created = True
    else:
        if not isinstance(identifier, str) or not identifier.startswith(prefix + "-"):
            raise _invalid("the id is not a %s id" % kind, id=identifier)
        existing = shadow.records[kind].get(identifier)
        if existing is None:
            raise QaError("unknown_record", "no such %s to change" % kind, id=identifier)
        record = {name: value for name, value in existing.items() if name not in _DERIVED.get(kind, ())}
        record.update(given)
        if kind == "criterion" and record["requirement"] != existing["requirement"]:
            raise _invalid("an acceptance criterion keeps its requirement", id=identifier)
        created = False
    record["id"] = identifier
    problems = schema.errors(record, schema.load_schema("events/%s.schema.json" % event_type), base="events/%s.schema.json" % event_type)
    if problems:
        raise _invalid("the %s does not fit the record schema" % kind, id=identifier if not created else None, errors=problems)
    links = _links(shadow, kind, identifier, link_input)
    if kind == "risk" and "accepted_gap" not in record:
        held = any(l[0] == identifier and l[1] == "mitigated_by" for l in shadow.links)
        if not links and not held:
            raise _invalid("a risk needs mitigations (test cases that exist) or an accepted_gap with a reason and who accepted it")
    shadow.records[kind][identifier] = record
    shadow.links.update(links)
    return identifier, event_type, record, links


# --------------------------------------------------------------------------- recording

def add_many(ws: Workspace, actor: ledger.Actor, kind: str, items: List[dict]) -> List[dict]:
    """Record every item, or none: one that fails leaves the ledger as it was. Returns ``{id, event, created}`` per item."""
    _kind(kind)
    if not isinstance(items, list) or not items:
        raise _invalid("expected a non-empty list of records")
    with ws.lock():
        shadow = _Shadow(state.load(ws))
        prepared = []
        for position, item in enumerate(items):
            try:
                prepared.append(_prepare(ws, shadow, kind, item))
            except QaError as exc:
                if len(items) > 1:
                    exc.details["entry"] = position
                raise
        done = []
        known = set(state.load(ws)[_KINDS[kind][0]])
        for identifier, event_type, record, links in prepared:
            event = ledger.append(ws, actor, event_type, record)
            for source, relation, target in links:
                ledger.append(ws, actor, "trace.linked", {"source": source, "relation": relation, "target": target})
            done.append({"id": identifier, "event": event["id"], "created": identifier not in known})
            known.add(identifier)
        return done


def add(ws: Workspace, actor: ledger.Actor, kind: str, data: dict) -> dict:
    """Record one record: ``{id, event, created}``. With no ``id`` in ``data`` it is new; with one it changes that record."""
    return add_many(ws, actor, kind, [data])[0]


# --------------------------------------------------------------------------- decisions

def _decision_approval(approval: str) -> str:
    if not isinstance(approval, str) or not approval.strip():
        raise QaError("blank_approval", "a decision needs the user's approval, in their words")
    return approval


def confirm_requirement(ws: Workspace, actor: ledger.Actor, req_id: str, approval: str) -> dict:
    """Record the user's confirmation of a requirement (a signed event). Returns ``{id, event}``."""
    _decision_approval(approval)
    with ws.lock():
        if req_id not in state.load(ws)["requirements"]:
            raise QaError("unknown_record", "no such requirement", id=req_id)
        event = ledger.append(ws, actor, "requirement.confirmed", {"id": req_id, "approval": approval})
    return {"id": req_id, "event": event["id"]}


def decide_assumption(ws: Workspace, actor: ledger.Actor, asm_id: str, status: str, approval: str) -> dict:
    """Record the user's decision on an assumption, ``confirmed`` or ``rejected`` (a signed event). Returns ``{id, event, status}``."""
    if status not in DECISIONS:
        raise _invalid("an assumption is decided as confirmed or rejected", status=status)
    _decision_approval(approval)
    with ws.lock():
        if asm_id not in state.load(ws)["assumptions"]:
            raise QaError("unknown_record", "no such assumption", id=asm_id)
        event = ledger.append(ws, actor, "assumption.decided",
                              {"id": asm_id, "status": status, "confirmed_by": "user", "approval": approval})
    return {"id": asm_id, "event": event["id"], "status": status}
