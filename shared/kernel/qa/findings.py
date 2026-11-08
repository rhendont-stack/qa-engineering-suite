"""Findings: the record, its severity rubric and priority matrix, deduplication and lifecycle (§10.3-§10.7).

Severity comes only from the rubric (§10.4): an anchor, plus bounded modifiers, each used at most once. A tool's own
severity is an input kept in ``source``; the one exception is the anchor ``TOOL-PROVISIONAL``, which a finding raised
from a tool alert carries until someone triages it. Priority comes from the matrix (§10.5). ``add`` computes it
with no adjustments: Appendix A.3 has no field to record them, and they arrive with milestone 3's triage.

A finding's identity is its fingerprint (§10.7): ``hash([category, rule, component, location, key_attributes])``.
Adding a finding whose fingerprint matches an existing one in any state but ``verified_fixed`` records a new sighting
instead of a duplicate, so a re-scan neither opens duplicates nor undoes an ``accepted_risk`` or ``wont_fix`` decision.
Known limitation: the fingerprint has no line number, so two alerts of one rule in one file are one finding, seen twice.
An alert path is made repo-relative from the run's recorded directory; one nobody can place keeps its raw text in
``key_attributes``, so different such paths stay different findings.
"""
from __future__ import annotations

import copy
import math
import os
import posixpath
import re
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

from . import ids, ledger, resource_dir, schema, state
from .canonical import hash_obj, loads
from .errors import QaError
from .ingest import Alert, ReportSummary
from .vocab import MILESTONE_3_STATES, TRANSITIONS
from .workspace import Workspace

SEVERITIES = ("INFORMATIONAL", "LOW", "MEDIUM", "HIGH", "CRITICAL")
TOOL_PROVISIONAL = "TOOL-PROVISIONAL"
_PRIORITIES = ("P0", "P1", "P2", "P3")
_OPENED = "events/finding.opened.schema.json"

# Set by the kernel, never by a caller. ``id``, ``priority`` and ``state`` would let an agent rank its own finding.
_KERNEL_FIELDS = ("id", "state", "priority", "fingerprint")
_INPUT_FIELDS = ("title", "description", "category", "severity", "severity_rationale", "component", "evidence",
                 "reproduction_steps", "impact", "probable_cause", "recommendation", "verification_status", "refs",
                 "links", "source", "commit", "key_attributes", "justification")
_NO_PLACEHOLDER = "sha256:" + "0" * 64
_CONFIRMING = ("exit_code", "structured")

# The lookbehind starts the path only where its run of characters starts: without it every position rescans the run.
_CAUSE_CITATION = re.compile(r"(?<![\w./\\-])(?=[\w./\\-]*[A-Za-z])[\w./\\-]+:[0-9]+|\bEV-[0-9a-f]{12}\b|\bEXE-[0-9A-HJKMNP-TV-Z]{26}\b")
_DRIVE = re.compile(r"[A-Za-z]:[\\/]")
_URI_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_LINE_SUFFIX = re.compile(r":[0-9]+$")
_TITLE_CHARS = 120
_LEVELS = {"error": "HIGH", "warning": "MEDIUM", "note": "LOW", "none": "INFORMATIONAL"}

_RUBRIC: Optional[dict] = None
_MATRIX: Optional[dict] = None


# --------------------------------------------------------------------------- policy

def _load_policy(name: str, schema_name: str) -> dict:
    doc = loads((resource_dir("policies") / name).read_bytes())
    schema.validate(doc, schema_name)
    return doc


def load_rubric() -> dict:
    """``severity-rubric.json``, validated once (cached; callers must not mutate it)."""
    global _RUBRIC
    if _RUBRIC is None:
        _RUBRIC = _load_policy("severity-rubric.json", "severity-rubric.schema.json")
    return _RUBRIC


def load_matrix() -> dict:
    """``priority-matrix.json``, validated once (cached; callers must not mutate it)."""
    global _MATRIX
    if _MATRIX is None:
        _MATRIX = _load_policy("priority-matrix.json", "priority-matrix.schema.json")
    return _MATRIX


