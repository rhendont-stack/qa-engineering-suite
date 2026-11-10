"""Turn the reports test tools write (JUnit XML, CTRF JSON, SARIF 2.1.0) into counted results and alerts."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from qa.errors import QaError
from qa.workspace import FileTooLarge, read_limited


class ReportInvalid(QaError):
    """A report that cannot be trusted as input. `details["detail"]` says why."""

    def __init__(self, detail: str, message: str = "", **details: object) -> None:
        super().__init__("report_invalid", message or detail, detail=detail, **details)


@dataclass(frozen=True)
class CaseResult:
    test_id: str
    name: str
    classname: Optional[str]
    file: Optional[str]
    status: str  # passed | failed | not_run
    failure_kind: Optional[str]  # assertion | error
    skip_reason: Optional[str]
    message: Optional[str]
    duration_ms: Optional[int]
    flaky: bool


@dataclass(frozen=True)
class Alert:
    tool: str
    rule_id: str
    level: str
    security_severity: Optional[float]
    message: str
    path: Optional[str]
    start_line: Optional[int]
    partial_fingerprints: Dict[str, object]
    tags: Tuple[str, ...]


@dataclass(frozen=True)
class ReportSummary:
    format: str
    kind: str  # tests | alerts
    cases: Tuple[CaseResult, ...]
    alerts: Tuple[Alert, ...]
    runs: int
    anomalies: Tuple[str, ...]

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.status == "passed")

    @property
    def failed(self) -> int:
        return sum(1 for c in self.cases if c.status == "failed" and c.failure_kind == "assertion")

    @property
    def errors(self) -> int:
        return sum(1 for c in self.cases if c.status == "failed" and c.failure_kind == "error")

    @property
    def skipped(self) -> int:
        return sum(1 for c in self.cases if c.status == "not_run")

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped


def add_anomaly(anomalies: List[str], name: str) -> None:
    if name not in anomalies:
        anomalies.append(name)


from qa.ingest import ctrf as _ctrf  # noqa: E402  (after the models they import)
from qa.ingest import junit as _junit  # noqa: E402
from qa.ingest import sarif as _sarif  # noqa: E402

_PARSERS = {"junit": _junit.parse, "ctrf": _ctrf.parse, "sarif": _sarif.parse}
# What each format counts, so a request mixing kinds is refused before anything runs.
FORMAT_KINDS = {"junit": "tests", "ctrf": "tests", "sarif": "alerts"}


def read_report(path: Path, max_bytes: int) -> bytes:
    """A report's bytes. The path is the user's, so a symlink is followed, but only to a regular file under the cap."""
    try:
        return read_limited(Path(path), max_bytes, follow=True)
    except FileTooLarge as exc:
        raise ReportInvalid("too_large", max_bytes=max_bytes) from exc
    except OSError as exc:
        raise ReportInvalid("unreadable", str(exc)) from exc


def parse_report(fmt: str, path: Path, max_bytes: int) -> ReportSummary:
    if fmt not in _PARSERS:
        raise ReportInvalid("unknown_format", format=fmt)
    return parse_bytes(fmt, read_report(path, max_bytes))


def parse_bytes(fmt: str, data: bytes) -> ReportSummary:
    parser = _PARSERS.get(fmt)
    if parser is None:
        raise ReportInvalid("unknown_format", format=fmt)
    try:
        summary = parser(data)
        # JSON can spell a lone surrogate (\ud800) or nest too deep to dump; the ledger could store neither.
        json.dumps(asdict(summary), ensure_ascii=False).encode("utf-8")
    except (UnicodeEncodeError, RecursionError) as exc:
        raise ReportInvalid("not_storable", type(exc).__name__) from exc
    return summary


def merge(summaries: List[ReportSummary]) -> ReportSummary:
    if not summaries:
        raise QaError("nothing_to_merge", "merge needs at least one summary")
    kinds = {s.kind for s in summaries}
    if len(kinds) > 1:
        raise QaError("kind_mismatch", "cannot merge reports of different kinds", kinds=sorted(kinds))
    formats = {s.format for s in summaries}
    anomalies: List[str] = []
    for s in summaries:
        for a in s.anomalies:
            add_anomaly(anomalies, a)
    return ReportSummary(
        format=formats.pop() if len(formats) == 1 else "mixed",
        kind=summaries[0].kind,
        cases=tuple(c for s in summaries for c in s.cases),
        alerts=tuple(a for s in summaries for a in s.alerts),
        runs=sum(s.runs for s in summaries),
        anomalies=tuple(anomalies),
    )
