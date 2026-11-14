"""The self-contained HTML report: one page, one inline style element allowed by its hash, nothing that runs or loads.

``render_html`` is a pure function of the state, like ``render_markdown``: no clock and no report ID, so the same state
gives the same bytes. Each section builder returns rows of plain cells; ``_table`` writes them, and every value from the
ledger or the config reaches the page through ``html_text`` as element text. No value is ever placed in an attribute, a
URL, the style element or a script, and the page renders no link, so a value shaped like markup or a URL stays text.

The content security policy sits in a meta element before the style element. It names the SHA-256 of that element's text,
computed here from the template, so the template's style is the only one a browser applies; the policy's other
directives refuse every fetch, base URL and form submission.
"""
from __future__ import annotations

import base64
import hashlib
import html
import re
import shlex
from string import Template
from typing import List, Optional, Sequence, Tuple

from .. import resource_dir, trace
from . import (EXECUTIVE_ROW_LIMIT, INTEGRITY_ROWS_SHOWN, _LIMITS, _SEVERITY_ORDER, _UNNAMED, _UNRESOLVED, _counts,
               _executive_rows, _headline, _in_id_order, _latest_gate, _short_commit, _test_integrity, _traceability_target,
               _verdict_line)

__all__ = ["html_text", "render_html"]

# C0 controls but tab and newline, DEL, the bidi embeddings and overrides, the bidi isolates, and the tag characters.
_MARKED = re.compile("[\x00-\x08\x0b-\x1f\x7f\u202a-\u202e\u2066-\u2069\U000e0000-\U000e007f]")
_STYLE = re.compile(r"<style>(.*?)</style>", re.S)
_LABEL = re.compile(r"\*\*([^*]+)\*\* (.*)", re.S)  # the bold label the Markdown headline and verdict line open with


def html_text(value: object) -> str:
    """``value`` as element text: HTML-escaped, with each control, bidi or tag character shown as ``[U+XXXX]``."""
    return _MARKED.sub(lambda found: "[U+%04X]" % ord(found.group(0)), html.escape(str(value), quote=True))


