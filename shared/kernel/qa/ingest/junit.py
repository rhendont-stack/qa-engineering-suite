"""JUnit XML: tolerant of producer differences, counts <testcase> elements, refuses DTDs and entities."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import List, Optional

from qa.ingest import CaseResult, ReportInvalid, ReportSummary, add_anomaly
from qa.ingest.xmlsafe import local as _local
from qa.ingest.xmlsafe import parse_xml

_FLAKY_OR_RERUN = ("flakyFailure", "flakyError", "rerunFailure", "rerunError")
_MESSAGE_LIMIT = 1000


def _duration_ms(value: Optional[str]) -> Optional[int]:
    try:
        return int(round(float(value) * 1000)) if value is not None else None
    except (ValueError, OverflowError):
        return None


def parse(data: bytes) -> ReportSummary:
    root = parse_xml(data)
    if _local(root.tag) not in ("testsuites", "testsuite"):
        raise ReportInvalid("not_junit", root=_local(root.tag))

    cases: List[CaseResult] = []
    anomalies: List[str] = []

    # Explicit stack, so a deeply nested document cannot exhaust the interpreter's recursion limit.
    root_name = (root.get("name") or "") if _local(root.tag) == "testsuite" else ""
    stack = [(iter(root), root_name)]
    while stack:
        children, suite_name = stack[-1]
        child = next(children, None)
        if child is None:
            stack.pop()
            continue
        tag = _local(child.tag)
        if tag == "testsuite":
            stack.append((iter(child), child.get("name") or ""))
        elif tag == "testsuites":
            stack.append((iter(child), suite_name))
        elif tag == "testcase":
            cases.append(_case(child, len(cases), suite_name, anomalies))
    return ReportSummary("junit", "tests", tuple(cases), (), 1, tuple(anomalies))


def _case(tc: ET.Element, index: int, suite_name: str, anomalies: List[str]) -> CaseResult:
    classname = tc.get("classname") or None
    name = tc.get("name")
    if not name:
        add_anomaly(anomalies, "partial")
        name = "#%d" % index
    test_id = "%s::%s" % (classname or suite_name or "", name)

    found = {}
    for child in tc:
        found.setdefault(_local(child.tag), child)
    if any(t in found for t in _FLAKY_OR_RERUN):
        add_anomaly(anomalies, "retried_in_framework")

    status, kind, skip_reason, message = "passed", None, None, None
    if "failure" in found:
        status, kind, message = "failed", "assertion", found["failure"].get("message")
    elif "error" in found:
        status, kind, message = "failed", "error", found["error"].get("message")
    elif "skipped" in found:
        status, skip_reason = "not_run", found["skipped"].get("message")
    flaky = status == "passed" and ("flakyFailure" in found or "flakyError" in found)
    if message is not None:
        message = message[:_MESSAGE_LIMIT]
    return CaseResult(test_id, name, classname, tc.get("file"), status, kind, skip_reason, message,
                      _duration_ms(tc.get("time")), flaky)
