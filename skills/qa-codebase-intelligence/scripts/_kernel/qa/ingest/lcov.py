"""LCOV tracefiles (lcov, genhtml, coverage.py, Istanbul and V8 write them): one record per source file, ``SF:`` to ``end_of_record``."""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from qa.ingest import FileCoverage, ReportInvalid, ReportSummary, count, coverage_summary, file_coverage

_TAG = re.compile(r"[A-Z][A-Z0-9_]*")


class _Record:
    def __init__(self, path: str) -> None:
        self.path = path
        self.da: Dict[str, int] = {}  # line -> hits
        self.br: Dict[Tuple[str, str], int] = {}  # (line, block, branch) -> taken
        self.lf: Optional[int] = None
        self.lh: Optional[int] = None
        self.brf: Optional[int] = None
        self.brh: Optional[int] = None

    def finish(self) -> FileCoverage:
        # The summary lines (LF, LH, BRF, BRH) are what the tool counted; without them the records are.
        lines_total = self.lf if self.lf is not None else len(self.da)
        lines_covered = self.lh if self.lh is not None else sum(1 for h in self.da.values() if h > 0)
        branches_total = self.brf if self.brf is not None else len(self.br)
        branches_covered = self.brh if self.brh is not None else sum(1 for h in self.br.values() if h > 0)
        return file_coverage(self.path, lines_total, lines_covered, branches_total, branches_covered)

    def merge(self, other: "_Record") -> None:
        """A tracefile merged by concatenation names a file twice: its lines and branches join, hits add, nothing is counted twice."""
        for line, hits in other.da.items():
            self.da[line] = self.da.get(line, 0) + hits
        for key, taken in other.br.items():
            self.br[key] = self.br.get(key, 0) + taken
        self.lf = self.lh = self.brf = self.brh = None  # the per-record summaries no longer describe the union


def parse(data: bytes) -> ReportSummary:
    text = data.decode("utf-8", errors="replace")
    records: Dict[str, _Record] = {}
    current: Optional[_Record] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        key, colon, value = line.partition(":")
        if not colon:
            if line != "end_of_record":
                raise ReportInvalid("not_lcov")
            if current is None:
                raise ReportInvalid("record_without_file")
            if current.path in records:
                records[current.path].merge(current)
            else:
                records[current.path] = current
            current = None
            continue
        if not _TAG.fullmatch(key):
            raise ReportInvalid("not_lcov")
        if key == "SF":
            if current is not None:
                raise ReportInvalid("unterminated_record")
            current = _Record(value)
            continue
        if key in ("TN", "VER"):
            continue
        if current is None:
            raise ReportInvalid("record_without_file")
        if key == "DA":  # DA:<line>,<hits>[,<checksum>]
            fields = value.split(",")
            if len(fields) < 2:
                raise ReportInvalid("invalid_count", what="DA")
            count(fields[0], "DA line")
            hits = count(fields[1], "DA hits")
            current.da[fields[0].strip()] = current.da.get(fields[0].strip(), 0) + hits
        elif key == "BRDA":  # BRDA:<line>,<block>,<branch>,<taken or ->
            # The block and branch are names: coverage.py writes "jump to line 3" for the branch. Only the line and the count are numbers.
            fields = value.rsplit(",", 1)[0].split(",", 1) + value.rsplit(",", 1)[1:]
            if len(fields) != 3 or value.count(",") < 3:
                raise ReportInvalid("invalid_count", what="BRDA")
            count(fields[0], "BRDA line")
            taken = 0 if fields[2].strip() == "-" else count(fields[2], "BRDA taken")
            where = (fields[0].strip(), fields[1])  # the line, and the block and branch names together
            current.br[where] = current.br.get(where, 0) + taken
        elif key in ("LF", "LH", "BRF", "BRH"):
            setattr(current, key.lower(), count(value, key))
        # FN, FNDA, FNF, FNH and any other tag name functions or are newer than this reader: they are not counted.
    if current is not None:
        raise ReportInvalid("unterminated_record")
    return coverage_summary("lcov", [r.finish() for r in records.values()])
