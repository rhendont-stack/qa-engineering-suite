"""What each report section says, once, for every renderer.

A section builder returns blocks of raw values: a sentence (``str``), a ``Labelled`` sentence that opens with a bold
label, a ``Table`` of rows, a ``Line`` (one row with its column names), ``Bullets`` or a ``Heading``. Every selection,
filter, sort key, count, column header and empty-state sentence of a report lives here. The Markdown renderer in
``__init__`` and the HTML renderer in ``html`` only format these blocks and escape their values for their own syntax, so
a change to what a report counts or lists reaches every format at once.
"""
from __future__ import annotations

import shlex
from typing import Callable, Iterable, List, NamedTuple, Optional, Sequence, Tuple, Union

from .. import trace

EXECUTIVE_ROW_LIMIT = 10  # the "one page" holds this many recommendations; the rest are counted in one generated line
INTEGRITY_ROWS_SHOWN = 20  # a hostile ledger can make verification report any number of problems
UNNAMED = "unnamed project"
UNRESOLVED = ("open", "reopened", "fix_submitted")  # the lifecycle states the gate counts as unresolved
SEVERITY_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFORMATIONAL")
LIMITS = (  # §12.10, restated in every report
    "It cannot stop an agent that ignores the skills and runs commands directly. "
    "It can only refuse to count unrecorded results and flag the gap.",
    "It cannot verify that a human really approved a grant or a configuration change. "
    "It records the agent's quote of the approval. The host's own permission prompts remain the authoritative control.",
    "It cannot make pattern-matching a sandbox.",
    "It cannot make a single-agent session truly independent.",
    "It cannot tell that a .qa/ copied in outside git carries approvals nobody here gave.",
)
HEADLINE_LABEL = "Integrity failure:"
STALE_LABEL = "Recorded verdict, not current:"
VERDICT_LABEL = "Verdict:"
NO_GATE = "No gate evaluation recorded."
NO_UNRESOLVED = "No unresolved findings."
ALL_MET = "Every criterion is met or not applicable."
LEDGER_VERIFY = "qa ledger verify"


class Table(NamedTuple):
    header: Tuple[str, ...]
    rows: List[list]


class Line(NamedTuple):
    """One row with its column names. Markdown writes it as a bare table row; HTML as a one-row table."""
    header: Tuple[str, ...]
    cells: list


class Bullets(NamedTuple):
    items: List[object]


class Labelled(NamedTuple):
    label: str
    text: str


class Heading(NamedTuple):
    level: int
    text: str


Block = Union[str, Labelled, Table, Line, Bullets, Heading]


# --------------------------------------------------------------------------- record helpers

def _number(name: str) -> Tuple[int, str]:
    return (len(name), name)


def in_id_order(names: Iterable[str]) -> List[str]:
    return sorted(names, key=_number)


def latest_gate(st: dict) -> Optional[dict]:
    """The highest GATE number (T17-R3)."""
    ids = in_id_order(st["gates"])
    return st["gates"][ids[-1]] if ids else None


def integrity_criterion(gate: Optional[dict]) -> Optional[dict]:
    if gate is None:
        return None
    return next((c for c in gate["criteria"] if c["type"] == "test_integrity"), None)


def short_commit(execution: dict) -> Optional[str]:
    git = execution["git"]
    return git["commit"][:7] if git else None


def counts(execution: dict) -> list:
    report = execution["report"]
    if report is None or report["kind"] != "tests":
        return [None] * 4
    return [report["tests"], report["failures"], report["errors"], report["skipped"]]


def criterion_kind(criterion: dict) -> str:
    return "blocking" if criterion["blocking"] else "advisory"


def command_line(argv: Sequence[str], shell: bool = False) -> str:
    """The recorded command as one line: a shell run's own line as given, else the argument vector shell-quoted, so where
    each argument starts and ends stays visible."""
    words = [str(arg) for arg in argv]
    return " ".join(words) if shell else shlex.join(words)


def traceability_target(st: dict, gate: Optional[dict]) -> Tuple[Optional[str], str]:
    """The commit the matrix is about and where it came from: the latest gate's target, else the newest execution's."""
    if gate is not None:
        commit = (gate.get("target") or {}).get("commit")
        if isinstance(commit, str) and trace._FULL_COMMIT.match(commit):
            return commit, "the target of %s" % gate["id"]
    runs = sorted(st["executions"].values(), key=lambda e: (e["started_at"], e["id"]), reverse=True)
    commit = next((c for c in ((run["git"] or {}).get("commit") for run in runs) if isinstance(c, str) and trace._FULL_COMMIT.match(c)), None)
    return commit, "newest execution; %s" % ("no gate recorded" if gate is None else "%s recorded no commit" % gate["id"])


# --------------------------------------------------------------------------- the verdict and the executive summary

