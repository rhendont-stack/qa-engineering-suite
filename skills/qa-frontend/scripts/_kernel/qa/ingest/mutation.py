"""Mutation-testing-report-schema JSON (StrykerJS writes it; PIT and mutmut reach the kernel through converters that write it).

A report names each source file with its mutants. The kernel reads the status the tool recorded and nothing else about the
verdict: it never runs a mutant and never takes a score from the tool, so the score is always computed from the mutants.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

from qa import canonical
from qa.errors import QaError
from qa.ingest import Mutant, ReportInvalid, ReportSummary, mutation_summary, normalize_path

# The schema's status vocabulary, exactly.
STATUSES = ("Killed", "Survived", "NoCoverage", "Timeout", "CompileError", "RuntimeError", "Ignored", "Pending")
DETECTED = ("Killed", "Timeout")  # the tests noticed the mutant (a timeout is an infinite loop the tests caught)
UNDETECTED = ("Survived", "NoCoverage")
# CompileError, RuntimeError, Ignored and Pending say nothing about the tests, so they are in no score.

_VERSION = re.compile(r"[12](\.[0-9]+)*")  # schema versions 1 and 2, as Stryker writes "1.0" and others "1" or "2.0.1"


def score_of_counts(counts: Dict[str, int]) -> Optional[float]:
    """100 x (Killed + Timeout) / (Killed + Timeout + Survived + NoCoverage); None when there is nothing to divide by."""
    detected = sum(counts.get(status, 0) for status in DETECTED)
    total = detected + sum(counts.get(status, 0) for status in UNDETECTED)
    return None if total == 0 else 100.0 * detected / total


def score(mutants: Iterable[Mutant]) -> Optional[float]:
    counts: Dict[str, int] = {}
    for m in mutants:
        counts[m.status] = counts.get(m.status, 0) + 1
    return score_of_counts(counts)


def _object(value: object, what: str) -> dict:
    if not isinstance(value, dict):
        raise ReportInvalid("not_mutation_report", what=what)
    return value


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReportInvalid("not_mutation_report", what=what)
    return value


def _thresholds(value: object) -> None:
    """Thresholds are percentages: a number from 0 to 100 (``break`` may be null), or the report is not one."""
    for name, limit in _object(value, "thresholds").items():
        if limit is None and name == "break":
            continue
        if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not 0 <= limit <= 100:
            raise ReportInvalid("invalid_count", what="thresholds.%s" % name)


def _line(mutant: dict) -> int:
    start = _object(_object(mutant.get("location"), "location").get("start"), "location.start")
    line = start.get("line")
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:  # lines are 1-based
        raise ReportInvalid("invalid_count", what="line")
    return line


def _mutant(path: str, raw: object) -> Mutant:
    item = _object(raw, "mutant")
    status = item.get("status")
    if status not in STATUSES:  # also a non-string: the schema's eight words, spelled as the schema spells them
        raise ReportInvalid("unknown_status", status=status if isinstance(status, str) else type(status).__name__)
    killed_by = item.get("killedBy", [])
    if not isinstance(killed_by, list) or not all(isinstance(test, str) for test in killed_by):
        raise ReportInvalid("not_mutation_report", what="killedBy")
    return Mutant(normalize_path(path), _text(item.get("id"), "id"), _text(item.get("mutatorName"), "mutatorName"), _line(item),
                  status, tuple(killed_by))


def parse(data: bytes) -> ReportSummary:
    try:
        doc = canonical.loads(data)
    except QaError as exc:
        raise ReportInvalid("invalid_json", exc.message) from exc
    doc = _object(doc, "document")
    if "schemaVersion" not in doc:
        raise ReportInvalid("not_mutation_report", what="schemaVersion")
    version = doc["schemaVersion"]
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ReportInvalid("unsupported_schema_version", version=version if isinstance(version, str) else type(version).__name__)
    if "thresholds" in doc:
        _thresholds(doc["thresholds"])
    mutants: List[Mutant] = []
    seen = set()
    for path, entry in _object(doc.get("files"), "files").items():
        listed = _object(entry, "file").get("mutants")
        if not isinstance(listed, list):
            raise ReportInvalid("not_mutation_report", what="mutants")
        for raw in listed:
            mutant = _mutant(path, raw)
            if (mutant.file, mutant.mutant_id) in seen:
                raise ReportInvalid("duplicate_mutant", mutant=mutant.mutant_id)
            seen.add((mutant.file, mutant.mutant_id))
            mutants.append(mutant)
    # A Pending mutant was never run: the score leaves it out, and the report says it is not the whole picture.
    return mutation_summary(mutants, ["partial"] if any(m.status == "Pending" for m in mutants) else None)