def _cell(value: object) -> str:
    """One table cell's content: nothing for None, true or false for a boolean, one line per item of a list."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "<br>".join(_cell(item) for item in value)
    return html_text(value)


def _table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    head = "".join("<th>%s</th>" % html_text(name) for name in header)
    body = "".join("<tr>%s</tr>\n" % "".join("<td>%s</td>" % _cell(value) for value in row) for row in rows)
    return "<table>\n<thead><tr>%s</tr></thead>\n<tbody>\n%s</tbody>\n</table>" % (head, body)


def _p(text: object) -> str:
    return "<p>%s</p>" % html_text(text)


def _ul(items: Sequence[object]) -> str:
    return "<ul>\n%s</ul>" % "".join("<li>%s</li>\n" % html_text(item) for item in items)


def _h(level: int, text: object) -> str:
    return "<h%d>%s</h%d>" % (level, html_text(text), level)


def _labelled(line: str) -> str:
    """A Markdown headline or verdict line as a paragraph, its bold label in ``<strong>``: the same words in every format."""
    line = line.rstrip("\n")
    found = _LABEL.fullmatch(line)
    if found is None:
        return _p(line)
    return "<p><strong>%s</strong> %s</p>" % (html_text(found.group(1)), html_text(found.group(2)))


def _command(argv: Sequence[str], shell: bool = False) -> str:
    """The recorded command as one line: a shell run's own line as given, else the argument vector shell-quoted, so where
    each argument starts and ends stays visible."""
    words = [str(arg) for arg in argv]
    return " ".join(words) if shell else shlex.join(words)


# --------------------------------------------------------------------------- section builders: rows of plain cells

def _summary_rows(st: dict, gate: Optional[dict]) -> List[Tuple[str, object]]:
    unresolved = [f for f in st["findings"].values() if f["state"] in _UNRESOLVED]
    rows: List[Tuple[str, object]] = [("Unresolved findings", len(unresolved))]
    by_severity = ["%s %d" % (s, sum(1 for f in unresolved if f["severity"] == s)) for s in _SEVERITY_ORDER
                   if any(f["severity"] == s for f in unresolved)]
    if by_severity:
        rows.append(("By severity", ", ".join(by_severity)))
    rows.append(("Executions recorded", len(st["executions"])))
    if gate is not None:
        met = sum(1 for c in gate["criteria"] if c["outcome"] == "MET")
        rows.append(("%s criteria MET" % gate["id"], "%d of %d" % (met, len(gate["criteria"]))))
    return rows


def _recommendation_rows(ranked: List[dict]) -> List[list]:
    return [[n, f["priority"], f["id"], f["severity"], f["category"], f["recommendation"] or f["title"]]
            for n, f in enumerate(ranked[:EXECUTIVE_ROW_LIMIT], 1)]


def _follow_up_rows(gate: dict) -> List[list]:
    return [[gate["id"], c["id"], c["outcome"], "blocking" if c["blocking"] else "advisory"]
            for c in gate["criteria"] if c["outcome"] not in ("MET", "NOT_APPLICABLE")]


def _gate_rows(gate: dict) -> List[list]:
    return [[c["id"], c["outcome"], "blocking" if c["blocking"] else "advisory", c["detail"]] for c in gate["criteria"]]


def _execution_rows(st: dict) -> List[list]:
    rows = []
    for eid in _in_id_order(st["executions"]):
        e = st["executions"][eid]
        manifest = e["evidence_manifest"]
        rows.append([eid, e["status"], e["reason"], e["strength"], e["scope"], *_counts(e), _short_commit(e),
                     _command(e["command"]["argv"], e["command"]["shell"]), list(manifest["files"]) if manifest else None])
    return rows


def _finding_rows(st: dict) -> List[list]:
    rows = []
    for fid in _in_id_order(st["findings"]):
        f = st["findings"][fid]
        rows.append([fid, f["title"], f["severity"], f["priority"], f["state"], f["verification_status"], list(f["evidence"])])
    return rows


def _problem_rows(problems: Sequence[dict]) -> List[list]:
    return [[p["kind"], p["where"], p["detail"]] for p in problems[:INTEGRITY_ROWS_SHOWN]]


def _differences(differences: dict) -> List[str]:
    shown = {True: "true", False: "false", None: "null"}
    return ["%s: %s" % (name, shown[value] if value is None or isinstance(value, bool) else value)
            for name, value in sorted(differences.items())]


def _comparison_rows(record: dict) -> List[list]:
    return [[c["local"], c["remote"], c["commit"], c["matching"], _differences(c["differences"])] for c in record["comparisons"]]


def _bundle_facts(record: dict) -> List[list]:
    source = record["source"]
    return [["SHA-256", record["sha256"]], ["Manifest SHA-256", record["manifest_sha256"]],
            ["Source kernel version", source["kernel_version"]], ["Source state hash", source["state_hash"]],
            ["Files", record["files"]], ["Imported executions", len(record["executions"])],
            ["Source ledger violations", record["violations"]], ["Authorizations ignored", record["ignored"]["authorizations"]]]


def _imported_rows(record: dict) -> List[list]:
    return [[eid, e["status"], e["reason"], e["strength"], e["exit_code"], e["activity"], e["scope"], e["commit"],
             _command(e["argv"])] for eid, e in sorted(record["executions"].items())]


def _limitation_rows(st: dict) -> List[list]:
    return [[eid, text] for eid in _in_id_order(st["executions"]) for text in st["executions"][eid]["limitations"]]


# --------------------------------------------------------------------------- sections

def _summary(st: dict, gate: Optional[dict], problems: Sequence[dict]) -> str:
    parts = []
    headline = _headline(st, gate, problems)
    if headline:
        parts.append(_labelled(headline))
    parts.append(_labelled(_verdict_line(gate, problems)))
    parts.append(_table(("Measure", "Value"), _summary_rows(st, gate)))
    parts.append(_h(3, "Recommendations"))
    ranked = _executive_rows(st)
    if ranked:
        parts.append(_table(("#", "Priority", "Finding", "Severity", "Category", "Recommendation"), _recommendation_rows(ranked)))
        if len(ranked) > EXECUTIVE_ROW_LIMIT:
            parts.append(_p("%d more unresolved findings are not listed here, lowest priority last; see Findings."
                            % (len(ranked) - EXECUTIVE_ROW_LIMIT)))
    else:
        parts.append(_p("No unresolved findings."))
    parts.append(_h(3, "Gate follow-up"))
    if gate is None:
        parts.append(_p("No gate evaluation recorded."))
    else:
        follow = _follow_up_rows(gate)
        parts.append(_table(("Gate", "Criterion", "Outcome", "Kind"), follow) if follow
                     else _p("Every criterion is met or not applicable."))
    return "\n".join(parts)


def _gate(gate: Optional[dict]) -> str:
    if gate is None:
        return _p("No gate evaluation recorded.")
    parts = [_table(("Criterion", "Outcome", "Kind", "Detail"), _gate_rows(gate))]
    if gate["conditions"]:
        parts.append(_ul(gate["conditions"]))
    return "\n".join(parts)


def _executions(st: dict) -> str:
    if not st["executions"]:
        return _p("No executions recorded.")
    return _table(("ID", "Status", "Reason", "Strength", "Scope", "Total", "Failures", "Errors", "Skipped", "Commit", "Command",
                   "Evidence"), _execution_rows(st))


def _findings(st: dict) -> str:
    if not st["findings"]:
        return _p("No findings recorded.")
    return _table(("ID", "Title", "Severity", "Priority", "State", "Verification", "Evidence"), _finding_rows(st))


def _traceability(st: dict, gate: Optional[dict]) -> str:
    if not (st["requirements"] or st["testcases"]):
        return _p("No requirements or test cases recorded.")
    target, source = _traceability_target(st, gate)
    if target is None:
        return _p("No execution on a recorded commit; the matrix needs one.")
    matrix = trace.matrix(st, target)
    parts = [_p("Target commit: %s (%s)" % (target[:12], source)),
             _table(("Gap kind", "Count"), [[kind, matrix["counts"][kind]] for kind in trace.GAP_KINDS])]
    if matrix["gaps"]:
        parts.append(_table(("Record", "Gap"), [[g["id"], g["kind"]] for g in matrix["gaps"]]))
    return "\n".join(parts)


def _integrity(st: dict, gate: Optional[dict], problems: Sequence[dict]) -> str:
    parts = []
    if problems:
        parts.append(_p("Ledger verification found %d problem%s." % (len(problems), "" if len(problems) == 1 else "s")))
        parts.append(_table(("Kind", "Where", "Detail"), _problem_rows(problems)))
        if len(problems) > INTEGRITY_ROWS_SHOWN:
            parts.append(_p("%d more problems are not listed; run qa ledger verify." % (len(problems) - INTEGRITY_ROWS_SHOWN)))
    if st["violations"]:
        parts.append(_table(("Event", "Kind"), [[v["event"], v["kind"]] for v in sorted(st["violations"], key=lambda v: v["event"])]))
    else:
        parts.append(_p("The state fold found no violations."))
    criterion = _test_integrity(gate)
    if criterion is not None:
        parts.append(_table(("Criterion", "Outcome", "Detail"), [[criterion["id"], criterion["outcome"], criterion["detail"]]]))
    return "\n".join(parts)


def _reexecution(st: dict) -> str:
    if not st["bundles"]:
        return _p("No evidence bundles imported.")
    parts = []
    for bid in _in_id_order(st["bundles"]):
        record = st["bundles"][bid]
        parts += [_h(3, bid), _table(("Field", "Value"), _bundle_facts(record)), _h(4, "Comparisons")]
        comparisons = _comparison_rows(record)
        parts.append(_table(("Local", "Remote", "Commit", "Matching", "Differences"), comparisons) if comparisons
                     else _p("No local execution ran the same command at the same commit."))
        parts.append(_h(4, "Imported executions"))
        imported = _imported_rows(record)
        parts.append(_table(("ID", "Status", "Reason", "Strength", "Exit code", "Activity", "Scope", "Commit", "Command"), imported)
                     if imported else _p("The bundle holds no executions."))
    return "\n".join(parts)


def _limitations(st: dict) -> str:
    parts = [_ul(_LIMITS)]
    rows = _limitation_rows(st)
    if rows:
        parts.append(_table(("Execution", "Limitation"), rows))
    return "\n".join(parts)


def _style_hash(source: str) -> str:
    """The CSP source for the template's one style element: base64 of the SHA-256 of its exact text."""
    styles = _STYLE.findall(source)
    if len(styles) != 1 or "$" in styles[0]:  # a "$" would change in substitution, and the hash would name other text
        raise ValueError("the HTML report template must hold exactly one style element, with no $ in it")
    return base64.b64encode(hashlib.sha256(styles[0].encode("utf-8")).digest()).decode("ascii")


def render_html(st: dict, *, project: Optional[str] = None, problems: Sequence[dict] = ()) -> str:
    source = (resource_dir("templates") / "report.html.tmpl").read_text(encoding="utf-8")
    gate = _latest_gate(st)
    text = Template(source).substitute(
        style_hash=_style_hash(source), project=html_text(_UNNAMED if project is None else project),
        summary=_summary(st, gate, problems), gate=_gate(gate), executions=_executions(st), findings=_findings(st),
        traceability=_traceability(st, gate), integrity=_integrity(st, gate, problems), reexecution=_reexecution(st),
        limitations=_limitations(st))
    return text.rstrip("\n") + "\n"
