"""Cobertura XML (coverage.py, Istanbul, Cobertura itself, JaCoCo converters): ``<class filename>`` with a ``<lines>`` list of ``<line>``.

A ``<line>`` inside a ``<method>`` repeats one of the class's own lines, so only the class's direct ``<lines>`` count. Several
classes can name one file (Java inner classes): their lines merge by number.
"""
from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Tuple

from qa.ingest import FileCoverage, ReportInvalid, ReportSummary, add_anomaly, checked, count, coverage_summary, file_coverage
from qa.ingest.xmlsafe import local, parse_xml

_DTD = re.compile(r"http://cobertura\.sourceforge\.net/xml/coverage-0[1-9]\.dtd")
_CONDITION = re.compile(r"\(([0-9]+)/([0-9]+)\)\s*$")


def _doctype(name: str, system: Optional[str], public: Optional[str]) -> bool:
    return name == "coverage" and public is None and system is not None and bool(_DTD.fullmatch(system))


def _rate(value: Optional[str], what: str) -> None:
    if value is None:
        return
    try:
        number = float(value)
    except ValueError:
        raise ReportInvalid("invalid_count", what=what) from None
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ReportInvalid("invalid_count", what=what)


def _check_root(root) -> None:
    pairs = (("lines-valid", "lines-covered"), ("branches-valid", "branches-covered"))
    for valid_name, covered_name in pairs:
        valid, covered = root.get(valid_name), root.get(covered_name)
        if valid is not None or covered is not None:
            checked(count(covered if covered is not None else "0", covered_name), count(valid if valid is not None else "0", valid_name),
                    valid_name.split("-")[0])
    _rate(root.get("line-rate"), "line-rate")
    _rate(root.get("branch-rate"), "branch-rate")


def parse(data: bytes) -> ReportSummary:
    root = parse_xml(data, _doctype)
    if local(root.tag) != "coverage":
        raise ReportInvalid("not_cobertura", root=local(root.tag))
    _check_root(root)

    files: Dict[str, Dict[str, Tuple[int, int, int]]] = {}  # filename -> line number -> (hits, branches covered, branches total)
    anomalies: List[str] = []
    stack = [iter(root)]  # explicit, so a deeply nested document cannot exhaust the interpreter's recursion limit
    while stack:
        child = next(stack[-1], None)
        if child is None:
            stack.pop()
            continue
        if local(child.tag) != "class":
            stack.append(iter(child))
            continue
        filename = child.get("filename")
        if not filename:
            add_anomaly(anomalies, "partial")
            continue
        lines = files.setdefault(filename, {})
        for block in child:
            if local(block.tag) != "lines":
                continue
            for line in block:
                if local(line.tag) == "line":
                    _add_line(lines, line)

    result: List[FileCoverage] = []
    for filename, lines in files.items():
        covered = sum(1 for hits, _c, _t in lines.values() if hits > 0)
        result.append(file_coverage(filename, len(lines), covered, sum(t for _h, _c, t in lines.values()),
                                    sum(c for _h, c, _t in lines.values())))
    return coverage_summary("cobertura", result, anomalies)


def _add_line(lines: Dict[str, Tuple[int, int, int]], line) -> None:
    number = line.get("number")
    count(number, "line number")
    hits = count(line.get("hits", "0"), "hits")
    covered = total = 0
    condition = line.get("condition-coverage")
    if condition is not None:
        found = _CONDITION.search(condition)
        if found is None:
            raise ReportInvalid("invalid_count", what="condition-coverage")
        covered, total = count(found.group(1), "condition-coverage"), count(found.group(2), "condition-coverage")
        checked(covered, total, "branches")
    old_hits, old_covered, old_total = lines.get(number, (0, 0, 0))
    lines[number] = (old_hits + hits, max(old_covered, covered), max(old_total, total))