def headline(st: dict, gate: Optional[dict], problems: Sequence[dict] = ()) -> Optional[Labelled]:
    causes = []
    if problems:
        causes.append("ledger verification found %d problem%s" % (len(problems), "" if len(problems) == 1 else "s"))
    if st["violations"]:
        causes.append("the state fold recorded %d violation%s" % (len(st["violations"]), "" if len(st["violations"]) == 1 else "s"))
    criterion = integrity_criterion(gate)
    if criterion is not None and criterion["outcome"] == "NOT_MET":
        causes.append("the latest gate evaluation found test integrity NOT_MET")
    return Labelled(HEADLINE_LABEL, "%s; see Integrity." % "; ".join(causes)) if causes else None


def verdict(gate: Optional[dict], problems: Sequence[dict] = ()) -> Union[str, Labelled]:
    if gate is None:
        return "No gate evaluation recorded; no verdict."
    criterion = integrity_criterion(gate)
    if problems and not (criterion is not None and criterion["outcome"] == "NOT_MET"):
        # No integrity failure in the recorded gate, yet verification fails now: the gate was computed before the change.
        # A gate that already found test integrity NOT_MET did see it, so its verdict stands.
        return Labelled(STALE_LABEL, "%s (%s, profile %s, independence %s, state %s). It was computed before the change that "
                        "ledger verification found and cannot be relied on; repair the ledger and evaluate the gate again."
                        % (gate["verdict"], gate["id"], gate["profile"], gate["independence"], gate["state_hash"]))
    return Labelled(VERDICT_LABEL, "%s (%s, profile %s, independence %s, state %s)" % (
        gate["verdict"], gate["id"], gate["profile"], gate["independence"], gate["state_hash"]))


def executive_rows(st: dict) -> List[dict]:
    """Unresolved findings that need work, most urgent first: priority, then severity, then ID."""
    rows = [dict(f, id=fid) for fid, f in st["findings"].items() if f["state"] in UNRESOLVED and f["priority"] != "none"]
    return sorted(rows, key=lambda f: (f["priority"], SEVERITY_ORDER.index(f["severity"]), _number(f["id"])))


def executive_summary(st: dict, gate: Optional[dict]) -> Table:
    """The counts, as (measure, value) rows."""
    unresolved = [f for f in st["findings"].values() if f["state"] in UNRESOLVED]
    rows: List[list] = [["Unresolved findings", str(len(unresolved))]]
    by_severity = ["%s %d" % (s, sum(1 for f in unresolved if f["severity"] == s)) for s in SEVERITY_ORDER
                   if any(f["severity"] == s for f in unresolved)]
    if by_severity:
        rows.append(["By severity", ", ".join(by_severity)])
    rows.append(["Executions recorded", str(len(st["executions"]))])
    if gate is not None:
        met = sum(1 for c in gate["criteria"] if c["outcome"] == "MET")
        rows.append([gate["id"], "%d of %d criteria MET" % (met, len(gate["criteria"]))])
    return Table(("Measure", "Value"), rows)


def recommendations(st: dict) -> Tuple[Union[str, Table], int]:
    """The first ``EXECUTIVE_ROW_LIMIT`` findings of ``executive_rows`` as numbered rows, and how many more there are."""
    ranked = executive_rows(st)
    if not ranked:
        return NO_UNRESOLVED, 0
    rows = [[n, f["priority"], f["id"], f["severity"], f["category"], f["recommendation"] or f["title"]]
            for n, f in enumerate(ranked[:EXECUTIVE_ROW_LIMIT], 1)]
    return Table(("#", "Priority", "Finding", "Severity", "Category", "Recommendation"), rows), max(0, len(ranked) - EXECUTIVE_ROW_LIMIT)


def more_findings(count: int, see: str) -> str:
    """The line that counts the recommendations not listed; ``see`` names where each format lists them all."""
    return "%d more unresolved findings are not listed here, lowest priority last; see %s." % (count, see)


def follow_up(gate: Optional[dict]) -> Union[str, Table]:
    """What the latest gate still lacks: each criterion neither met nor not applicable."""
    if gate is None:
        return NO_GATE
    rows = [[gate["id"], c["id"], c["outcome"], criterion_kind(c)] for c in gate["criteria"] if c["outcome"] not in ("MET", "NOT_APPLICABLE")]
    return Table(("Gate", "Criterion", "Outcome", "Kind"), rows) if rows else ALL_MET


# --------------------------------------------------------------------------- the sections of the full report

def gate_section(gate: Optional[dict]) -> List[Block]:
    if gate is None:
        return [NO_GATE]
    blocks: List[Block] = [Table(("Criterion", "Outcome", "Kind", "Detail"),
                                 [[c["id"], c["outcome"], criterion_kind(c), c["detail"]] for c in gate["criteria"]])]
    if gate["conditions"]:
        blocks.append(Bullets(list(gate["conditions"])))
    return blocks


def execution_section(st: dict, *, wide: bool = False) -> List[Block]:
    """``wide`` adds the recorded command and the evidence file names."""
    if not st["executions"]:
        return ["No executions recorded."]
    header = ("ID", "Status", "Reason", "Strength", "Scope", "Total", "Failures", "Errors", "Skipped", "Commit")
    rows = []
    for eid in in_id_order(st["executions"]):
        e = st["executions"][eid]
        row = [eid, e["status"], e["reason"], e["strength"], e["scope"], *counts(e), short_commit(e)]
        if wide:
            manifest = e["evidence_manifest"]
            row += [command_line(e["command"]["argv"], e["command"]["shell"]), list(manifest["files"]) if manifest else None]
        rows.append(row)
    return [Table(header + (("Command", "Evidence") if wide else ()), rows)]


