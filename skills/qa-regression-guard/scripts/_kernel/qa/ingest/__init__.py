"""Turn the reports test tools write (JUnit XML, CTRF JSON, SARIF 2.1.0) into counted results and alerts, and coverage
reports (LCOV, Cobertura, JaCoCo) and mutation reports (mutation-testing-report-schema JSON) into measurements."""
from __future__ import annotations

import json
import re
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
class FileCoverage:
    """One source file's counts, its path as the report wrote it (normalised to ``/``)."""
    path: str
    lines_total: int
    lines_covered: int
    branches_total: int
    branches_covered: int


@dataclass(frozen=True)
class Mutant:
    """One mutant as the mutation report wrote it: ``file`` is its path (normalised to ``/``), ``status`` one of the schema's."""
    file: str
    mutant_id: str
    mutator: str
    line: int
    status: str
    killed_by: Tuple[str, ...]


@dataclass(frozen=True)
class ReportSummary:
    format: str
    kind: str  # tests | alerts | coverage | mutants
    cases: Tuple[CaseResult, ...]
    alerts: Tuple[Alert, ...]
    runs: int
    anomalies: Tuple[str, ...]
    files: Tuple[FileCoverage, ...] = ()
    mutants: Tuple[Mutant, ...] = ()

    @property
    def lines_total(self) -> int:
        return sum(f.lines_total for f in self.files)

    @property
    def lines_covered(self) -> int:
        return sum(f.lines_covered for f in self.files)

    @property
    def branches_total(self) -> int:
        return sum(f.branches_total for f in self.files)

    @property
    def branches_covered(self) -> int:
        return sum(f.branches_covered for f in self.files)

    def coverage_totals(self) -> Dict[str, int]:
        return {"files": len(self.files), "lines_total": self.lines_total, "lines_covered": self.lines_covered,
                "branches_total": self.branches_total, "branches_covered": self.branches_covered}

    def mutation_totals(self) -> dict:
        """The mutants per file and status (only statuses that occur), which is all the gate needs to score any set of paths."""
        by_file: Dict[str, Dict[str, int]] = {}
        for m in self.mutants:
            counts = by_file.setdefault(m.file, {})
            counts[m.status] = counts.get(m.status, 0) + 1
        return {"files": len(by_file), "mutants": len(self.mutants), "by_file": by_file}

    def measurement_totals(self) -> dict:
        return self.mutation_totals() if self.kind == "mutants" else self.coverage_totals()

    @property
    def paths(self) -> Tuple[str, ...]:
        """The source paths the report names, as it wrote them."""
        return tuple(f.path for f in self.files) if self.kind == "coverage" else tuple(dict.fromkeys(m.file for m in self.mutants))

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


_DRIVE = re.compile(r"^[A-Za-z]:/")


def normalize_path(path: str) -> str:
    """A path as a report wrote it, with ``\\`` read as ``/``. Nothing else changes: it is a name the tool gave."""
    return path.replace("\\", "/")


def is_absolute(path: str) -> bool:
    """A path as a report wrote it that starts at a root: ``/`` or a drive letter."""
    norm = normalize_path(path)
    return norm.startswith("/") or bool(_DRIVE.match(norm))


def outside_project(path: str, root: Optional[str] = None) -> bool:
    """Does a recorded path leave the project? It does when it climbs (a ``..`` part) or, once ``root`` is known, is absolute
    and not under it. Such a path is recorded as given and flagged, and is never matched against a ``paths`` glob."""
    norm = normalize_path(path)
    if ".." in norm.split("/"):
        return True
    if root is not None and is_absolute(norm):
        base = normalize_path(root).rstrip("/")
        return not (norm == base or norm.startswith(base + "/"))
    return False


def add_anomaly(anomalies: List[str], name: str) -> None:
    if name not in anomalies:
        anomalies.append(name)


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
        files=tuple(f for s in summaries for f in s.files),
        mutants=tuple(m for s in summaries for m in s.mutants),
    )


_COUNT = re.compile(r"[0-9]{1,15}")


def count(value: object, what: str) -> int:
    """A count as a report wrote it: digits only. A sign, a fraction, NaN, infinity or an absurd length is not a count."""
    if isinstance(value, str) and _COUNT.fullmatch(value.strip()):
        return int(value)
    raise ReportInvalid("invalid_count", what=what)


def checked(covered: int, total: int, what: str) -> None:
    if covered > total:
        raise ReportInvalid("covered_over_total", what=what)


def file_coverage(path: str, lines_total: int, lines_covered: int, branches_total: int, branches_covered: int) -> FileCoverage:
    checked(lines_covered, lines_total, "lines")
    checked(branches_covered, branches_total, "branches")
    return FileCoverage(normalize_path(path), lines_total, lines_covered, branches_total, branches_covered)


def coverage_summary(fmt: str, files: List[FileCoverage], anomalies: Optional[List[str]] = None) -> ReportSummary:
    found = list(anomalies or [])
    if any(outside_project(f.path) for f in files):
        add_anomaly(found, "path_outside_project")
    return ReportSummary(fmt, "coverage", (), (), 1, tuple(found), tuple(files))


def mutation_summary(mutants: List[Mutant], anomalies: Optional[List[str]] = None) -> ReportSummary:
    found = list(anomalies or [])
    if any(outside_project(m.file) for m in mutants):
        add_anomaly(found, "path_outside_project")
    return ReportSummary("mutation", "mutants", (), (), 1, tuple(found), (), tuple(mutants))


# The parsers import the models and helpers above, so they come last.
from qa.ingest import ctrf as _ctrf  # noqa: E402  (after the models they import)
from qa.ingest import cobertura as _cobertura  # noqa: E402
from qa.ingest import jacoco as _jacoco  # noqa: E402
from qa.ingest import junit as _junit  # noqa: E402
from qa.ingest import lcov as _lcov  # noqa: E402
from qa.ingest import mutation as _mutation  # noqa: E402
from qa.ingest import sarif as _sarif  # noqa: E402

_PARSERS = {"junit": _junit.parse, "ctrf": _ctrf.parse, "sarif": _sarif.parse, "lcov": _lcov.parse,
            "cobertura": _cobertura.parse, "jacoco": _jacoco.parse, "mutation": _mutation.parse}
# What each format counts, so a request mixing kinds is refused before anything runs.
FORMAT_KINDS = {"junit": "tests", "ctrf": "tests", "sarif": "alerts", "lcov": "coverage", "cobertura": "coverage",
                "jacoco": "coverage", "mutation": "mutants"}
# Measurement reports are numbers a tool measured. They are recorded beside a run and never decide its status.
MEASUREMENT_KINDS = ("coverage", "mutants")


def is_measurement(fmt: str) -> bool:
    return FORMAT_KINDS.get(fmt) in MEASUREMENT_KINDS
