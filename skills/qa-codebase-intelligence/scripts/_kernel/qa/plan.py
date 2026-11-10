"""Plans: steps that complete only through recorded outputs (§7.3).

A plan is a kernel record. ``create`` freezes the target commit into it and into each expected output. ``status``
evaluates every step's ``expected_outputs`` against the ledger as it stands, so a step is ``done`` only when its outputs
exist at that commit, and nobody can say it is: ``update`` records a hold (``blocked`` or ``not_run``, each with its
reason, or ``pending`` to release one) and refuses ``done``. ``done`` is computed here and never stored.

A step's status, first match:

1. ``not_run``: the user descoped it. Evidence arriving later does not undo that decision.
2. ``done``: every expected output is met. Evidence overrules a hold of ``blocked``.
3. ``blocked``: a recorded hold, or its skill is not installed (§15.3), with a ``remedy`` naming ``/plugin install``.
4. ``pending``.

An output kind counts only at the plan's commit. What each kind needs:

* ``execution``: ``min_count`` executions by ``skill`` that ran (not blocked, not not_run) and left evidence.
* ``result_set``: ``min_count`` results of such executions, none with a status in ``status_not_in``.
* ``artifact``: the file ``path`` under ``.qa/`` (a regular file, no link on the way) validates against ``schema`` and
  names the plan's commit in its ``commit`` field.
* ``gate``: ``min_count`` gate evaluations of the commit, optionally of one ``profile`` and one of ``verdict_in``.
* ``report``: ``min_count`` rendered reports whose gate evaluated the commit, optionally holding ``format``.
* ``revalidation``: ``min_count`` revalidations (``RVL-``) of a fix whose ``fix_commit`` is the commit, optionally with an
  outcome in ``outcome_in``. Only a recorded revalidation meets it, so "verify the fix" is never done by a claim.
"""
from __future__ import annotations

import copy
import graphlib
import os
import stat
from typing import Dict, Iterable, List, Optional, Tuple

from . import gitinfo, ledger, schema, state
from .canonical import loads
from .errors import QaError
from .workspace import Workspace, read_limited

# QaError codes raised here: plan_invalid, unknown_plan, unknown_step, status_not_settable, invalid_status,
# blank_reason; plus ledger.append's.

_TEMPLATE = {"$ref": "plan.schema.json#/$defs/template"}
SETTABLE = ("blocked", "not_run", "pending")
_STATUSES = ("pending", "done", "blocked", "not_run")
_MAX_ARTIFACT_BYTES = 1 << 20
_REMEDY = ("install it with /plugin install qa-engineering-suite@<marketplace>, the marketplace you added with "
           "/plugin marketplace add; the plugin holds %s")


def _invalid(message: str, **details: object) -> QaError:
    return QaError("plan_invalid", message, **details)


# --------------------------------------------------------------------------- create

def _order_check(steps: List[dict]) -> None:
    ids_seen = [s["id"] for s in steps]
    duplicates = sorted({i for i in ids_seen if ids_seen.count(i) > 1})
    if duplicates:
        raise _invalid("step ids repeat", steps=duplicates)
    for step in steps:
        unknown = sorted(set(step.get("depends_on", [])) - set(ids_seen))
        if unknown:
            raise _invalid("a step depends on a step the plan does not have", step=step["id"], depends_on=unknown)
    graph = {s["id"]: set(s.get("depends_on", [])) for s in steps}
    try:
        graphlib.TopologicalSorter(graph).prepare()
    except graphlib.CycleError as exc:
        raise _invalid("depends_on has a cycle", cycle=[str(i) for i in exc.args[1]])


def _frozen_step(step: dict, commit: Optional[str]) -> dict:
    outputs = []
    for output in step["expected_outputs"]:
        frozen = dict(output, min_count=output.get("min_count", 1), commit=commit)
        if frozen["kind"] in ("execution", "result_set"):
            frozen.setdefault("skill", step["skill"])
        outputs.append(frozen)
    return {"id": step["id"], "skill": step["skill"], "depends_on": list(step.get("depends_on", [])),
            "requires_authorization": list(step.get("requires_authorization", [])), "expected_outputs": outputs,
            "exclusive": step.get("exclusive", False)}


