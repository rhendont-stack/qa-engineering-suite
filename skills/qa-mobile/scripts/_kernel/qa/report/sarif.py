"""SARIF 2.1.0 export of the findings the state holds, and nothing else.

A pure function of the state: no clock, no ID of the report, no finding made up for a tool. The same findings give the
same bytes. A finding the user dismissed (accepted risk, won't fix, duplicate, false positive) stays in the file with a
``suppressions`` entry, so a viewer shows it as suppressed instead of the export hiding that it was ever found.
"""
from __future__ import annotations

from typing import Dict, List

from .. import KERNEL_VERSION
from ..canonical import dumps

SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
_LEVEL = {"CRITICAL": "error", "HIGH": "error", "MEDIUM": "warning", "LOW": "note", "INFORMATIONAL": "note"}
_SUPPRESSED_STATES = ("accepted_risk", "wont_fix", "duplicate")


def _in_id_order(names: List[str]) -> List[str]:
    return sorted(names, key=lambda name: (len(name), name))


def _location(finding: dict) -> List[dict]:
    path = finding["component"].get("path")
    if path is None:
        return []
    physical: Dict[str, object] = {"artifactLocation": {"uri": path}}
    line = finding["source"].get("start_line")
    if isinstance(line, int) and line > 0:
        physical["region"] = {"startLine": line}
    location: Dict[str, object] = {"physicalLocation": physical}
    symbol = finding["refs"].get("location")
    if isinstance(symbol, str):
        location["logicalLocations"] = [{"fullyQualifiedName": symbol}]
    return [location]


def _suppressions(finding: dict) -> List[dict]:
    if finding["verification_status"] == "false_positive":
        reason = finding.get("justification") or "recorded as a false positive"
    elif finding["state"] in _SUPPRESSED_STATES:
        reason = finding.get("state_reason") or finding["state"]
    else:
        return []
    return [{"kind": "external", "status": "accepted", "justification": reason}]


def _result(fid: str, finding: dict) -> dict:
    text = finding["title"]
    if finding["description"] and finding["description"] != text:
        text = "%s: %s" % (text, finding["description"])
    result: Dict[str, object] = {
        "ruleId": finding["refs"]["rule"], "level": _LEVEL[finding["severity"]], "message": {"text": text},
        "locations": _location(finding), "partialFingerprints": {"qa/v1": finding["fingerprint"]},
        "properties": {"id": fid, "category": finding["category"], "severity": finding["severity"],
                       "priority": finding["priority"], "state": finding["state"],
                       "verification_status": finding["verification_status"]},
    }
    suppressions = _suppressions(finding)
    if suppressions:
        result["suppressions"] = suppressions
    return result


def render_sarif_document(st: dict) -> dict:
    results = [_result(fid, st["findings"][fid]) for fid in _in_id_order(list(st["findings"]))]
    rules = [{"id": rule, "name": rule} for rule in sorted({r["ruleId"] for r in results})]
    driver = {"name": "qa", "version": KERNEL_VERSION, "rules": rules}
    return {"$schema": SARIF_SCHEMA, "version": "2.1.0", "runs": [{"tool": {"driver": driver}, "results": results}]}


def render_sarif(st: dict) -> bytes:
    return dumps(render_sarif_document(st)) + b"\n"
