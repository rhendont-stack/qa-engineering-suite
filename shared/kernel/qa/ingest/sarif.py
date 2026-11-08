"""SARIF 2.1.0: one Alert per result. Code snippets are never copied."""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from qa import canonical
from qa.errors import QaError
from qa.ingest import Alert, ReportInvalid, ReportSummary


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _severity(*sources: dict) -> Optional[float]:
    for props in sources:
        raw = _dict(props).get("security-severity")
        if isinstance(raw, bool):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _tags(*sources: dict) -> Tuple[str, ...]:
    for props in sources:
        tags = _dict(props).get("tags")
        if isinstance(tags, list) and tags:
            return tuple(t for t in tags if isinstance(t, str))
    return ()


def parse(data: bytes) -> ReportSummary:
    try:
        doc = canonical.loads(data)
    except QaError as exc:
        raise ReportInvalid("invalid_json", exc.message) from exc
    if not isinstance(doc, dict) or doc.get("version") != "2.1.0" or not isinstance(doc.get("runs"), list):
        raise ReportInvalid("not_sarif_2_1_0")

    alerts: List[Alert] = []
    for run in doc["runs"]:
        driver = _dict(_dict(_dict(run).get("tool")).get("driver"))
        tool = driver.get("name") if isinstance(driver.get("name"), str) else ""
        rules: Dict[str, dict] = {}
        rule_list = driver.get("rules") if isinstance(driver.get("rules"), list) else []
        for r in rule_list:
            if isinstance(r, dict) and isinstance(r.get("id"), str):
                rules[r["id"]] = r
        results = _dict(run).get("results")
        for res in results if isinstance(results, list) else []:
            res = _dict(res)
            rule_id = res.get("ruleId") if isinstance(res.get("ruleId"), str) else ""
            rule = rules.get(rule_id)
            if rule is None and isinstance(res.get("ruleIndex"), int) and 0 <= res["ruleIndex"] < len(rule_list):
                rule = _dict(rule_list[res["ruleIndex"]])
            rule = rule or {}
            level = res.get("level") or _dict(rule.get("defaultConfiguration")).get("level") or "warning"
            locs = res.get("locations") if isinstance(res.get("locations"), list) else []
            phys = _dict(_dict(locs[0]).get("physicalLocation")) if locs else {}
            uri = _dict(phys.get("artifactLocation")).get("uri")
            line = _dict(phys.get("region")).get("startLine")
            fingerprints = res.get("partialFingerprints")
            text = _dict(res.get("message")).get("text")
            alerts.append(Alert(
                tool=tool, rule_id=rule_id, level=str(level),
                security_severity=_severity(res.get("properties"), rule.get("properties")),
                message=text if isinstance(text, str) else "",
                path=uri if isinstance(uri, str) else None,
                start_line=line if isinstance(line, int) and not isinstance(line, bool) else None,
                partial_fingerprints=dict(fingerprints) if isinstance(fingerprints, dict) else {},
                tags=_tags(res.get("properties"), rule.get("properties"))))
    return ReportSummary("sarif", "alerts", (), tuple(alerts), len(doc["runs"]), ())