def compute_severity(anchor: str, modifiers: List[str], rubric: dict) -> str:
    entry = rubric["anchors"].get(anchor) if isinstance(anchor, str) else None
    if entry is None:
        raise QaError("unknown_anchor", "no such rubric anchor", anchor=anchor)
    if entry["severity"] is None:
        raise QaError("tool_provisional", "this anchor takes its severity from the tool", anchor=anchor)
    steps = rubric["modifiers"]
    seen: List[str] = []
    for modifier in modifiers if isinstance(modifiers, (list, tuple)) else [None]:
        if not isinstance(modifier, str) or modifier not in steps:
            raise QaError("unknown_modifier", "no such severity modifier", modifier=modifier)
        if modifier in seen:
            raise QaError("duplicate_modifier", "a modifier applies at most once", modifier=modifier)
        seen.append(modifier)
    index = SEVERITIES.index(entry["severity"]) + sum(steps[m] for m in seen)
    return SEVERITIES[max(0, min(len(SEVERITIES) - 1, index))]


def compute_priority(severity: str, criticality: str, adjustments: List[str], matrix: dict) -> str:
    row = matrix["rows"].get(severity) if isinstance(severity, str) else None
    if row is None:
        raise QaError("unknown_severity", "no such severity", severity=severity)
    if criticality not in matrix["criticalities"]:
        raise QaError("unknown_criticality", "no such criticality", criticality=criticality)
    steps = matrix["adjustments"]
    seen: List[str] = []
    for adjustment in adjustments:
        if not isinstance(adjustment, str) or adjustment not in steps:
            raise QaError("unknown_adjustment", "no such priority adjustment", adjustment=adjustment)
        if adjustment in seen:
            raise QaError("duplicate_adjustment", "an adjustment applies at most once", adjustment=adjustment)
        seen.append(adjustment)
    base = row[matrix["criticalities"].index(criticality)]
    if base == "none":
        return "none"
    index = _PRIORITIES.index(base) - sum(steps[a] for a in seen)  # a positive step moves toward P0
    return _PRIORITIES[max(0, min(len(_PRIORITIES) - 1, index))]


# --------------------------------------------------------------------------- identity

def fingerprint(category: str, rule: str, component: str, location: str, key_attributes: dict) -> str:
    """§10.7. ``location`` is a repo-relative POSIX path plus an optional symbol, never a line number."""
    return hash_obj([category, rule, component, location, key_attributes])


def _location_problem(location: str) -> Optional[str]:
    if location.startswith(("/", "\\")) or _DRIVE.match(location):
        return "is absolute"
    if ".." in re.split(r"[\\/]", location):
        return "holds .."
    if _LINE_SUFFIX.search(location):
        return "ends in a line number"
    return None


# --------------------------------------------------------------------------- validation

