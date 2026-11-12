"""CTRF JSON (results.tests[])."""
from __future__ import annotations

from typing import List, Optional

from qa import canonical
from qa.errors import QaError
from qa.ingest import CaseResult, ReportInvalid, ReportSummary, add_anomaly

_STATUS = {"passed": "passed", "failed": "failed", "skipped": "not_run", "pending": "not_run", "other": "not_run"}
_MESSAGE_LIMIT = 1000


def _text(value: object) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def parse(data: bytes) -> ReportSummary:
    try:
        doc = canonical.loads(data)
    except QaError as exc:
        raise ReportInvalid("invalid_json", exc.message) from exc
    results = doc.get("results") if isinstance(doc, dict) else None
    tests = results.get("tests") if isinstance(results, dict) else None
    if not isinstance(tests, list):
        raise ReportInvalid("not_ctrf")

    cases: List[CaseResult] = []
    anomalies: List[str] = []
    for index, t in enumerate(tests):
        if not isinstance(t, dict):
            raise ReportInvalid("not_ctrf", index=index)
        name = _text(t.get("name"))
        if name is None:
            add_anomaly(anomalies, "partial")
            name = "#%d" % index
        status = _STATUS.get(t.get("status")) if isinstance(t.get("status"), str) else None
        if status is None:
            add_anomaly(anomalies, "partial")
            status = "not_run"
        retries = t.get("retries")
        retried = isinstance(retries, int) and not isinstance(retries, bool) and retries > 0
        if retried:
            add_anomaly(anomalies, "retried_in_framework")
        flaky = status == "passed" and (t.get("flaky") is True or retried)
        file_path = _text(t.get("filePath"))
        prefix = file_path or _text(t.get("suite")) or ""
        message = _text(t.get("message"))
        if message is not None:
            message = message[:_MESSAGE_LIMIT]
        duration = t.get("duration")
        duration_ms = int(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else None
        cases.append(CaseResult(
            test_id="%s::%s" % (prefix, name), name=name, classname=None, file=file_path, status=status,
            failure_kind="assertion" if status == "failed" else None,
            skip_reason=message if status == "not_run" else None,
            message=message if status == "failed" else None,
            duration_ms=duration_ms, flaky=flaky))
    return ReportSummary("ctrf", "tests", tuple(cases), (), 1, tuple(anomalies))