def finding_section(st: dict, *, wide: bool = False) -> List[Block]:
    """``wide`` adds each finding's evidence entries."""
    if not st["findings"]:
        return ["No findings recorded."]
    rows = []
    for fid in in_id_order(st["findings"]):
        f = st["findings"][fid]
        rows.append([fid, f["title"], f["severity"], f["priority"], f["state"], f["verification_status"]]
                    + ([list(f["evidence"])] if wide else []))
    return [Table(("ID", "Title", "Severity", "Priority", "State", "Verification") + (("Evidence",) if wide else ()), rows)]


def traceability_section(st: dict, gate: Optional[dict]) -> List[Block]:
    if not (st["requirements"] or st["testcases"]):
        return ["No requirements or test cases recorded."]
    target, source = traceability_target(st, gate)
    if target is None:
        return ["No execution on a recorded commit; the matrix needs one."]
    matrix = trace.matrix(st, target)
    blocks: List[Block] = ["Target commit: %s (%s)" % (target[:12], source),
                           Table(("Gap kind", "Count"), [[k, matrix["counts"][k]] for k in trace.GAP_KINDS])]
    if matrix["gaps"]:
        blocks.append(Table(("Record", "Gap"), [[g["id"], g["kind"]] for g in matrix["gaps"]]))
    return blocks


def integrity_section(st: dict, gate: Optional[dict], problems: Sequence[dict] = (), *,
                      code: Callable[[str], str] = lambda text: text) -> List[Block]:
    """``code`` shows a command name in the renderer's own style."""
    blocks: List[Block] = []
    if problems:
        blocks.append("Ledger verification found %d problem%s." % (len(problems), "" if len(problems) == 1 else "s"))
        blocks.append(Table(("Kind", "Where", "Detail"), [[p["kind"], p["where"], p["detail"]] for p in problems[:INTEGRITY_ROWS_SHOWN]]))
        if len(problems) > INTEGRITY_ROWS_SHOWN:
            blocks.append("%d more problems are not listed; run %s." % (len(problems) - INTEGRITY_ROWS_SHOWN, code(LEDGER_VERIFY)))
    if st["violations"]:
        blocks.append(Table(("Event", "Kind"), [[v["event"], v["kind"]] for v in sorted(st["violations"], key=lambda v: v["event"])]))
    else:
        blocks.append("The state fold found no violations.")
    criterion = integrity_criterion(gate)
    if criterion is not None:
        blocks.append(Line(("Criterion", "Outcome", "Detail"), [criterion["id"], criterion["outcome"], criterion["detail"]]))
    return blocks


def _differences(differences: dict) -> List[str]:
    shown = {True: "true", False: "false", None: "null"}
    return ["%s: %s" % (name, shown[value] if value is None or isinstance(value, bool) else value)
            for name, value in sorted(differences.items())]


def reexecution_section(st: dict) -> List[Block]:
    """Each imported bundle: its facts, its comparisons with the local runs, and its executions."""
    if not st["bundles"]:
        return ["No evidence bundles imported."]
    blocks: List[Block] = []
    for bid in in_id_order(st["bundles"]):
        record = st["bundles"][bid]
        source = record["source"]
        blocks += [Heading(3, bid), Table(("Field", "Value"), [
            ["SHA-256", record["sha256"]], ["Manifest SHA-256", record["manifest_sha256"]],
            ["Source kernel version", source["kernel_version"]], ["Source state hash", source["state_hash"]],
            ["Files", record["files"]], ["Imported executions", len(record["executions"])],
            ["Source ledger violations", record["violations"]], ["Authorizations ignored", record["ignored"]["authorizations"]]]),
            Heading(4, "Comparisons")]
        compared = [[c["local"], c["remote"], c["commit"], c["matching"], _differences(c["differences"])] for c in record["comparisons"]]
        blocks.append(Table(("Local", "Remote", "Commit", "Matching", "Differences"), compared) if compared
                      else "No local execution ran the same command at the same commit.")
        blocks.append(Heading(4, "Imported executions"))
        imported = [[eid, e["status"], e["reason"], e["strength"], e["exit_code"], e["activity"], e["scope"], e["commit"],
                     command_line(e["argv"])] for eid, e in sorted(record["executions"].items())]
        blocks.append(Table(("ID", "Status", "Reason", "Strength", "Exit code", "Activity", "Scope", "Commit", "Command"), imported)
                      if imported else "The bundle holds no executions.")
    return blocks


def limitation_section(st: dict) -> List[Block]:
    blocks: List[Block] = [Bullets(list(LIMITS))]
    rows = [[eid, text] for eid in in_id_order(st["executions"]) for text in st["executions"][eid]["limitations"]]
    if rows:
        blocks.append(Table(("Execution", "Limitation"), rows))
    return blocks