def _blank(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def _with_defaults(f: dict) -> dict:
    """A copy of ``f``'s caller-settable fields with the optional ones filled in; ``f`` itself is not touched."""
    g = {key: copy.deepcopy(f[key]) for key in _INPUT_FIELDS if key in f}
    for key in ("impact", "probable_cause", "recommendation", "commit"):
        g.setdefault(key, None)
    g.setdefault("links", {})
    g.setdefault("key_attributes", {})
    component = g.setdefault("component", {})
    if isinstance(component, dict) and "criticality" not in component:
        component["criticality"] = "normal"
        component["criticality_source"] = "default"
    return g


def _cited_strengths(st: dict, reference: str) -> List[Optional[str]]:
    """The strength each record behind a citation carries: an ingested copy has its own, apart from its execution's."""
    found: List[Optional[str]] = []
    if reference.startswith("EXE-"):
        record = st["executions"].get(reference)
        if record is not None:
            found.append(record["strength"])
    elif reference.startswith("EV-"):
        for record in st["executions"].values():
            manifest = record["evidence_manifest"]
            if manifest is not None and reference in manifest["ids"].values():
                found.append(record["strength"])
            found.extend(item["strength"] for item in record["ingested"] if item["ev_id"] == reference)
    return found


def validate_new(st: dict, f: dict) -> List[str]:
    """Every reason ``f`` cannot be recorded as a finding; empty when it can. Never raises, never changes ``f``."""
    if not isinstance(f, dict):
        return ["a finding must be an object"]
    problems: List[str] = []
    for key in sorted(f, key=str):
        if key in _KERNEL_FIELDS:
            problems.append("%s is set by the kernel and cannot be supplied" % key)
        elif key not in _INPUT_FIELDS:
            problems.append("%s is not a finding field" % key)
    g = _with_defaults(f)
    rubric = load_rubric()
    rationale = g.get("severity_rationale")
    anchor = rationale.get("anchor") if isinstance(rationale, dict) else None
    modifiers = rationale.get("modifiers") if isinstance(rationale, dict) else None
    severity: Optional[str] = None  # the rubric's answer
    if isinstance(anchor, str) and isinstance(modifiers, list):
        if anchor == TOOL_PROVISIONAL:
            if not isinstance(g.get("severity"), str):
                problems.append("severity is required under %s: it is the tool's, mapped to the scale" % TOOL_PROVISIONAL)
            source = g.get("source")
            if not isinstance(source, dict) or _blank(source.get("tool")):
                problems.append("source.tool is required under %s: the anchor is for a tool's untriaged alert" % TOOL_PROVISIONAL)
            if modifiers:
                problems.append("modifiers must be empty under %s: they have nothing to adjust" % TOOL_PROVISIONAL)
            if g.get("verification_status") != "suspected":
                problems.append("verification_status must be suspected under %s: triage moves out of the anchor" % TOOL_PROVISIONAL)
        else:
            try:
                severity = compute_severity(anchor, modifiers, rubric)
            except QaError as exc:
                problems.append("severity_rationale is refused (%s)" % exc.code)
            if severity is not None and "severity" in g and g["severity"] != severity:
                problems.append("severity %s does not match the rubric result %s" % (g["severity"], severity))
        entry = rubric["anchors"].get(anchor)
        column = rubric["columns"].get(g.get("category")) if isinstance(g.get("category"), str) else None
        if entry is not None and entry["column"] is not None and column is not None and entry["column"] != column:
            problems.append("severity_rationale.anchor %s is rated in the %s column, but category %s uses %s"
                            % (anchor, entry["column"], g["category"], column))
    for key in ("title", "description"):
        if isinstance(g.get(key), str) and not g[key].strip():
            problems.append("%s is blank" % key)
    refs = g.get("refs")
    if isinstance(refs, dict):
        if "rule" not in refs or (isinstance(refs["rule"], str) and not refs["rule"].strip()):
            problems.append("refs.rule is required: the detecting rule's ID, or manual")
        location = refs.get("location")
        if isinstance(location, str):
            why = "is blank" if not location.strip() else _location_problem(location)
            if why is not None:
                problems.append("refs.location %s: use a repo-relative path and an optional symbol, never a line number" % why)
    else:
        problems.append("refs.rule is required: the detecting rule's ID, or manual")
    problems.extend(_evidence_problems(st, g))
    status = g.get("verification_status")
    if status != "suspected" and isinstance(status, str):
        for key in ("impact", "recommendation"):
            if _blank(g.get(key)):
                problems.append("%s is required unless verification_status is suspected" % key)
    if status == "false_positive" and _blank(g.get("justification")):
        problems.append("justification is required for false_positive: state how the finding was disproved")
    cause = g.get("probable_cause")
    if cause is not None and (not isinstance(cause, str) or not _CAUSE_CITATION.search(cause)):
        problems.append("probable_cause must cite path:line, an EV- id or an EXE- id")
    problems.extend(schema.errors(_schema_candidate(g, severity, refs), schema.load_schema(_OPENED), base=_OPENED))
    return list(dict.fromkeys(problems))


def _evidence_problems(st: dict, g: dict) -> List[str]:
    evidence = g.get("evidence")
    if not isinstance(evidence, list):
        return []  # the schema pass says so
    problems = []
    known = state.known_references(st)
    cited: List[Optional[str]] = []
    for reference in evidence:
        if isinstance(reference, str) and reference.startswith(("EXE-", "EV-")):
            if reference not in known:
                problems.append("evidence %s resolves to no recorded execution or evidence file" % reference)
            cited.extend(_cited_strengths(st, reference))
    if g.get("verification_status") == "confirmed" and not any(s in _CONFIRMING for s in cited):
        problems.append("a confirmed finding must cite an execution of strength exit_code or structured in evidence")
    return problems


def _schema_candidate(g: dict, severity: Optional[str], refs: object) -> dict:
    """``g`` with the kernel's fields and every value validation already reported replaced by a valid placeholder."""
    candidate = dict(g, id="FND-000", state="open", priority="none", fingerprint=_NO_PLACEHOLDER)
    candidate.setdefault("source", {"skill": None, "method": "manual"})
    rationale = g.get("severity_rationale")
    if not (isinstance(rationale, dict) and rationale.get("anchor") == TOOL_PROVISIONAL and "severity" in g):
        candidate["severity"] = severity or "INFORMATIONAL"
    if isinstance(refs, dict) and "rule" not in refs:
        candidate["refs"] = dict(refs, rule="-")
    elif not isinstance(refs, dict) and "refs" not in g:
        candidate["refs"] = {"rule": "-"}
    return candidate


# --------------------------------------------------------------------------- adding

def _build(f: dict, actor: ledger.Actor) -> dict:
    """The finding record for a ``validate_new``-clean ``f``, without its ``id``."""
    g = _with_defaults(f)
    g.setdefault("source", {"skill": actor.skill, "method": "manual"})
    if g["severity_rationale"]["anchor"] != TOOL_PROVISIONAL:
        g["severity"] = compute_severity(g["severity_rationale"]["anchor"], g["severity_rationale"]["modifiers"], load_rubric())
    component = g["component"]
    g["priority"] = compute_priority(g["severity"], component["criticality"], [], load_matrix())
    g["state"] = "open"
    g["fingerprint"] = fingerprint(g["category"], g["refs"]["rule"], component.get("path") or component.get("endpoint") or "",
                                   g["refs"].get("location") or "", g["key_attributes"])
    return g


def _add(ws: Workspace, actor: ledger.Actor, f: dict, st: dict, next_id: Callable[[], str]) -> Tuple[dict, bool]:
    """``add`` under the lock. ``st`` is kept current, so a batch need not fold the ledger once per finding."""
    errors = validate_new(st, f)
    if errors:
        raise QaError("finding_invalid", "the finding cannot be recorded", errors=errors)
    record = _build(f, actor)
    seen = next((fid for fid, old in st["findings"].items()
                 if old["fingerprint"] == record["fingerprint"] and old["state"] != "verified_fixed"), None)
    if seen is not None:
        last_seen = ids.format_ts(ids.now_utc())
        event = ledger.append(ws, actor, "finding.updated", {"id": seen, "last_seen": last_seen, "source": record["source"]})
        st["findings"][seen].update(last_seen=last_seen, source=copy.deepcopy(record["source"]))
        return event, False
    record["id"] = next_id()
    event = ledger.append(ws, actor, "finding.opened", record)
    state.REDUCERS["finding.opened"](st, event)
    return event, True


def add(ws: Workspace, actor: ledger.Actor, f: dict) -> Tuple[dict, bool]:
    """Record ``f`` as a new finding, or as a new sighting of the open one with its fingerprint."""
    with ws.lock():
        return _add(ws, actor, f, state.load(ws), lambda: ledger.next_human_id(ws, "FND"))


# --------------------------------------------------------------------------- changing

_IDENTITY_FIELDS = ("id", "state", "priority", "fingerprint", "last_seen", "category", "key_attributes")
_IDENTITY_PARTS = (("refs", "rule"), ("refs", "location"), ("component", "path"), ("component", "endpoint"))


def update(ws: Workspace, actor: ledger.Actor, fid: str, changes: dict) -> dict:
    """Change a finding's fields. Severity and priority are recomputed, never taken from the caller, and identity is fixed."""
    with ws.lock():
        st = state.load(ws)
        current = st["findings"].get(fid)
        if current is None:
            raise QaError("unknown_finding", "no such finding", id=fid)
        if not isinstance(changes, dict):
            raise QaError("finding_invalid", "the changes must be an object", errors=["changes must be an object"])
        fixed = [key for key in _IDENTITY_FIELDS if key in changes]
        fixed += ["%s.%s" % (top, key) for top, key in _IDENTITY_PARTS
                  if isinstance(changes.get(top), dict) and key in changes[top]]
        if fixed:
            raise QaError("finding_invalid", "these fields are the finding's identity or the kernel's",
                          errors=["%s cannot be changed: a finding that changes identity is a new finding" % key for key in fixed])
        merged = {key: copy.deepcopy(current[key]) for key in _INPUT_FIELDS if key in current}
        final: Dict[str, object] = {}
        for key, value in changes.items():
            if key in ("component", "refs") and isinstance(value, dict) and isinstance(merged.get(key), dict):
                value = dict(merged[key], **copy.deepcopy(value))
                if key == "component" and "criticality" in changes[key]:
                    value.pop("criticality_source", None)  # no longer the default
            final[key] = copy.deepcopy(value)
        merged.update(final)
        _recompute(merged, final, changes)
        errors = _provisional_problems(current, merged)
        errors += validate_new(st, merged)
        if errors:
            raise QaError("finding_invalid", "the finding would no longer be valid", errors=errors)
        return ledger.append(ws, actor, "finding.updated", {"id": fid, "changes": final})


def _provisional_problems(current: dict, merged: dict) -> List[str]:
    """Only an alert's own finding is provisional; triage leaves the anchor, nobody enters it or edits its severity."""
    anchor = merged.get("severity_rationale", {}).get("anchor") if isinstance(merged.get("severity_rationale"), dict) else None
    if anchor != TOOL_PROVISIONAL:
        return []
    if current["severity_rationale"]["anchor"] != TOOL_PROVISIONAL:
        return ["severity_rationale.anchor cannot become %s: only a tool's alert starts there" % TOOL_PROVISIONAL]
    if merged.get("severity") != current["severity"]:
        return ["severity cannot be edited under %s: triage to a rubric anchor instead" % TOOL_PROVISIONAL]
    return []


def _recompute(merged: dict, final: dict, changes: dict) -> None:
    """Put the recomputed ``severity`` (also in ``merged``) and ``priority`` into ``final`` when a change affects them."""
    try:
        rationale = merged["severity_rationale"]
        if "severity_rationale" in changes and "severity" not in changes and rationale["anchor"] != TOOL_PROVISIONAL:
            merged["severity"] = final["severity"] = compute_severity(rationale["anchor"], rationale["modifiers"], load_rubric())
        component_changed = isinstance(changes.get("component"), dict) and "criticality" in changes["component"]
        if component_changed or "severity_rationale" in changes or "severity" in changes:
            final["priority"] = compute_priority(
                merged["severity"], merged["component"]["criticality"], [], load_matrix())
    except (QaError, KeyError, TypeError):
        pass


def change_state(ws: Workspace, actor: ledger.Actor, fid: str, new_state: str, *, reason: str,
                 accepted_by: Optional[str] = None, expires_at: Optional[str] = None,
                 duplicate_of: Optional[str] = None) -> dict:
    """Move a finding along ``TRANSITIONS``; the fields a transition needs are checked, the others are recorded as null."""
    if new_state in MILESTONE_3_STATES:
        raise QaError("needs_milestone_3", "fixes and revalidation arrive in milestone 3", state=new_state)
    with ws.lock():
        st = state.load(ws)
        current = st["findings"].get(fid)
        if current is None:
            raise QaError("unknown_finding", "no such finding", id=fid)
        needs = TRANSITIONS.get(current["state"], {}).get(new_state)
        if needs is None:
            raise QaError("invalid_transition", "the lifecycle has no such step", id=fid, **{"from": current["state"], "to": new_state})
        given = {"reason": reason, "accepted_by": accepted_by, "expires_at": expires_at, "duplicate_of": duplicate_of}
        for name in needs:
            if _blank(given[name]):
                raise QaError("missing_field", "this transition needs a non-blank %s" % name, field=name)
        if "expires_at" in needs:
            try:
                ids.parse_ts(expires_at)  # a date in the past is allowed: the gate treats it as an expired acceptance
            except QaError as exc:
                raise QaError("missing_field", "expires_at must look like YYYY-MM-DDTHH:MM:SS.mmmZ", field="expires_at") from exc
        if "duplicate_of" in needs and (duplicate_of == fid or duplicate_of not in st["findings"]):
            raise QaError("unknown_finding", "duplicate_of must name another existing finding", id=duplicate_of)
        data = {"id": fid, "from": current["state"], "to": new_state}
        data.update({name: given[name] if name in needs else None for name in given})
        return ledger.append(ws, actor, "finding.state_changed", data)


# --------------------------------------------------------------------------- alerts

def _finite(score: object) -> Optional[float]:
    return score if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score) else None


