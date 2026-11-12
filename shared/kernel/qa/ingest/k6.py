"""k6 summaries: the object k6 passes to ``handleSummary()``, written as JSON.

Each threshold is one test case (``k6::<metric>::<expression>``), passed when k6 says ``ok``; the metrics are what the
run measured, kept beside the cases. A summary without thresholds has no tests, and the status rules read it as such.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

from qa import canonical
from qa.errors import QaError
from qa.ingest import CaseResult, Metric, ReportInvalid, ReportSummary

METRIC_TYPES = ("counter", "gauge", "rate", "trend")
NAME_LIMIT = 200  # a metric, a statistic or a threshold expression longer than this is not a name k6 wrote


def _name(value: str, what: str) -> str:
    if len(value) > NAME_LIMIT:
        raise ReportInvalid("invalid_name", what=what, limit=NAME_LIMIT)
    return value


def _number(value: object, what: str) -> float:
    """A finite JSON number; a boolean, a string or a null is not one, nor an integer literal past float range (the loader
    keeps those as ``int``, and every limit compared with them is a float)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportInvalid("invalid_metric_value", what=what)
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ReportInvalid("invalid_metric_value", what=what)
    return value


def _metric(name: str, raw: object) -> Metric:
    if not isinstance(raw, dict) or raw.get("type") not in METRIC_TYPES or not isinstance(raw.get("values"), dict):
        raise ReportInvalid("invalid_metric_value", what="metric", metric=name)
    values: Dict[str, float] = {}
    for stat, value in raw["values"].items():
        values[_name(stat, "statistic")] = _number(value, "statistic")
    return Metric(name, raw["type"], values)


def _cases(name: str, thresholds: object) -> List[CaseResult]:
    if thresholds is None:
        return []
    if not isinstance(thresholds, dict):
        raise ReportInvalid("invalid_threshold", metric=name)
    cases = []
    for expression in sorted(thresholds):
        verdict = thresholds[expression]
        ok = verdict.get("ok") if isinstance(verdict, dict) else None
        if not isinstance(ok, bool):
            raise ReportInvalid("invalid_threshold", metric=name)
        _name(expression, "threshold")
        cases.append(CaseResult(
            test_id="k6::%s::%s" % (name, expression), name=expression, classname=name, file=None,
            status="passed" if ok else "failed", failure_kind=None if ok else "assertion", skip_reason=None,
            message=None if ok else "threshold %s on %s was crossed" % (expression, name), duration_ms=None, flaky=False))
    return cases


def _duration_ms(doc: dict) -> Optional[int]:
    """``state.testRunDurationMs`` rounded up, so a run is never read as shorter than k6 measured it."""
    state = doc.get("state")
    if not isinstance(state, dict) or "testRunDurationMs" not in state:
        return None
    return int(math.ceil(_number(state["testRunDurationMs"], "testRunDurationMs")))


def parse(data: bytes) -> ReportSummary:
    try:
        doc = canonical.loads(data)
    except QaError as exc:
        raise ReportInvalid("invalid_json", exc.message) from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("metrics"), dict):
        raise ReportInvalid("not_k6_summary")
    metrics: List[Metric] = []
    cases: List[CaseResult] = []
    for name in sorted(doc["metrics"]):
        raw = doc["metrics"][name]
        _name(name, "metric")
        metrics.append(_metric(name, raw))
        cases.extend(_cases(name, raw.get("thresholds")))
    return ReportSummary("k6", "tests", tuple(cases), (), 1, (), metrics=tuple(metrics), run_duration_ms=_duration_ms(doc))
