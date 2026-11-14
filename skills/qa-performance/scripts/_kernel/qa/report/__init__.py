"""Deterministic reports (§6.4 P, card P) and the claim checker.

``render_markdown`` and ``render_json`` are pure functions of the state, so the same state gives the same bytes: neither
file holds the report ID or the time. Every value that comes from the ledger passes through ``md_cell``, except the
closed values of the verdict line, which ``check_markdown`` must be able to read (T17-R2). What each section says comes
from ``_rows``; this module formats those blocks as Markdown, and ``html.render_html`` formats the same blocks as one
self-contained page.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from string import Template
from typing import Iterable, List, Optional, Sequence, Tuple

from .. import config, doctor, ledger, resource_dir, state
from ..canonical import dumps, sha256_bytes
from ..errors import QaError
from ..workspace import Workspace, _not_redirected, write_atomic
from . import _rows
from ._rows import EXECUTIVE_ROW_LIMIT, INTEGRITY_ROWS_SHOWN  # both stay importable from qa.report
from .check import ALIAS, PROJECT_ALIAS, check_markdown
from .html import render_html
from .sarif import render_sarif

__all__ = ["check", "check_markdown", "md_cell", "render", "render_executive", "render_json", "render_markdown", "render_sarif"]

# QaError codes raised here: invalid_format; plus ledger.append's and config.load_config's.

_FORMATS = ("md", "json", "sarif", "executive", "html")
_DEFAULT_FORMATS = ("md", "json")  # SARIF, the executive one-pager and the HTML page are written only when asked for
_FILE_NAMES = {"md": "report.md", "json": "report.json", "sarif": "report.sarif", "executive": "executive.md",
               "html": "report.html"}
_HEADLINE_START = "**%s**" % _rows.HEADLINE_LABEL
_STALE_START = "**%s**" % _rows.STALE_LABEL
_VERDICT_START = "**%s**" % _rows.VERDICT_LABEL
_HEADLINE_SHAPE = re.compile(
    re.escape(_HEADLINE_START) + r" ledger verification found ([0-9]+) problems?"
    r"(?:; the state fold recorded [0-9]+ violations?)?(?:; the latest gate evaluation found test integrity NOT_MET)?; see Integrity\.")
_LINE_BREAKS = "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"  # every boundary str.splitlines honors
_ESCAPES = (("\\", "\\\\"), ("|", "\\|"), ("`", "\\`"), ("*", "\\*"), ("_", "\\_"), ("[", "\\["), ("]", "\\]"),
            ("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))


def md_cell(value: object) -> str:
    """``value`` as text that cannot end a table cell, open markup or start a line of its own."""
    text = "" if value is None else value if isinstance(value, str) else str(value)
    for raw, escaped in _ESCAPES:  # the backslash is first, so no later escape is escaped twice
        text = text.replace(raw, escaped)
    return "".join(" " if ch in _LINE_BREAKS else ch for ch in text)


def _row(*cells: object) -> str:
    return "| " + " | ".join(md_cell(cell) for cell in cells) + " |"


def _table(header: Iterable[str], rows: List[str]) -> List[str]:
    header = list(header)
    return [_row(*header), "| " + " | ".join("---" for _ in header) + " |"] + rows


def _code(text: str) -> str:
    return "`%s`" % text


def _labelled(block: _rows.Labelled) -> str:
    return "**%s** %s" % (block.label, block.text)


def _block_lines(block: _rows.Block) -> List[str]:
    """One block as Markdown lines. A sentence and a labelled line are the kernel's own words with closed values, kept as
    they are; every value in a table, a line, a list or a heading passes through ``md_cell``."""
    if isinstance(block, str):
        return [block]
    if isinstance(block, _rows.Labelled):
        return [_labelled(block)]
    if isinstance(block, _rows.Table):
        return _table(block.header, [_row(*cells) for cells in block.rows])
    if isinstance(block, _rows.Line):
        return [_row(*block.cells)]
    if isinstance(block, _rows.Bullets):
        return ["- " + md_cell(item) for item in block.items]
    if isinstance(block, _rows.Heading):
        return ["#" * block.level + " " + md_cell(block.text)]
    raise TypeError("not a report block: %r" % (block,))


def _section(blocks: Sequence[_rows.Block]) -> str:
    """The blocks as Markdown, with a blank line between each two."""
    lines: List[str] = []
    for number, block in enumerate(blocks):
        if number:
            lines.append("")
        lines += _block_lines(block)
    return "\n".join(lines)


def _headline(st: dict, gate: Optional[dict], problems: Sequence[dict] = ()) -> str:
    found = _rows.headline(st, gate, problems)
    return _labelled(found) + "\n" if found is not None else ""


def _verdict_line(gate: Optional[dict], problems: Sequence[dict] = ()) -> str:
    return _block_lines(_rows.verdict(gate, problems))[0]


def render_markdown(st: dict, *, project: Optional[str] = None, problems: Sequence[dict] = ()) -> str:
    template = Template((resource_dir("templates") / "report.md.tmpl").read_text(encoding="utf-8"))
    gate = _rows.latest_gate(st)
    text = template.substitute(
        project=md_cell(_rows.UNNAMED if project is None else project), headline=_headline(st, gate, problems),
        verdict=_verdict_line(gate, problems), gate=_section(_rows.gate_section(gate)),
        executions=_section(_rows.execution_section(st)), findings=_section(_rows.finding_section(st)),
        traceability=_section(_rows.traceability_section(st, gate)),
        integrity=_section(_rows.integrity_section(st, gate, problems, code=_code)),
        limitations=_section(_rows.limitation_section(st)))
    return text.rstrip("\n") + "\n"


def _executive_summary(st: dict, gate: Optional[dict]) -> List[str]:
    return ["- %s: %s" % (measure, value) for measure, value in _rows.executive_summary(st, gate).rows]


def _executive_recommendations(st: dict) -> List[str]:
    found, more = _rows.recommendations(st)
    if isinstance(found, str):
        return [found]
    lines = ["%d. %s" % (n, md_cell("%s %s (%s, %s): %s" % (priority, fid, severity, category, text)))
             for n, priority, fid, severity, category, text in found.rows]
    if more:
        lines += ["", _rows.more_findings(more, "the full report")]
    return lines


def _executive_follow_up(gate: Optional[dict]) -> List[str]:
    found = _rows.follow_up(gate)
    if isinstance(found, str):
        return [found]
    return ["- %s %s: %s (%s)" % (gid, md_cell(cid), outcome, kind) for gid, cid, outcome, kind in found.rows]


def render_executive(st: dict, *, project: Optional[str] = None, problems: Sequence[dict] = ()) -> str:
    """The one-page summary: verdict, counts, recommendations by priority and what the gate still lacks. A pure function of ``st``."""
    template = Template((resource_dir("templates") / "executive.md.tmpl").read_text(encoding="utf-8"))
    gate = _rows.latest_gate(st)
    text = template.substitute(
        project=md_cell(_rows.UNNAMED if project is None else project), headline=_headline(st, gate, problems), verdict=_verdict_line(gate, problems),
        summary="\n".join(_executive_summary(st, gate)), recommendations="\n".join(_executive_recommendations(st)),
        follow_up="\n".join(_executive_follow_up(gate)))
    return text.rstrip("\n") + "\n"


def render_json(st: dict, *, project: Optional[str] = None, problems: Sequence[dict] = ()) -> dict:
    gate = _rows.latest_gate(st)
    executions = []
    for eid in _rows.in_id_order(st["executions"]):
        e = st["executions"][eid]
        executions.append({"id": eid, "status": e["status"], "reason": e["reason"], "strength": e["strength"], "scope": e["scope"],
                           "report": e["report"], "commit": _rows.short_commit(e), "limitations": list(e["limitations"])})
    findings = []
    for fid in _rows.in_id_order(st["findings"]):
        f = st["findings"][fid]
        findings.append({"id": fid, "title": f["title"], "severity": f["severity"], "priority": f["priority"],
                         "state": f["state"], "verification_status": f["verification_status"]})
    out = {
        "project": _rows.UNNAMED if project is None else project,
        "state_hash": state.state_hash(st),
        "verdict": gate["verdict"] if gate else None,
        "gate": {k: gate[k] for k in ("id", "profile", "independence", "state_hash", "criteria")} if gate else None,
        "conditions": list(gate["conditions"]) if gate else [],
        "executions": executions,
        "findings": findings,
        "integrity": {"violations": [dict(v) for v in st["violations"]], "test_integrity": _rows.integrity_criterion(gate)},
        "limitations": {"kernel": list(_rows.LIMITS),
                        "executions": [{"id": e["id"], "text": text} for e in executions for text in e["limitations"]]},
    }
    if problems:  # only a ledger that fails verification adds these, so a clean state keeps its bytes
        out["recorded_verdict"], out["verdict"] = out["verdict"], None  # the old verdict is not the project's now
        out["verdict_current"] = False
        out["integrity"]["ledger_problems"] = [dict(p) for p in problems[:INTEGRITY_ROWS_SHOWN]]
        out["integrity"]["ledger_problem_count"] = len(problems)
    return out


def render(ws: Workspace, actor: ledger.Actor, *, formats: Tuple[str, ...] = _DEFAULT_FORMATS) -> dict:
    """Write the report files for the state as it is now, record ``report.rendered`` and return that event."""
    if (isinstance(formats, str) or not isinstance(formats, (tuple, list)) or not formats
            or any(f not in _FORMATS for f in formats)):
        raise QaError("invalid_format", "formats is a non-empty subset of md, json, sarif, executive and html", formats=formats)
    wanted = [f for f in _FORMATS if f in formats]
    with ws.lock():
        st = state.load(ws)
        problems = doctor.ledger_problems(ws)  # the recorded gate says nothing about a ledger altered after it
        project = config.load_config(ws)["project"]["name"]
        rid = ledger.next_human_id(ws, "RPT")
        contents = {"md": lambda: render_markdown(st, project=project, problems=problems).encode("utf-8"),
                    "json": lambda: dumps(render_json(st, project=project, problems=problems)) + b"\n",
                    "sarif": lambda: render_sarif(st),
                    "executive": lambda: render_executive(st, project=project, problems=problems).encode("utf-8"),
                    "html": lambda: render_html(st, project=project, problems=problems).encode("utf-8")}
        files = {_FILE_NAMES[f]: contents[f]() for f in wanted}
        folder = ws.path("reports", rid)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(str(folder), 0o700)
        for name, data in files.items():
            write_atomic(folder / name, data)
        gate = _rows.latest_gate(st)
        return ledger.append(ws, actor, "report.rendered", {
            "id": rid, "state_hash": state.state_hash(st), "gate": gate["id"] if gate else None,
            "files": {name: sha256_bytes(data) for name, data in files.items()}})


def _generated_headline(line: str, count: int) -> bool:
    """Whether ``line`` is the headline ``_headline`` writes for ``count`` ledger problems, with nothing else in it."""
    return _HEADLINE_SHAPE.fullmatch(line) is not None and int(_HEADLINE_SHAPE.fullmatch(line).group(1)) == count


def _ledger_flaw(st: dict, lines: List[str], count: int) -> bool:
    """True when a report checked while verification fails does not carry what the renderer writes for that case."""
    if len(lines) < 4 or not _generated_headline(lines[2], count):  # the title, a blank line, then the headline
        return True
    gate = _rows.latest_gate(st)
    criterion = _rows.integrity_criterion(gate)
    if gate is None or (criterion is not None and criterion["outcome"] == "NOT_MET"):
        return False  # the gate saw the problem itself, so the plain verdict line is the renderer's own
    return not lines[3].startswith(_STALE_START) or any(line.startswith(_VERDICT_START) for line in lines)


def _open_extra(path: str) -> Tuple[Optional[Workspace], Optional[ledger.Problem]]:
    """A read-only view of another ledger: ``path`` is a ``.qa`` directory, its ``ledger`` directory, or a project root that
    holds a ``.qa``. A symlink at any of those, or a ``qa_dir`` entry that is not a real directory, is refused."""
    shown = str(path)
    try:
        here = Path(os.path.abspath(shown))
        if not stat.S_ISDIR(os.lstat(str(here)).st_mode):
            return None, ledger.Problem("extra_ledger_invalid", shown, "not a real directory (a symlink is not followed)")
        if os.path.lexists(str(here / "ledger")):
            qa_dir = here
        elif here.name == "ledger":
            qa_dir = here.parent
        elif os.path.lexists(str(here / ".qa")):
            qa_dir = here / ".qa"
        else:
            qa_dir = here
        extra = _not_redirected(Workspace(root=qa_dir.parent, qa_dir=qa_dir))
        if not stat.S_ISDIR(os.lstat(str(extra.path("ledger"))).st_mode):
            return None, ledger.Problem("extra_ledger_invalid", shown, "it holds no ledger directory")
        return extra, None
    except (OSError, QaError) as exc:
        return None, ledger.Problem("extra_ledger_invalid", shown, "it cannot be opened as a ledger (%s)" % type(exc).__name__)


def _split_alias(given: str, taken: Sequence[Optional[str]]) -> Tuple[Optional[str], str, Optional[ledger.Problem]]:
    """(alias, path, problem) of one ``--ledger`` value. ``ALIAS=PATH`` when the text before the first ``=`` has the shape of
    an alias; any other value is a path (write ``./a=b`` for a path whose first part holds ``=``)."""
    head, sep, tail = given.partition("=")
    if not sep or "/" in head or os.sep in head:
        return None, given, None
    if not ALIAS.fullmatch(head) or head == PROJECT_ALIAS:
        return None, given, ledger.Problem("extra_ledger_invalid", given, "%r is not an alias: lower-case letters, digits and "
                                           "hyphens, starting with a letter, at most 32, and not %r" % (head, PROJECT_ALIAS))
    if head in taken:
        return None, given, ledger.Problem("extra_ledger_invalid", given, "the alias %r is given twice" % head)
    return head, tail, None


def _load_extra(path: str) -> Tuple[Optional[dict], List[ledger.Problem]]:
    """The state of the ledger at ``path`` when it passes the verification ``qa ledger verify`` runs, otherwise the problems."""
    extra, refused = _open_extra(path)
    if extra is None:
        return None, [refused]
    found = doctor.ledger_problems(extra)
    if found:
        first = found[0]
        return None, [ledger.Problem("extra_ledger_invalid", str(path), "ledger verification found %d problem%s, the first a %s at %s"
                                     % (len(found), "" if len(found) == 1 else "s", first["kind"], first["where"]))]
    try:
        return state.load(extra), []
    except Exception as exc:  # untrusted content that cannot be folded is a failed check, not a crash
        return None, [ledger.Problem("extra_ledger_invalid", str(path), "the ledger could not be folded (%s)" % type(exc).__name__)]


def check(ws: Workspace, text: str, ledgers: Sequence[str] = ()) -> list:
    """``check_markdown`` plus the ledger: while verification fails, a report must carry the headline and the verdict line the
    renderer writes for exactly the problems found now; anything else was rendered earlier or says something of its own.

    ``ledgers`` names other ledgers whose records the report may cite, each as ``PATH`` or ``ALIAS=PATH``; the report cites
    an aliased ledger's IDs as ``ALIAS:ID``. Each is opened read-only and must verify; one that does not, or a bad or repeated
    alias, is a problem of its own and its IDs stay unknown."""
    st = state.load(ws)
    extras: List[dict] = []
    aliases: List[Optional[str]] = []
    refused: List[ledger.Problem] = []
    for given in ledgers:
        alias, path, problem = _split_alias(str(given), aliases)
        if problem is not None:
            refused.append(problem)
            continue
        loaded, problems = _load_extra(path)
        refused += problems
        if loaded is not None:
            extras.append(loaded)
            aliases.append(alias)
    problems = refused + check_markdown(text, st, extras, aliases)
    found = doctor.ledger_problems(ws)
    if found and _ledger_flaw(st, text.splitlines(), len(found)):
        problems.append(ledger.Problem("ledger_integrity", "ledger", "ledger verification found %d problem%s; the report does not "
                                       "carry the matching integrity headline and verdict line; run `qa ledger verify`"
                                       % (len(found), "" if len(found) == 1 else "s")))
    return problems
