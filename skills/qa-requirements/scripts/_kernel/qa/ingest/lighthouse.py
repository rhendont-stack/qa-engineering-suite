"""Lighthouse results (the LHR JSON): category scores and the lab metrics, as measured numbers.

A lab run on a throttled or shared machine is a measurement, not a verdict: the metrics are recorded beside the run and
never decide its status. A result whose run failed (``runtimeError``) measured nothing and is refused.
"""
from __future__ import annotations

from typing import List

from qa import canonical
from qa.errors import QaError
from qa.ingest import Metric, ReportInvalid, ReportSummary
from qa.ingest.k6 import number

NAME_LIMIT = 200
LAB_AUDITS = ("first-contentful-paint", "largest-contentful-paint", "total-blocking-time", "cumulative-layout-shift", "speed-index",
              "interactive")


def _scores(categories: dict) -> List[Metric]:
    metrics = []
    for cid in sorted(categories):
        if len(cid) > NAME_LIMIT:
            raise ReportInvalid("invalid_name", what="category", limit=NAME_LIMIT)
        category = categories[cid]
        score = category.get("score") if isinstance(category, dict) else None
        if score is None and isinstance(category, dict):
            continue  # a category Lighthouse could not score
        value = number(score, "score")
        if not 0 <= value <= 1:
            raise ReportInvalid("invalid_metric_value", what="score", category=cid)
        metrics.append(Metric("category:" + cid, "score", {"score": value}))
    return metrics


def _lab(audits: dict) -> List[Metric]:
    metrics = []
    for name in LAB_AUDITS:
        audit = audits.get(name)
        if audit is None:
            continue
        if not isinstance(audit, dict):
            raise ReportInvalid("invalid_metric_value", what="audit", audit=name)
        if audit.get("numericValue") is None:
            continue  # an audit that errored or did not apply carries no number
        unit = audit.get("numericUnit")
        if not isinstance(unit, str) or not unit or len(unit) > NAME_LIMIT:
            raise ReportInvalid("invalid_metric_value", what="numericUnit", audit=name)
        metrics.append(Metric(name, "lab:" + unit, {"value": number(audit["numericValue"], "numericValue")}))
    return metrics


def parse(data: bytes) -> ReportSummary:
    try:
        doc = canonical.loads(data)
    except QaError as exc:
        raise ReportInvalid("invalid_json", exc.message) from exc
    if (not isinstance(doc, dict) or not isinstance(doc.get("lighthouseVersion"), str)
            or not isinstance(doc.get("categories"), dict) or not isinstance(doc.get("audits"), dict)):
        raise ReportInvalid("not_lighthouse_result")
    if len(doc["lighthouseVersion"]) > NAME_LIMIT:
        raise ReportInvalid("invalid_name", what="tool version", limit=NAME_LIMIT)
    error = doc.get("runtimeError")
    if error is not None:
        code = error.get("code") if isinstance(error, dict) else None
        raise ReportInvalid("lighthouse_runtime_error", runtime_code=code[:NAME_LIMIT] if isinstance(code, str) else None)
    return ReportSummary("lighthouse", "metrics", (), (), 1, (), metrics=tuple(_scores(doc["categories"]) + _lab(doc["audits"])),
                         tool_version=doc["lighthouseVersion"])
