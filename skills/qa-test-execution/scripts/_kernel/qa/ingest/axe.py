"""axe-core results: one Alert per (violation, node), the WCAG success criteria as tags.

The input is one results object (what ``axe.run()`` resolves to, and what ``@axe-core/playwright`` returns) or an array of
them. A violation's impact gives the level (critical and serious are errors, moderate and minor warnings); an ``incomplete``
check is a note tagged ``needs-review``, a person's call. ``passes`` and ``inapplicable`` are only counted. The page's URL
keeps its path alone, and a node's HTML is never copied.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

from qa import canonical
from qa.errors import QaError
from qa.ingest import Alert, ReportInvalid, ReportSummary

ALERT_LIMIT = 10000
NAME_LIMIT = 200  # a rule id longer than this is not one axe wrote
TARGET_LIMIT = 500
PATH_LIMIT = 500
TAG_LIMIT = 1000  # tags read per rule; axe writes a dozen
VERSION_LIMIT = 200
_ERRORS = ("critical", "serious")
_WARNINGS = ("moderate", "minor")
_SC = re.compile(r"wcag([0-9])([0-9])([0-9]{1,2})")
_LISTS = ("violations", "incomplete", "passes", "inapplicable")
_LEVELS = {"wcag2a": "A", "wcag21a": "A", "wcag2aa": "AA", "wcag21aa": "AA", "wcag22aa": "AA"}


def _path(url: object) -> Optional[str]:
    """The page's path: no scheme, host, credentials, query or fragment. A text that is not a full URL names no path."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return (parts.path or "/")[:PATH_LIMIT]


def _tags(raw: object, needs_review: bool) -> Tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
        raise ReportInvalid("invalid_node", what="tags")
    if len(raw) > TAG_LIMIT:
        raise ReportInvalid("invalid_node", what="tags", limit=TAG_LIMIT)
    found: List[str] = []
    seen = set()
    for tag in raw:
        sc = _SC.fullmatch(tag)
        mapped = "wcag-sc:%s.%s.%s" % sc.groups() if sc else ("wcag-level:" + _LEVELS[tag] if tag in _LEVELS
                                                              else tag if tag == "best-practice" else None)
        if mapped is not None and mapped not in seen:
            seen.add(mapped)
            found.append(mapped)
    if needs_review:
        found.append("needs-review")
    return tuple(found)


def _target(raw: object) -> str:
    """The selector chain, joined by `` > ``. A shadow DOM or frame step is itself a list of selectors."""
    if not isinstance(raw, list):
        raise ReportInvalid("invalid_node", what="target")
    steps: List[str] = []
    for item in raw:
        parts = item if isinstance(item, list) else [item]
        if not all(isinstance(p, str) for p in parts):
            raise ReportInvalid("invalid_node", what="target")
        steps.extend(parts)
    return " > ".join(steps)[:TARGET_LIMIT]


def _level(rule: dict, node: dict, incomplete: bool) -> str:
    if incomplete:
        return "note"
    impact = node.get("impact")
    if impact is None:
        impact = rule.get("impact")
    if impact in _ERRORS:
        return "error"
    if impact in _WARNINGS:
        return "warning"
    raise ReportInvalid("invalid_node", what="impact")


def _alerts(doc: dict, key: str, incomplete: bool, path: Optional[str], so_far: int) -> List[Alert]:
    alerts: List[Alert] = []
    for rule in doc[key]:
        if not isinstance(rule, dict) or not isinstance(rule.get("id"), str) or not isinstance(rule.get("nodes"), list):
            raise ReportInvalid("invalid_node", what="rule")
        if len(rule["id"]) > NAME_LIMIT:
            raise ReportInvalid("invalid_name", what="rule id", limit=NAME_LIMIT)
        tags = _tags(rule.get("tags", []), incomplete)
        helped = rule.get("help") if isinstance(rule.get("help"), str) else rule["id"]
        for node in rule["nodes"]:
            if not isinstance(node, dict):
                raise ReportInvalid("invalid_node", what="node")
            if so_far + len(alerts) >= ALERT_LIMIT:
                raise ReportInvalid("too_many_alerts", limit=ALERT_LIMIT)
            alerts.append(Alert(
                tool="axe-core", rule_id=rule["id"], level=_level(rule, node, incomplete), security_severity=None, message=helped,
                path=path, start_line=None, partial_fingerprints={"target": _target(node.get("target")), "url": path}, tags=tags))
    return alerts


def _engine(doc: object) -> str:
    """The version of axe-core that wrote ``doc``, which must be a results object: the engine and the four result lists."""
    engine = doc.get("testEngine") if isinstance(doc, dict) else None
    if (not isinstance(engine, dict) or engine.get("name") != "axe-core" or not isinstance(engine.get("version"), str)
            or not all(isinstance(doc.get(key), list) for key in _LISTS)):
        raise ReportInvalid("not_axe_results")
    return engine["version"]


def parse(data: bytes) -> ReportSummary:
    try:
        doc = canonical.loads(data)
    except QaError as exc:
        raise ReportInvalid("invalid_json", exc.message) from exc
    runs = doc if isinstance(doc, list) else [doc]
    if not runs:
        raise ReportInvalid("not_axe_results")
    versions = [_engine(run) for run in runs]
    alerts: List[Alert] = []
    passed = inapplicable = 0
    for run in runs:
        path = _path(run.get("url"))
        alerts.extend(_alerts(run, "violations", False, path, len(alerts)))
        alerts.extend(_alerts(run, "incomplete", True, path, len(alerts)))
        passed += len(run["passes"])
        inapplicable += len(run["inapplicable"])
    version = ", ".join(dict.fromkeys(versions))
    if len(version) > VERSION_LIMIT:
        raise ReportInvalid("invalid_name", what="tool version", limit=VERSION_LIMIT)
    return ReportSummary("axe", "alerts", (), tuple(alerts), len(runs), (), tool_version=version,
                         rules_passed=passed, rules_inapplicable=inapplicable)