def create(ws: Workspace, actor: ledger.Actor, template: dict) -> dict:
    """Record ``plan.created`` for ``template`` and return that event. The target commit is read now and frozen."""
    if not isinstance(template, dict):
        raise _invalid("a plan template is a JSON object")
    problems = schema.errors(template, _TEMPLATE)
    if problems:
        raise _invalid("the template does not fit the plan schema", errors=problems)
    _order_check(template["steps"])
    with ws.lock():
        facts, _limitation = gitinfo.git_info(ws.root)
        commit = None if facts is None else facts["commit"]
        number = int(ledger.next_human_id(ws, "PLAN").split("-")[1])
        return ledger.append(ws, actor, "plan.created", {
            "id": "PLAN-%04d" % number, "mode": template["mode"],
            "target": {"commit": commit, "dirty": None if facts is None else facts["dirty"]},
            "steps": [_frozen_step(step, commit) for step in template["steps"]]})


# --------------------------------------------------------------------------- update

def update(ws: Workspace, actor: ledger.Actor, plan_id: str, step_id: str, status: str, reason: str) -> dict:
    """Record a hold on one step and return the event. ``done`` is refused: only recorded outputs make a step done."""
    if status not in SETTABLE:
        if status in _STATUSES or status in ("passed", "complete", "completed"):
            raise QaError("status_not_settable", "a step becomes done only through recorded outputs", status=status,
                          settable=list(SETTABLE))
        raise QaError("invalid_status", "no such step status", status=status, settable=list(SETTABLE))
    if not isinstance(reason, str) or not reason.strip():
        raise QaError("blank_reason", "a hold needs a reason")
    with ws.lock():
        steps = _plan(state.load(ws), plan_id)["steps"]  # a named plan is never None
        if step_id not in [s["id"] for s in steps]:
            raise QaError("unknown_step", "the plan has no such step", plan=plan_id, step=step_id)
        return ledger.append(ws, actor, "plan.step_updated",
                             {"plan": plan_id, "step": step_id, "status": status, "reason": reason})


def _plan(st: dict, plan_id: Optional[str]) -> Optional[dict]:
    plans = st["plans"]
    if plan_id is None:
        if not plans:
            return None
        return plans[max(plans, key=lambda i: (len(i), i))]
    if plan_id not in plans:
        raise QaError("unknown_plan", "no such plan", plan=plan_id)
    return plans[plan_id]


# --------------------------------------------------------------------------- outputs

def _commit_of(record: dict) -> Optional[str]:
    git = record.get("git")
    return git["commit"] if isinstance(git, dict) else None


def _ran(record: dict) -> bool:
    return record["status"] not in ("blocked", "not_run") and record["evidence_manifest"] is not None


def _executions(st: dict, skill: str, commit: Optional[str]) -> List[str]:
    return sorted(i for i, r in st["executions"].items()
                  if r["actor"]["skill"] == skill and _commit_of(r) == commit and _ran(r))


def _count(found: int, wanted: int, noun: str) -> Tuple[bool, str]:
    return found >= wanted, "%d of %d %s" % (found, wanted, noun)


def _artifact(ws: Workspace, output: dict) -> Tuple[bool, str]:
    parts = output["path"].split("/")
    if output["path"].startswith("/") or any(p in ("", ".", "..") or "\\" in p or ":" in p for p in parts):
        return False, "the path must stay inside .qa/"
    where = ws.qa_dir
    try:
        for part in parts[:-1]:
            where = where / part
            if not stat.S_ISDIR(os.lstat(str(where)).st_mode):
                return False, "no such artifact"
        document = loads(read_limited(where / parts[-1], _MAX_ARTIFACT_BYTES))
    except (OSError, QaError):
        return False, "no readable artifact at that path"
    try:
        schema.validate(document, output["schema"])
    except QaError as exc:
        return False, "the artifact does not fit %s (%s)" % (output["schema"], exc.code)
    if not isinstance(document, dict) or document.get("commit") != output["commit"]:
        return False, "the artifact does not name the plan's commit"
    return True, "valid at the plan's commit"


