"""Deterministic reports (§6.4 P, card P) and the claim checker.

``render_markdown`` and ``render_json`` are pure functions of the state, so the same state gives the same bytes: neither
file holds the report ID or the time. Every value that comes from the ledger passes through ``md_cell``, except the
closed values of the verdict line, which ``check_markdown`` must be able to read (T17-R2).
"""
from __future__ import annotations

import os
from string import Template
from typing import Iterable, List, Optional, Tuple

from .. import config, ledger, resource_dir, state
from ..canonical import dumps, sha256_bytes
from ..errors import QaError
from ..workspace import Workspace, write_atomic
from .check import check_markdown

__all__ = ["check_markdown", "md_cell", "render", "render_json", "render_markdown"]

# QaError codes raised here: invalid_format; plus ledger.append's and config.load_config's.

_FORMATS = ("md", "json")
_FILE_NAMES = {"md": "report.md", "json": "report.json"}
_UNNAMED = "unnamed project"
_LINE_BREAKS = "\r\n\v\f\x1c\x1d\x1e\x85  "  # every boundary str.splitlines honors
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


def _integrity_section(st: dict, gate: Optional[dict]) -> List[str]:
    lines: List[str] = []
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


def _headline(st: dict, gate: Optional[dict]) -> str:
    causes = []
    if st["violations"]:
        causes.append("the state fold recorded %d violation%s" % (len(st["violations"]), "" if len(st["violations"]) == 1 else "s"))
    criterion = _test_integrity(gate)
    if criterion is not None and criterion["outcome"] == "NOT_MET":
        causes.append("the latest gate evaluation found test integrity NOT_MET")
    return "**Integrity failure:** %s; see Integrity.\n" % "; ".join(causes) if causes else ""


def _verdict_line(gate: Optional[dict]) -> str:
    if gate is None:
        return "No gate evaluation recorded; no verdict."
    return "**Verdict:** %s (%s, profile %s, independence %s, state %s)" % (
        gate["verdict"], gate["id"], gate["profile"], gate["independence"], gate["state_hash"])


def render_markdown(st: dict, *, project: Optional[str] = None) -> str:
    template = Template((resource_dir("templates") / "report.md.tmpl").read_text(encoding="utf-8"))
    gate = _latest_gate(st)
    text = template.substitute(
        project=md_cell(_UNNAMED if project is None else project), headline=_headline(st, gate), verdict=_verdict_line(gate),
        gate="\n".join(_gate_section(gate)), executions="\n".join(_execution_section(st)),
        findings="\n".join(_finding_section(st)), integrity="\n".join(_integrity_section(st, gate)),
        limitations="\n".join(_limitation_section(st)))
    return text.rstrip("\n") + "\n"


def render_json(st: dict, *, project: Optional[str] = None) -> dict:
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
    return {
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


def render(ws: Workspace, actor: ledger.Actor, *, formats: Tuple[str, ...] = _FORMATS) -> dict:
    """Write the report files for the state as it is now, record ``report.rendered`` and return that event."""
    if (isinstance(formats, str) or not isinstance(formats, (tuple, list)) or not formats
            or any(f not in _FORMATS for f in formats)):
        raise QaError("invalid_format", "formats is a non-empty subset of md and json", formats=formats)
    wanted = [f for f in _FORMATS if f in formats]
    with ws.lock():
        st = state.load(ws)
        project = config.load_config(ws)["project"]["name"]
        rid = ledger.next_human_id(ws, "RPT")
        contents = {"md": lambda: render_markdown(st, project=project).encode("utf-8"),
                    "json": lambda: dumps(render_json(st, project=project)) + b"\n"}
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
