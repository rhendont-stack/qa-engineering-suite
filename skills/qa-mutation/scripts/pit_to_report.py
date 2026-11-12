"""Convert a PIT ``mutations.xml`` into a mutation-testing-report-schema JSON file the kernel can ingest.

Usage: python3 pit_to_report.py --xml FILE --out FILE [--source-root DIR]

PIT mutates bytecode in memory and writes ``mutations.xml`` (``outputFormats`` XML; with the Gradle plugin under
``build/reports/pitest/``, with Maven under ``target/pit-reports/``). Each ``<mutation>`` names its status, source file, class,
method, line, mutator, description and killing test. The converter maps the status through a table fixed from PIT 1.30.2,
names the file ``<source root>/<package path>/<source file>`` (``--source-root`` such as ``src/main/java``; the source text is
read from there when it exists) and numbers the mutants in the order PIT wrote them. The XML goes through the kernel's hardened
reader: a DOCTYPE or an entity is refused. The report is written only once the kernel's own parser accepts it.

Prints one JSON object (``ok``, ``out``, ``files``, ``mutants``, ``statuses``, ``score``, ``undetected``). Exit 0 when
written, 2 when refused (``report_invalid``, ``unknown_status``, ``no_mutation_data``). Reads only; writes only ``--out``.

PIT's own percentage counts every status it calls detected, NON_VIABLE, MEMORY_ERROR and RUN_ERROR among them; the schema's
formula leaves those out. On a run without them the two numbers are the same.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import _mutation  # noqa: E402

# PIT 1.30.2 DetectionStatus -> the schema's status. NON_VIABLE: the mutated bytecode is invalid, as a compile error.
# MEMORY_ERROR and RUN_ERROR: the run failed, which says nothing about the tests. NOT_STARTED and STARTED: never finished.
# EQUIVALENT (written by extensions that prove a mutant equivalent): out of every score.
STATUS = {"KILLED": "Killed", "SURVIVED": "Survived", "NO_COVERAGE": "NoCoverage", "TIMED_OUT": "Timeout",
          "NON_VIABLE": "CompileError", "MEMORY_ERROR": "RuntimeError", "RUN_ERROR": "RuntimeError", "NOT_STARTED": "Pending",
          "STARTED": "Pending", "EQUIVALENT": "Ignored"}


def _text(element, name: str) -> str:
    child = element.find(name)
    return (child.text or "").strip() if child is not None else ""


def _path(source_root: Optional[str], mutated_class: str, source_file: str) -> str:
    package = mutated_class.split("$", 1)[0].rpartition(".")[0]
    parts = ([source_root.strip("/")] if source_root else []) + ([package.replace(".", "/")] if package else []) + [source_file]
    return str(PurePosixPath(*parts))


def convert(data: bytes, source_root: Optional[str]) -> Dict[str, Dict[str, Any]]:
    root = _mutation.parse_xml(data)
    if root.tag != "mutations":
        raise _mutation.Refusal("report_invalid", "not a PIT mutations report", root=str(root.tag)[:40])
    files: Dict[str, Dict[str, Any]] = {}
    sources: Dict[str, List[str]] = {}
    for number, item in enumerate(root.findall("mutation"), 1):
        status = item.get("status")
        if status not in STATUS:
            raise _mutation.Refusal("unknown_status", "PIT wrote a status this converter has no mapping for", status=str(status)[:40])
        source_file, mutated_class, mutator = _text(item, "sourceFile"), _text(item, "mutatedClass"), _text(item, "mutator")
        line_text = _text(item, "lineNumber")
        if not source_file or not mutated_class or not mutator or not line_text.isdigit() or int(line_text) < 1:
            raise _mutation.Refusal("report_invalid", "a mutation without its source file, class, mutator or a line from 1",
                                    mutation=number)
        line = int(line_text)
        path = _path(source_root, mutated_class, source_file)
        if path not in files:
            text = ""
            if source_root and Path(path).is_file():
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            files[path] = {"language": "java" if source_file.endswith(".java") else "kotlin" if source_file.endswith(".kt") else "jvm",
                           "source": text, "mutants": []}
            sources[path] = text.splitlines()
        killers = [t for t in _text(item, "killingTest").split("|") if t]
        source_line = sources[path][line - 1] if line <= len(sources[path]) else ""
        method = _text(item, "mutatedMethod")
        mutant = {"id": str(number), "mutatorName": mutator.rpartition(".")[2], "status": STATUS[status],
                  "statusReason": "PIT: %s" % status,
                  "location": {"start": _mutation.position(line, 1), "end": _mutation.position(line, len(source_line) + 1)},
                  "description": "%s (%s.%s)" % (_text(item, "description") or mutator.rpartition(".")[2], mutated_class, method)}
        if killers:
            mutant["killedBy"] = killers
        files[path]["mutants"].append(mutant)
    return files


def run(argv: Optional[List[str]]) -> Dict[str, Any]:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--xml", required=True, help="PIT's mutations.xml")
    parser.add_argument("--out", required=True, help="the report file to write")
    parser.add_argument("--source-root", help="where the sources live, relative to the current directory (e.g. src/main/java)")
    args = parser.parse_args(argv)
    try:
        data = Path(args.xml).read_bytes()
    except OSError:
        raise _mutation.Refusal("no_mutation_data", "the XML file cannot be read", xml=args.xml)
    return _mutation.finish(convert(data, args.source_root), Path(args.out), "pit")


if __name__ == "__main__":
    sys.exit(_mutation.main_wrapper(run, sys.argv[1:]))
