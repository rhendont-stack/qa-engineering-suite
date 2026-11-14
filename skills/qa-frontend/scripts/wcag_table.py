"""Writes the WCAG 2.2 level A and AA table: one entry per success criterion, saying what checked it and with what result.

Usage: python3 wcag_table.py [--axe-execution EXE] [--axe-report PATH] [--suspected SC=FND-NNN ...] [--out PATH]

It writes ``.qa/work/frontend/wcag-table.json`` (``--out`` names another file under ``.qa/work/frontend/``): the 55 success
criteria of WCAG 2.2 at levels A and AA (4.1.1, removed in 2.2, is not one of them), each with its level, its title, what
can decide it, a ``status`` in the kernel's words and a ``reason``:

* A criterion that some axe-core rule tests (``AXE_DECIDABLE``: the criteria the rules run under the tags ``wcag2a``,
  ``wcag2aa``, ``wcag21a``, ``wcag21aa`` and ``wcag22aa`` name, axe-core 4.14.0). Without ``--axe-report`` it is
  ``not_run``, reason ``tool-missing``: axe-core did not run here (``--axe-execution`` names the run that was recorded
  ``blocked``, when there was one). With ``--axe-report`` (the stored ``axe.json`` of a run under ``.qa/evidence/``): a
  criterion a violation names is ``failed`` (``axe-violation``, with the rules), one only an incomplete check names is
  ``inconclusive`` (``needs-review``), and any other is ``inconclusive`` (``no-axe-violation``): axe's rules cover part
  of a criterion, so a person still decides it.
* Every other criterion is ``not_run``, reason ``manual-only``: no automated rule decides it, and a person checks it
  (``references/wcag-manual.md``).

No entry is ever ``passed``, and the table holds no verdict on the standard as a whole: an automated run decides part of
some criteria at most. ``--suspected 1.1.1=FND-001`` lists a recorded finding against a criterion (a static lead, for
example) without changing its status. Written through no link, by exclusive create of a temporary file and a rename.
Prints one JSON summary. Exit 0 on success, 2 on bad arguments, a refused path or an unreadable report. Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Sequence, Set

HERE = Path(__file__).resolve().parent
OUT = ".qa/work/frontend/wcag-table.json"
MAX_REPORT = 16 * 1024 * 1024
EXECUTION = re.compile(r"EXE-[0-9A-Z]{26}")
FINDING = re.compile(r"FND-[0-9]{3,}")
AXE_TAG = re.compile(r"wcag([1-4])([0-9])([0-9]{1,2})")

# WCAG 2.2, levels A and AA: number -> (level, title).
WCAG22 = {
    "1.1.1": ("A", "Non-text Content"), "1.2.1": ("A", "Audio-only and Video-only (Prerecorded)"),
    "1.2.2": ("A", "Captions (Prerecorded)"), "1.2.3": ("A", "Audio Description or Media Alternative (Prerecorded)"),
    "1.2.4": ("AA", "Captions (Live)"), "1.2.5": ("AA", "Audio Description (Prerecorded)"),
    "1.3.1": ("A", "Info and Relationships"), "1.3.2": ("A", "Meaningful Sequence"), "1.3.3": ("A", "Sensory Characteristics"),
    "1.3.4": ("AA", "Orientation"), "1.3.5": ("AA", "Identify Input Purpose"), "1.4.1": ("A", "Use of Color"),
    "1.4.2": ("A", "Audio Control"), "1.4.3": ("AA", "Contrast (Minimum)"), "1.4.4": ("AA", "Resize Text"),
    "1.4.5": ("AA", "Images of Text"), "1.4.10": ("AA", "Reflow"), "1.4.11": ("AA", "Non-text Contrast"),
    "1.4.12": ("AA", "Text Spacing"), "1.4.13": ("AA", "Content on Hover or Focus"),
    "2.1.1": ("A", "Keyboard"), "2.1.2": ("A", "No Keyboard Trap"), "2.1.4": ("A", "Character Key Shortcuts"),
    "2.2.1": ("A", "Timing Adjustable"), "2.2.2": ("A", "Pause, Stop, Hide"), "2.3.1": ("A", "Three Flashes or Below Threshold"),
    "2.4.1": ("A", "Bypass Blocks"), "2.4.2": ("A", "Page Titled"), "2.4.3": ("A", "Focus Order"),
    "2.4.4": ("A", "Link Purpose (In Context)"), "2.4.5": ("AA", "Multiple Ways"), "2.4.6": ("AA", "Headings and Labels"),
    "2.4.7": ("AA", "Focus Visible"), "2.4.11": ("AA", "Focus Not Obscured (Minimum)"), "2.5.1": ("A", "Pointer Gestures"),
    "2.5.2": ("A", "Pointer Cancellation"), "2.5.3": ("A", "Label in Name"), "2.5.4": ("A", "Motion Actuation"),
    "2.5.7": ("AA", "Dragging Movements"), "2.5.8": ("AA", "Target Size (Minimum)"),
    "3.1.1": ("A", "Language of Page"), "3.1.2": ("AA", "Language of Parts"), "3.2.1": ("A", "On Focus"), "3.2.2": ("A", "On Input"),
    "3.2.3": ("AA", "Consistent Navigation"), "3.2.4": ("AA", "Consistent Identification"), "3.2.6": ("A", "Consistent Help"),
    "3.3.1": ("A", "Error Identification"), "3.3.2": ("A", "Labels or Instructions"), "3.3.3": ("AA", "Error Suggestion"),
    "3.3.4": ("AA", "Error Prevention (Legal, Financial, Data)"), "3.3.7": ("A", "Redundant Entry"),
    "3.3.8": ("AA", "Accessible Authentication (Minimum)"),
    "4.1.2": ("A", "Name, Role, Value"), "4.1.3": ("AA", "Status Messages"),
}
# The criteria some axe-core 4.14.0 rule names by its tag, among the rules that run under the five WCAG A and AA tags.
AXE_DECIDABLE = ("1.1.1", "1.2.2", "1.3.1", "1.3.5", "1.4.1", "1.4.2", "1.4.3", "1.4.4", "1.4.12", "2.1.1", "2.2.1", "2.2.2",
                 "2.4.1", "2.4.2", "2.4.4", "2.5.3", "2.5.8", "3.1.1", "3.1.2", "3.3.2", "4.1.2")
VERDICT = ("None for the standard as a whole. This table records what checked each criterion and what it found; an automated "
           "run decides part of some criteria at most, and every manual-only criterion needs a person's review.")


def _writer():
    """``static_ui_scan.write_new`` and ``Refused``, loaded from beside this file."""
    spec = importlib.util.spec_from_file_location("qa_frontend_static_ui_scan", str(HERE / "static_ui_scan.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_report(text: str, refused) -> List[Dict[str, Any]]:
    """The results objects of a stored axe report: a regular file under ``.qa/evidence/``, reached through no link."""
    parts = PurePosixPath(text).parts
    if os.path.isabs(text) or parts[:2] != (".qa", "evidence") or ".." in parts or "." in parts or len(parts) < 3:
        raise refused("--axe-report must be a stored report under .qa/evidence/")
    here = Path.cwd()
    for part in parts:
        here = here / part
        try:
            mode = os.lstat(str(here)).st_mode
        except OSError:
            raise refused("--axe-report names no file") from None
        if stat.S_ISLNK(mode):
            raise refused("--axe-report passes through a link")
    if not stat.S_ISREG(mode) or os.lstat(str(here)).st_size > MAX_REPORT:
        raise refused("--axe-report is not a regular file of at most 16 MiB")
    try:
        document = json.loads(here.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise refused("--axe-report is not JSON") from None
    runs = document if isinstance(document, list) else [document]
    for run in runs:
        engine = run.get("testEngine") if isinstance(run, dict) else None
        if not isinstance(engine, dict) or engine.get("name") != "axe-core" or not all(
                isinstance(run.get(k), list) for k in ("violations", "incomplete", "passes", "inapplicable")):
            raise refused("--axe-report is not an axe-core results object or array")
    return runs


def criteria_of(rule: Dict[str, Any]) -> Set[str]:
    found = set()
    for tag in rule.get("tags", []) if isinstance(rule.get("tags"), list) else []:
        match = AXE_TAG.fullmatch(tag) if isinstance(tag, str) else None
        if match:
            found.add("%s.%s.%s" % match.groups())
    return found


def axe_outcome(runs: List[Dict[str, Any]]) -> Dict[str, Dict[str, Set[str]]]:
    """``{criterion: {"violations": rules, "incomplete": rules, "ran": rules}}`` over every page of the report."""
    outcome: Dict[str, Dict[str, Set[str]]] = {}
    for run in runs:
        for key in ("violations", "incomplete", "passes", "inapplicable"):
            for rule in run[key]:
                if not isinstance(rule, dict):
                    continue
                for sc in criteria_of(rule):
                    entry = outcome.setdefault(sc, {"violations": set(), "incomplete": set(), "ran": set()})
                    entry["ran"].add(str(rule.get("id")))
                    if key in ("violations", "incomplete"):
                        entry[key].add(str(rule.get("id")))
    return outcome


def table(execution: Optional[str], runs: Optional[List[Dict[str, Any]]], suspected: Dict[str, List[str]]) -> Dict[str, Any]:
    outcome = axe_outcome(runs) if runs is not None else {}
    entries = []
    for sc, (level, title) in WCAG22.items():
        entry: Dict[str, Any] = {"criterion": sc, "level": level, "title": title}
        if sc in AXE_DECIDABLE:
            entry["decided_by"] = "axe-core rules for part of it, then a person"
            seen = outcome.get(sc)
            if runs is None:
                entry.update(status="not_run", reason="tool-missing",
                             note=("axe-core did not run here: %s was recorded blocked" % execution) if execution
                             else "axe-core did not run here")
            elif seen and seen["violations"]:
                entry.update(status="failed", reason="axe-violation", rules=sorted(seen["violations"]),
                             note="axe-core reported a violation of this criterion (%s)" % (execution or "the stored report"))
            elif seen and seen["incomplete"]:
                entry.update(status="inconclusive", reason="needs-review", rules=sorted(seen["incomplete"]),
                             note="axe-core could not decide some elements; a person reviews them")
            else:
                entry.update(status="inconclusive", reason="no-axe-violation", rules=sorted(seen["ran"]) if seen else [],
                             note="axe-core's rules found no violation; they test part of the criterion, and a person decides the rest")
            if execution:
                entry["execution"] = execution
        else:
            entry.update(decided_by="a person", status="not_run", reason="manual-only",
                         note="No automated rule decides it; a person checks it (references/wcag-manual.md)")
        if suspected.get(sc):
            entry["suspected_findings"] = suspected[sc]
        entries.append(entry)
    counts: Dict[str, int] = {}
    for entry in entries:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return {"standard": "WCAG 2.2", "levels": "A and AA", "verdict": VERDICT, "axe_execution": execution,
            "axe_report_read": runs is not None, "counts": counts, "criteria": entries}


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--axe-execution")
    parser.add_argument("--axe-report")
    parser.add_argument("--suspected", action="append", default=[])
    parser.add_argument("--out", default=OUT)
    args = parser.parse_args(argv)
    writer = _writer()
    try:
        if args.axe_execution is not None and not EXECUTION.fullmatch(args.axe_execution):
            raise writer.Refused("--axe-execution must be an execution id (EXE-...)")
        if os.path.isabs(args.out):
            raise writer.Refused("--out must name a file under .qa/work/frontend/")
        suspected: Dict[str, List[str]] = {}
        for item in args.suspected:
            sc, _, finding = item.partition("=")
            if sc not in WCAG22 or not FINDING.fullmatch(finding):
                raise writer.Refused("--suspected takes CRITERION=FND-NNN with a WCAG 2.2 A or AA criterion, such as 1.1.1=FND-001")
            suspected.setdefault(sc, []).append(finding)
        runs = read_report(args.axe_report, writer.Refused) if args.axe_report else None
        document = table(args.axe_execution, runs, suspected)
        data = (json.dumps(document, indent=1) + "\n").encode("utf-8")
        written = writer.write_new(args.out, data, Path.cwd())
    except (writer.Refused, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, writer.Refused) else type(exc).__name__}, sort_keys=True))
        return 2
    print(json.dumps({"ok": True, "written": written, "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                      "criteria": len(document["criteria"]), "axe_decidable": len(AXE_DECIDABLE),
                      "manual_only": len(WCAG22) - len(AXE_DECIDABLE), "counts": document["counts"],
                      "axe_report_read": document["axe_report_read"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
