"""Audit (F2): recall, precision and severity agreement of a run's detections against the answer key.

Detections come from two places, each ``{"origin", "id", "title", "tool", "path", "line", "endpoint", "cwe", "severity"}``
(a message detection also has ``recorded_as``):

- ``kernel_detections(findings, vocabulary=None)`` (``with-suite``): the findings the kernel recorded, except those marked
  ``duplicate`` or verified ``false_positive``. The tool is the finding's ``source.tool`` when it is in ``vocabulary`` (the
  scenario's ``tools_present``; any tool when None); otherwise the tool its method shows: ``in-process-check`` when it was
  found dynamically or its evidence cites an ``EXE-`` execution, and ``static-review`` for the rest. The line is
  ``source.start_line``; without one, a ``path:line`` at the start of ``refs.location`` gives the path and the line.
- ``message_detections``: the findings the judge (``rubrics/audit.md``) lists from the final message, as
  ``static-review``. One the judge ties to an ``FND-`` id the kernel holds keeps that id in ``recorded_as``: it is the
  message's copy of that recorded finding.

``grade`` gives ``recall`` (``recall.recall(key, detections, set(tools_present))``, over every detection, so an issue is
credited when a recorded finding or its message copy credits it), ``precision`` (units that locate a key issue with an
agreeing class ÷ all units, where a recorded finding and its message copies are one unit, matched when any of them matches;
``lower_bound: true``, since an unmatched finding may be real; None with no detection) and ``severity``: for each issue
recall credits, the first crediting detection with a severity, compared with the level of the issue's anchor in
``shared/policies/severity-rubric.json``, exactly and within one level.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

HERE = Path(__file__).resolve().parent
RUBRIC = HERE / "rubrics" / "audit.md"
SEVERITY_RUBRIC = HERE.parents[1] / "shared" / "policies" / "severity-rubric.json"
LEVELS = ("INFORMATIONAL", "LOW", "MEDIUM", "HIGH", "CRITICAL")
MESSAGE_TOOL = "static-review"
LOCATION_LINE = re.compile(r"\A(?P<path>[^\s:]+):(?P<line>[0-9]+)(?![0-9])")


def _recall_module() -> Any:
    spec = importlib.util.spec_from_file_location("qa_bench_recall", str(HERE / "recall.py"))
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


recall_module = _recall_module()


def _level(value: Any) -> Optional[str]:
    return value.strip().upper() if isinstance(value, str) and value.strip().upper() in LEVELS else None


def _line(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None


def kernel_detections(findings: Mapping[str, Mapping[str, Any]],
                      vocabulary: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    known = set(vocabulary) if vocabulary is not None else None
    found = []
    for ident in sorted(findings):
        finding = findings[ident]
        if finding.get("state") == "duplicate" or finding.get("verification_status") == "false_positive":
            continue
        source = finding.get("source") if isinstance(finding.get("source"), dict) else {}
        component = finding.get("component") if isinstance(finding.get("component"), dict) else {}
        refs = finding.get("refs") if isinstance(finding.get("refs"), dict) else {}
        cwes = refs.get("cwe") if isinstance(refs.get("cwe"), list) else []
        cites_run = "EXE-" in json.dumps(finding.get("evidence", []))
        named = _str(source.get("tool"))
        tool = named if named and (known is None or named in known) else (
            "in-process-check" if source.get("method") == "dynamic" or cites_run else "static-review")
        path, line = _str(component.get("path")), _line(source.get("start_line"))
        located = LOCATION_LINE.match(refs["location"]) if line is None and isinstance(refs.get("location"), str) else None
        if located:
            path, line = located.group("path"), int(located.group("line"))
        found.append({"origin": "kernel", "id": ident, "title": finding.get("title"), "tool": tool, "path": path,
                      "line": line, "endpoint": _str(component.get("endpoint")), "cwe": _str(cwes[0]) if cwes else None,
                      "severity": _level(finding.get("severity"))})
    return found


def message_detections(judged: Any, recorded: Iterable[str] = ()) -> Optional[List[Dict[str, Any]]]:
    """The judge's findings as detections, each tied to a ``recorded`` id keeping it in ``recorded_as``; None when the list
    is malformed."""
    if not isinstance(judged, list) or not all(isinstance(item, dict) for item in judged):
        return None
    known = set(recorded)
    return [{"origin": "message", "id": None, "title": item.get("title"), "tool": MESSAGE_TOOL, "path": _str(item.get("path")),
             "line": _line(item.get("line")), "endpoint": _str(item.get("endpoint")), "cwe": _str(item.get("cwe")),
             "severity": _level(item.get("severity")),
             "recorded_as": item["recorded_as"] if isinstance(item.get("recorded_as"), str) and item["recorded_as"] in known
             else None}
            for item in judged]


def _agrees(issue: Mapping[str, Any], detection: Mapping[str, Any]) -> bool:
    return recall_module.locates(issue, detection) and recall_module.class_agrees(issue, detection)


def _units(detections: Sequence[Mapping[str, Any]]) -> List[List[Mapping[str, Any]]]:
    """The detections grouped for precision: a recorded finding with its message copies, and every other one alone."""
    units: Dict[Any, List[Mapping[str, Any]]] = {}
    order: List[Any] = []
    for number, detection in enumerate(detections):
        tie = detection.get("recorded_as") if detection.get("origin") == "message" else (
            detection.get("id") if detection.get("origin") == "kernel" else None)
        key = ("recorded", tie) if isinstance(tie, str) else ("alone", number)
        if key not in units:
            units[key] = []
            order.append(key)
        units[key].append(detection)
    return [units[key] for key in order]


def precision(key: Mapping[str, Any], detections: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    units = _units(detections)
    matched = sum(1 for unit in units if any(_agrees(issue, d) for d in unit for issue in key.get("issues", [])))
    return {"matched": matched, "detections": len(units), "precision": matched / len(units) if units else None,
            "lower_bound": True}


def severity(key: Mapping[str, Any], detections: Sequence[Mapping[str, Any]], tools_present: Set[str],
             rubric: Mapping[str, Any]) -> Dict[str, Any]:
    items = []
    for issue in key.get("issues", []):
        tools = set(issue.get("detectable_by", ())) & set(tools_present)
        credits = [d for d in detections if d.get("tool") in tools and _agrees(issue, d) and d.get("severity")]
        anchor = rubric.get("anchors", {}).get(issue.get("severity_anchor"), {})
        expected = _level(anchor.get("severity"))
        if not credits or expected is None:
            continue
        given = credits[0]["severity"]
        apart = abs(LEVELS.index(given) - LEVELS.index(expected))
        items.append({"id": issue["id"], "anchor": issue.get("severity_anchor"), "expected": expected, "given": given,
                      "exact": apart == 0, "within_one": apart <= 1})
    compared = len(items)
    exact, near = sum(1 for i in items if i["exact"]), sum(1 for i in items if i["within_one"])
    return {"compared": compared, "exact": exact, "within_one": near, "exact_rate": exact / compared if compared else None,
            "within_one_rate": near / compared if compared else None, "items": items}


def grade(key: Mapping[str, Any], detections: Sequence[Mapping[str, Any]], tools_present: Iterable[str],
          rubric: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    present = set(tools_present)
    rubric = rubric if rubric is not None else json.loads(SEVERITY_RUBRIC.read_text(encoding="utf-8"))
    return {"recall": recall_module.recall(key, detections, present), "precision": precision(key, detections),
            "severity": severity(key, detections, present, rubric)}