def _alert_severity(alert: Alert) -> str:
    score = alert.security_severity
    if _finite(score) is not None and score >= 0:
        for floor, severity in ((9.0, "CRITICAL"), (7.0, "HIGH"), (4.0, "MEDIUM")):
            if score >= floor:
                return severity
        return "LOW" if score > 0 else "INFORMATIONAL"
    return _LEVELS.get(alert.level, "HIGH")  # an unknown level must not read as harmless (T13-R6)


def _repo_path(ws: Workspace, raw: Optional[str], cwd: str = ".") -> Optional[str]:
    """``raw`` as a repo-relative POSIX path, or None when it cannot be a stable one.

    A relative path is taken from ``cwd``, the run's directory relative to the workspace root."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = raw
    if _URI_SCHEME.match(raw):
        try:
            parts = urlsplit(raw)
        except ValueError:  # e.g. an unclosed IPv6 bracket
            return None
        if parts.scheme.lower() != "file" or parts.netloc not in ("", "localhost"):
            return None
        path = unquote(parts.path)
    if "\x00" in path or "\\" in path:
        return None
    if path.startswith("/"):
        normal = posixpath.normpath(path)
        for root in dict.fromkeys((str(ws.root), os.path.realpath(str(ws.root)))):
            prefix = root.rstrip("/") + "/"
            if normal.startswith(prefix):
                relative = normal[len(prefix):]
                break
        else:
            return None
    else:
        if ".." in path.split("/"):
            return None
        relative = posixpath.normpath(posixpath.join(cwd, path))
    if relative in ("", ".") or _location_problem(relative) is not None:
        return None
    return relative


def _alert_finding(ws: Workspace, actor: ledger.Actor, alert: Alert, execution_id: str, category: str,
                   commit: Optional[str], cwd: str) -> dict:
    rule = (alert.rule_id or "").strip() or "unknown"
    message = alert.message.strip() if isinstance(alert.message, str) else ""
    first = message.splitlines()[0].strip() if message else ""
    path = _repo_path(ws, alert.path, cwd)
    source: Dict[str, object] = {"skill": actor.skill, "method": "static", "tool": (alert.tool or "").strip() or "unknown", "level": alert.level,
                                 "security_severity": _finite(alert.security_severity), "start_line": alert.start_line}
    component: Dict[str, str] = {}
    refs: Dict[str, object] = {"rule": rule}
    key_attributes: Dict[str, str] = {}
    if path is not None:
        component["path"] = path
        refs["location"] = path
    elif isinstance(alert.path, str) and alert.path.strip():
        source["uri"] = alert.path
        key_attributes["uri"] = alert.path  # different paths nobody can place stay different findings
    return {
        "title": first[:_TITLE_CHARS].strip() or rule, "description": message or rule,
        "category": category, "severity": _alert_severity(alert),
        "severity_rationale": {"anchor": TOOL_PROVISIONAL, "modifiers": []}, "component": component,
        "evidence": [execution_id], "reproduction_steps": [], "verification_status": "suspected", "refs": refs,
        "source": source, "commit": commit,
        "key_attributes": key_attributes,
    }


def ingest_alerts(ws: Workspace, actor: ledger.Actor, summary: ReportSummary, *, execution_id: str, category: str) -> dict:
    """One ``suspected`` finding per alert, rated provisionally from the tool's own severity (§10.4, §10.6)."""
    opened = updated = 0
    with ws.lock():
        st = state.load(ws)
        git = st["executions"].get(execution_id, {}).get("git")
        commit = git["commit"] if isinstance(git, dict) else None
        command = st["executions"].get(execution_id, {}).get("command")
        cwd = command["cwd"] if isinstance(command, dict) and command.get("cwd") else "."
        counter = [int(ledger.next_human_id(ws, "FND").split("-")[1]) - 1]

        def next_id() -> str:
            counter[0] += 1
            return ledger.format_human_id("FND", counter[0])

        for alert in summary.alerts:
            _, created = _add(ws, actor, _alert_finding(ws, actor, alert, execution_id, category, commit, cwd), st, next_id)
            opened += created
            updated += not created
    return {"opened": opened, "updated": updated}
