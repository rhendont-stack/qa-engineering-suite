"""Shared helpers of the qa-mutation converters: write a mutation-testing-report-schema document that the kernel's own parser
accepts, or refuse with one JSON error. Standard library only, plus the vendored kernel.

A converter never invents a status: it maps the one the tool recorded through a table fixed from the tool's pinned version.
It never runs a mutant and never judges one; the score it prints is the schema's formula over the statuses it wrote, and the
kernel computes the same number again when the report is ingested.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.realpath(__file__))
KERNEL = os.path.join(HERE, "_kernel")
if not os.path.isdir(KERNEL):  # run from the source tree, before the kernel is vendored
    KERNEL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "shared", "kernel")
if not sys.path or sys.path[0] != KERNEL:
    sys.path.insert(0, KERNEL)  # first: this directory holds qa.py, the launcher, which would shadow the kernel package
if "qa" in sys.modules and not hasattr(sys.modules["qa"], "__path__"):
    del sys.modules["qa"]  # the launcher module, not the package

from qa.ingest import ReportInvalid  # noqa: E402
from qa.ingest import mutation as kernel_mutation  # noqa: E402
from qa.ingest import xmlsafe  # noqa: E402

SCHEMA_VERSION = "1.0"           # as StrykerJS writes it; the kernel reads schema versions 1 and 2
THRESHOLDS = {"high": 80, "low": 60}  # the schema's required thresholds, at StrykerJS's defaults; they judge nothing here


class Refusal(Exception):
    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail


def position(line: int, column: int) -> Dict[str, int]:
    return {"line": line, "column": max(1, column)}


def finish(files: Dict[str, Dict[str, Any]], out: Path, tool: str) -> Dict[str, Any]:
    """Write the report to ``out`` once the kernel's parser accepts it; the summary to print."""
    if not any(entry["mutants"] for entry in files.values()):
        raise Refusal("no_mutation_data", "the tool's output names no mutant")
    doc = {"schemaVersion": SCHEMA_VERSION, "thresholds": dict(THRESHOLDS),
           "files": {path: files[path] for path in sorted(files) if files[path]["mutants"]}}
    data = (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        summary = kernel_mutation.parse(data)
    except ReportInvalid as exc:
        raise Refusal("report_invalid", "the converted report is not one the kernel accepts: %s" % (exc.message or exc.code),
                      reason=exc.details.get("detail"))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    counts: Dict[str, int] = {}
    per_file: Dict[str, Dict[str, int]] = {}
    for m in summary.mutants:
        counts[m.status] = counts.get(m.status, 0) + 1
        per_file.setdefault(m.file, {})[m.status] = per_file.setdefault(m.file, {}).get(m.status, 0) + 1
    survivors: List[Dict[str, Any]] = [{"file": path, "id": m["id"], "line": m["location"]["start"]["line"],
                                        "description": m.get("description", m["mutatorName"])}
                                       for path, entry in doc["files"].items() for m in entry["mutants"]
                                       if m["status"] in ("Survived", "NoCoverage")]
    return {"ok": True, "tool": tool, "out": str(out), "files": len(doc["files"]), "mutants": len(summary.mutants),
            "statuses": {k: counts[k] for k in sorted(counts)}, "score": kernel_mutation.score(summary.mutants),
            "score_formula": "100 x (Killed + Timeout) / (Killed + Timeout + Survived + NoCoverage)",
            "by_file": {path: {"statuses": {k: c[k] for k in sorted(c)}, "score": kernel_mutation.score_of_counts(c)}
                        for path, c in sorted(per_file.items())},
            "undetected": survivors,
            "next": "ingest this file with the kernel (`ingest mutation <file> --from <EXE>`) before quoting any number"}


def parse_xml(data: bytes):
    """The document's root element, read by the kernel's hardened reader: no DOCTYPE, no entity, nothing fetched."""
    try:
        return xmlsafe.parse_xml(data)
    except ReportInvalid as exc:
        raise Refusal("report_invalid", "the XML cannot be read safely: %s" % exc.details.get("detail"))


def emit(body: Dict[str, Any]) -> None:
    print(json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False))


def main_wrapper(run, argv: Optional[List[str]] = None) -> int:
    try:
        emit(run(argv))
        return 0
    except Refusal as exc:
        emit({"ok": False, "error": {"code": exc.code, "message": exc.message, **exc.detail}})
        return 2