def _gates(st: dict, output: dict) -> List[str]:
    return sorted(i for i, g in st["gates"].items()
                  if g["target"]["commit"] == output["commit"] and output.get("profile") in (None, g["profile"])
                  and (output.get("verdict_in") is None or g["verdict"] in output["verdict_in"]))


def _evaluate(ws: Workspace, st: dict, output: dict) -> Tuple[bool, str]:
    kind, wanted, commit = output["kind"], output["min_count"], output["commit"]
    if kind == "execution":
        return _count(len(_executions(st, output["skill"], commit)), wanted, "executions by %s" % output["skill"])
    if kind == "result_set":
        runs = set(_executions(st, output["skill"], commit))
        excluded = set(output.get("status_not_in", []))
        found = sum(1 for r in st["results"].values() if r["execution"] in runs and r["status"] not in excluded)
        return _count(found, wanted, "results of %s" % output["skill"])
    if kind == "artifact":
        return _artifact(ws, output)
    if kind == "gate":
        return _count(len(_gates(st, output)), wanted, "gate evaluations")
    if kind == "revalidation":
        wanted_outcomes = output.get("outcome_in")
        found = sum(1 for r in st["revalidations"].values()
                    if r["fix_commit"] == commit and (wanted_outcomes is None or r["outcome"] in wanted_outcomes))
        return _count(found, wanted, "revalidations")
    gates = set(_gates(st, {"commit": commit}))
    fmt = output.get("format")
    found = sum(1 for r in st["reports"].values() if r["gate"] in gates and (fmt is None or "report." + fmt in r["files"]))
    return _count(found, wanted, "reports")


# --------------------------------------------------------------------------- status

def status(ws: Workspace, plan_id: Optional[str] = None, installed_skills: Optional[Iterable[str]] = None) -> dict:
    """Evaluate a plan (the latest by default) against the ledger.

    ``installed_skills`` is the names of the skills available, from ``catalog.list_skills``. A step whose skill is not
    among them is blocked with a remedy. None means nobody could tell what is installed, which blocks nothing. With no plan
at all, ``id`` is None and there are no steps.
    """
    st = state.load(ws)
    record = _plan(st, plan_id)
    if record is None:  # no plan yet is an answer, not an error: the caller creates one
        return {"id": None, "mode": None, "target": None, "steps": [], "counts": {name: 0 for name in _STATUSES}}
    installed = None if installed_skills is None else set(installed_skills)
    steps: Dict[str, dict] = {}
    for step in record["steps"]:
        outputs = []
        for output in step["expected_outputs"]:
            met, detail = _evaluate(ws, st, output)
            outputs.append({"kind": output["kind"], "met": met, "detail": detail})
        steps[step["id"]] = _step_status(step, outputs, installed)
    for step in record["steps"]:
        steps[step["id"]]["ready"] = steps[step["id"]]["status"] == "pending" and all(
            steps[d]["status"] in ("done", "not_run") for d in step["depends_on"])
    ordered = [steps[s["id"]] for s in record["steps"]]
    return {"id": record["id"], "mode": record["mode"], "target": copy.deepcopy(record["target"]), "steps": ordered,
            "counts": {name: sum(1 for s in ordered if s["status"] == name) for name in _STATUSES}}


def _step_status(step: dict, outputs: List[dict], installed: Optional[set]) -> dict:
    hold = step["hold"]
    state_ = reason = remedy = None
    if hold is not None and hold["status"] == "not_run":
        state_, reason = "not_run", hold["reason"]
    elif all(o["met"] for o in outputs):
        state_ = "done"
    else:
        if installed is not None and step["skill"] not in installed:
            remedy = _REMEDY % step["skill"]
        if hold is not None:
            state_, reason = "blocked", hold["reason"]
        elif remedy is not None:
            state_, reason = "blocked", "the skill %s is not installed" % step["skill"]
        else:
            state_ = "pending"
    return {"id": step["id"], "skill": step["skill"], "depends_on": list(step["depends_on"]), "status": state_,
            "reason": reason, "remedy": remedy, "ready": False, "outputs": outputs}
