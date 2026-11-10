"""Deterministic reports (§6.4 P, card P) and the claim checker.

``render_markdown`` and ``render_json`` are pure functions of the state, so the same state gives the same bytes: neither
file holds the report ID or the time. Every value that comes from the ledger passes through ``md_cell``, except the
closed values of the verdict line, which ``check_markdown`` must be able to read (T17-R2).
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from string import Template
from typing import Iterable, List, Optional, Sequence, Tuple

from .. import config, doctor, ledger, resource_dir, state, trace
from ..canonical import dumps, sha256_bytes
from ..errors import QaError
from ..workspace import Workspace, _not_redirected, write_atomic
from .check import ALIAS, PROJECT_ALIAS, check_markdown
from .sarif import render_sarif

__all__ = ["check", "check_markdown", "md_cell", "render", "render_executive", "render_json", "render_markdown", "render_sarif"]

# QaError codes raised here: invalid_format; plus ledger.append's and config.load_config's.

_FORMATS = ("md", "json", "sarif", "executive")
_DEFAULT_FORMATS = ("md", "json")  # SARIF and the executive one-pager are written only when asked for
_FILE_NAMES = {"md": "report.md", "json": "report.json", "sarif": "report.sarif", "executive": "executive.md"}
_UNRESOLVED = ("open", "reopened", "fix_submitted")  # the lifecycle states the gate counts as unresolved
_SEVERITY_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFORMATIONAL")
EXECUTIVE_ROW_LIMIT = 10  # the "one page" holds this many recommendations; the rest are counted in one generated line
_HEADLINE_START = "**Integrity failure:**"
_STALE_START = "**Recorded verdict, not current:**"
_HEADLINE_SHAPE = re.compile(
    re.escape(_HEADLINE_START) + r" ledger verification found ([0-9]+) problems?"
    r"(?:; the state fold recorded [0-9]+ violations?)?(?:; the latest gate evaluation found test integrity NOT_MET)?; see Integrity\.")
INTEGRITY_ROWS_SHOWN = 20  # a hostile ledger can make verification report any number of problems
_UNNAMED = "unnamed project"
_LINE_BREAKS = "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"  # every boundary str.splitlines honors
_ESCAPES = (("\\", "\\\\"), ("|", "\\|"), ("`", "\\`"), ("*", "\\*"), ("_", "\\_"), ("[", "\\["), ("]", "\\]"),
            ("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))
_LIMITS = (  # §12.10, restated in every report
    "It cannot stop an agent that ignores the skills and runs commands directly. "
    "It can only refuse to count unrecorded results and flag the gap.",
    "It cannot verify that a human really approved a grant or a configuration change. "
    "It records the agent's quote of the approval. The host's own permission prompts remain the authoritative control.",
    "It cannot make pattern-matching a sandbox.",
    "It cannot make a single-agent session truly independent.",
    "It cannot tell that a .qa/ copied in outside git carries approvals nobody here gave.",
)


def md_cell(value: object) -> str:
    """``value`` as text that cannot end a table cell, open markup or start a line of its own."""
    text = "" if value is None else value if isinstance(value, str) else str(value)
    for raw, escaped in _ESCAPES:  # the backslash is first, so no later escape is escaped twice
        text = text.replace(raw, escaped)
    return "".join(" " if ch in _LINE_BREAKS else ch for ch in text)


def _number(name: str) -> Tuple[int, str]:
    return (len(name), name)


def _in_id_order(names: Iterable[str]) -> List[str]:
    return sorted(names, key=_number)


def _latest_gate(st: dict) -> Optional[dict]:
    """The highest GATE number (T17-R3)."""
    ids = _in_id_order(st["gates"])
    return st["gates"][ids[-1]] if ids else None


def _test_integrity(gate: Optional[dict]) -> Optional[dict]:
    if gate is None:
        return None
    return next((c for c in gate["criteria"] if c["type"] == "test_integrity"), None)


def _short_commit(execution: dict) -> Optional[str]:
    git = execution["git"]
    return git["commit"][:7] if git else None


def _counts(execution: dict) -> list:
    report = execution["report"]
    if report is None or report["kind"] != "tests":
        return [None] * 4
    return [report["tests"], report["failures"], report["errors"], report["skipped"]]


def _row(*cells: object) -> str:
    return "| " + " | ".join(md_cell(cell) for cell in cells) + " |"


def _table(header: Iterable[str], rows: List[str]) -> List[str]:
    header = list(header)
    return [_row(*header), "| " + " | ".join("---" for _ in header) + " |"] + rows


def _gate_section(gate: Optional[dict]) -> List[str]:
    if gate is None:
        return ["No gate evaluation recorded."]
    rows = [_row(c["id"], c["outcome"], "blocking" if c["blocking"] else "advisory", c["detail"]) for c in gate["criteria"]]
    lines = _table(("Criterion", "Outcome", "Kind", "Detail"), rows)
    if gate["conditions"]:
        lines += [""] + ["- " + md_cell(condition) for condition in gate["conditions"]]
    return lines


def _execution_section(st: dict) -> List[str]:
    if not st["executions"]:
        return ["No executions recorded."]
    rows = []
    for eid in _in_id_order(st["executions"]):
        e = st["executions"][eid]
        rows.append(_row(eid, e["status"], e["reason"], e["strength"], e["scope"], *_counts(e), _short_commit(e)))
    return _table(("ID", "Status", "Reason", "Strength", "Scope", "Total", "Failures", "Errors", "Skipped", "Commit"), rows)


def _finding_section(st: dict) -> List[str]:
    if not st["findings"]:
        return ["No findings recorded."]
    rows = []
    for fid in _in_id_order(st["findings"]):
        f = st["findings"][fid]
        rows.append(_row(fid, f["title"], f["severity"], f["priority"], f["state"], f["verification_status"]))
    return _table(("ID", "Title", "Severity", "Priority", "State", "Verification"), rows)


def _traceability_target(st: dict, gate: Optional[dict]) -> Optional[str]:
    """The commit the matrix is about: the latest gate's target, else the newest recorded execution's. None when there is none."""
    commits = []
    if gate is not None:
        commits.append((gate.get("target") or {}).get("commit"))
    runs = sorted(st["executions"].values(), key=lambda e: (e["started_at"], e["id"]), reverse=True)
    commits += [(run["git"] or {}).get("commit") for run in runs]
    return next((c for c in commits if isinstance(c, str) and re.fullmatch(r"[0-9a-f]{40}", c)), None)


def _traceability_section(st: dict, gate: Optional[dict]) -> List[str]:
    if not (st["requirements"] or st["testcases"]):
        return ["No requirements or test cases recorded."]
    target = _traceability_target(st, gate)
    if target is None:
        return ["No execution on a recorded commit; the matrix needs one."]
    matrix = trace.matrix(st, target)
    lines = ["Target commit: %s" % target[:12], ""]
    lines += _table(("Gap kind", "Count"), [_row(kind, matrix["counts"][kind]) for kind in trace.GAP_KINDS])
    if matrix["gaps"]:
        lines += [""] + _table(("Record", "Gap"), [_row(g["id"], g["kind"]) for g in matrix["gaps"]])
    return lines


def _problem_rows(problems: Sequence[dict]) -> List[str]:
    rows = [_row(p["kind"], p["where"], p["detail"]) for p in problems[:INTEGRITY_ROWS_SHOWN]]
    lines = ["Ledger verification found %d problem%s." % (len(problems), "" if len(problems) == 1 else "s"), ""]
    lines += _table(("Kind", "Where", "Detail"), rows)
    if len(problems) > INTEGRITY_ROWS_SHOWN:
        lines += ["", "%d more problems are not listed; run `qa ledger verify`." % (len(problems) - INTEGRITY_ROWS_SHOWN)]
    return lines + [""]


def _integrity_section(st: dict, gate: Optional[dict], problems: Sequence[dict] = ()) -> List[str]:
    lines: List[str] = _problem_rows(problems) if problems else []
    if st["violations"]:
        lines += _table(("Event", "Kind"), [_row(v["event"], v["kind"]) for v in sorted(st["violations"], key=lambda v: v["event"])])
    else:
        lines.append("The state fold found no violations.")
    criterion = _test_integrity(gate)
    if criterion is not None:
        lines += ["", _row(criterion["id"], criterion["outcome"], criterion["detail"])]
    return lines


def _limitation_section(st: dict) -> List[str]:
    lines = ["- " + text for text in _LIMITS]
    rows = [_row(eid, text) for eid in _in_id_order(st["executions"])
            for text in st["executions"][eid]["limitations"]]
    return lines + ([""] + _table(("Execution", "Limitation"), rows) if rows else [])


def _headline(st: dict, gate: Optional[dict], problems: Sequence[dict] = ()) -> str:
    causes = []
    if problems:
        causes.append("ledger verification found %d problem%s" % (len(problems), "" if len(problems) == 1 else "s"))
    if st["violations"]:
        causes.append("the state fold recorded %d violation%s" % (len(st["violations"]), "" if len(st["violations"]) == 1 else "s"))
    criterion = _test_integrity(gate)
    if criterion is not None and criterion["outcome"] == "NOT_MET":
        causes.append("the latest gate evaluation found test integrity NOT_MET")
    return _HEADLINE_START + " %s; see Integrity.\n" % "; ".join(causes) if causes else ""


def _verdict_line(gate: Optional[dict], problems: Sequence[dict] = ()) -> str:
    if gate is None:
        return "No gate evaluation recorded; no verdict."
    criterion = _test_integrity(gate)
    if problems and not (criterion is not None and criterion["outcome"] == "NOT_MET"):
        # No integrity failure in the recorded gate, yet verification fails now: the gate was computed before the change.
        # A gate that already found test integrity NOT_MET did see it, so its verdict stands.
        return ("**Recorded verdict, not current:** %s (%s, profile %s, independence %s, state %s). It was computed before the "
                "change that ledger verification found and cannot be relied on; repair the ledger and evaluate the gate again."
                % (gate["verdict"], gate["id"], gate["profile"], gate["independence"], gate["state_hash"]))
    return "**Verdict:** %s (%s, profile %s, independence %s, state %s)" % (
        gate["verdict"], gate["id"], gate["profile"], gate["independence"], gate["state_hash"])


def render_markdown(st: dict, *, project: Optional[str] = None, problems: Sequence[dict] = ()) -> str:
    template = Template((resource_dir("templates") / "report.md.tmpl").read_text(encoding="utf-8"))
    gate = _latest_gate(st)
    text = template.substitute(
        project=md_cell(_UNNAMED if project is None else project), headline=_headline(st, gate, problems), verdict=_verdict_line(gate, problems),
        gate="\n".join(_gate_section(gate)), executions="\n".join(_execution_section(st)),
        findings="\n".join(_finding_section(st)), traceability="\n".join(_traceability_section(st, gate)),
        integrity="\n".join(_integrity_section(st, gate, problems)),
        limitations="\n".join(_limitation_section(st)))
    return text.rstrip("\n") + "\n"


def _executive_rows(st: dict) -> List[dict]:
    """Unresolved findings that need work, most urgent first: priority, then severity, then ID."""
    rows = [dict(f, id=fid) for fid, f in st["findings"].items() if f["state"] in _UNRESOLVED and f["priority"] != "none"]
    return sorted(rows, key=lambda f: (f["priority"], _SEVERITY_ORDER.index(f["severity"]), _number(f["id"])))


def _executive_summary(st: dict, gate: Optional[dict]) -> List[str]:
    open_rows = [dict(f, id=fid) for fid, f in st["findings"].items() if f["state"] in _UNRESOLVED]
    lines = ["- Unresolved findings: %d" % len(open_rows)]
    by_severity = ["%s %d" % (s, sum(1 for f in open_rows if f["severity"] == s)) for s in _SEVERITY_ORDER
                   if any(f["severity"] == s for f in open_rows)]
    if by_severity:
        lines.append("- By severity: " + ", ".join(by_severity))
    lines.append("- Executions recorded: %d" % len(st["executions"]))
    if gate is not None:
        met = sum(1 for c in gate["criteria"] if c["outcome"] == "MET")
        lines.append("- %s: %d of %d criteria MET" % (gate["id"], met, len(gate["criteria"])))
    return lines


def _executive_recommendations(st: dict) -> List[str]:
    rows = _executive_rows(st)
    if not rows:
        return ["No unresolved findings."]
    lines = ["%d. %s" % (n, md_cell("%s %s (%s, %s): %s" % (f["priority"], f["id"], f["severity"], f["category"],
                                                           f["recommendation"] or f["title"])))
             for n, f in enumerate(rows[:EXECUTIVE_ROW_LIMIT], 1)]
    if len(rows) > EXECUTIVE_ROW_LIMIT:
        lines += ["", "%d more unresolved findings are not listed here, lowest priority last; see the full report."
                  % (len(rows) - EXECUTIVE_ROW_LIMIT)]
    return lines


def _executive_follow_up(gate: Optional[dict]) -> List[str]:
    if gate is None:
        return ["No gate evaluation recorded."]
    rows = ["- %s %s: %s (%s)" % (gate["id"], md_cell(c["id"]), c["outcome"], "blocking" if c["blocking"] else "advisory")
            for c in gate["criteria"] if c["outcome"] not in ("MET", "NOT_APPLICABLE")]
    return rows or ["Every criterion is met or not applicable."]


def render_executive(st: dict, *, project: Optional[str] = None, problems: Sequence[dict] = ()) -> str:
    """The one-page summary: verdict, counts, recommendations by priority and what the gate still lacks. A pure function of ``st``."""
    template = Template((resource_dir("templates") / "executive.md.tmpl").read_text(encoding="utf-8"))
    gate = _latest_gate(st)
    text = template.substitute(
        project=md_cell(_UNNAMED if project is None else project), headline=_headline(st, gate, problems), verdict=_verdict_line(gate, problems),
        summary="\n".join(_executive_summary(st, gate)), recommendations="\n".join(_executive_recommendations(st)),
        follow_up="\n".join(_executive_follow_up(gate)))
    return text.rstrip("\n") + "\n"


def render_json(st: dict, *, project: Optional[str] = None, problems: Sequence[dict] = ()) -> dict:
    gate = _latest_gate(st)
    executions = []
    for eid in _in_id_order(st["executions"]):
        e = st["executions"][eid]
        executions.append({"id": eid, "status": e["status"], "reason": e["reason"], "strength": e["strength"], "scope": e["scope"],
                           "report": e["report"], "commit": _short_commit(e), "limitations": list(e["limitations"])})
    findings = []
    for fid in _in_id_order(st["findings"]):
        f = st["findings"][fid]
        findings.append({"id": fid, "title": f["title"], "severity": f["severity"], "priority": f["priority"],
                         "state": f["state"], "verification_status": f["verification_status"]})
    out = {
        "project": _UNNAMED if project is None else project,
        "state_hash": state.state_hash(st),
        "verdict": gate["verdict"] if gate else None,
        "gate": {k: gate[k] for k in ("id", "profile", "independence", "state_hash", "criteria")} if gate else None,
        "conditions": list(gate["conditions"]) if gate else [],
        "executions": executions,
        "findings": findings,
        "integrity": {"violations": [dict(v) for v in st["violations"]], "test_integrity": _test_integrity(gate)},
        "limitations": {"kernel": list(_LIMITS),
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
        raise QaError("invalid_format", "formats is a non-empty subset of md, json, sarif and executive", formats=formats)
    wanted = [f for f in _FORMATS if f in formats]
    with ws.lock():
        st = state.load(ws)
        problems = doctor.ledger_problems(ws)  # the recorded gate says nothing about a ledger altered after it
        project = config.load_config(ws)["project"]["name"]
        rid = ledger.next_human_id(ws, "RPT")
        contents = {"md": lambda: render_markdown(st, project=project, problems=problems).encode("utf-8"),
                    "json": lambda: dumps(render_json(st, project=project, problems=problems)) + b"\n",
                    "sarif": lambda: render_sarif(st),
                    "executive": lambda: render_executive(st, project=project, problems=problems).encode("utf-8")}
        files = {_FILE_NAMES[f]: contents[f]() for f in wanted}
        folder = ws.path("reports", rid)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(str(folder), 0o700)
        for name, data in files.items():
            write_atomic(folder / name, data)
        gate = _latest_gate(st)
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
    gate = _latest_gate(st)
    criterion = _test_integrity(gate)
    if gate is None or (criterion is not None and criterion["outcome"] == "NOT_MET"):
        return False  # the gate saw the problem itself, so the plain verdict line is the renderer's own
    return not lines[3].startswith(_STALE_START) or any(line.startswith("**Verdict:**") for line in lines)


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
