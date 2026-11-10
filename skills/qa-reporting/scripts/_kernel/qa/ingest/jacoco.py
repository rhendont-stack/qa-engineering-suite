"""JaCoCo XML: ``<package>`` holding ``<sourcefile>`` elements, each with LINE and BRANCH ``<counter>`` totals and per-line counts."""
from __future__ import annotations

from typing import Dict, List, Optional

from qa.ingest import FileCoverage, ReportInvalid, ReportSummary, count, coverage_summary, file_coverage
from qa.ingest.xmlsafe import local, parse_xml


def _doctype(name: str, system: Optional[str], public: Optional[str]) -> bool:
    return name == "report" and system == "report.dtd" and public is not None and public.startswith("-//JACOCO//DTD Report")


def parse(data: bytes) -> ReportSummary:
    root = parse_xml(data, _doctype)
    if local(root.tag) != "report":
        raise ReportInvalid("not_jacoco", root=local(root.tag))

    files: List[FileCoverage] = []
    # (children, package name): groups hold packages, and a deeply nested document must not exhaust the recursion limit.
    stack = [(iter(root), "")]
    while stack:
        children, package = stack[-1]
        child = next(children, None)
        if child is None:
            stack.pop()
            continue
        tag = local(child.tag)
        if tag == "counter":
            _counter(child)
        elif tag == "package":
            stack.append((iter(child), child.get("name") or ""))
        elif tag == "sourcefile":
            name = child.get("name")
            if name:
                files.append(_sourcefile(child, "%s/%s" % (package, name) if package else name))
        elif tag in ("group", "class", "method"):
            stack.append((iter(child), package))
    return coverage_summary("jacoco", files)


def _counter(element) -> Optional[tuple]:
    missed, covered = count(element.get("missed"), "missed"), count(element.get("covered"), "covered")
    return element.get("type"), missed, covered


def _sourcefile(element, path: str) -> FileCoverage:
    counters: Dict[str, tuple] = {}
    lines = []
    for child in element:
        tag = local(child.tag)
        if tag == "counter":
            kind, missed, covered = _counter(child)
            counters[kind] = (missed + covered, covered)
        elif tag == "line":
            lines.append(tuple(count(child.get(attr, "0"), attr) for attr in ("mi", "ci", "mb", "cb")))
            count(child.get("nr"), "nr")
    derived = {"LINE": (len(lines), sum(1 for mi, ci, _mb, _cb in lines if ci > 0)),
               "BRANCH": (sum(mb + cb for _mi, _ci, mb, cb in lines), sum(cb for _mi, _ci, _mb, cb in lines))}
    # The sourcefile's own counters are what the tool counted; without them its lines are.
    lines_total, lines_covered = counters.get("LINE", derived["LINE"])
    branches_total, branches_covered = counters.get("BRANCH", derived["BRANCH"])
    return file_coverage(path, lines_total, lines_covered, branches_total, branches_covered)
