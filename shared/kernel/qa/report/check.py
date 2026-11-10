"""``check_markdown``: every claim in a report must cite a recorded run (§6.4 P, card P).

It reads lines, skips fenced code and the generated title, and flags what a reader could mistake for evidence: a status
word, a verdict or a percentage with no ID that resolves; a status or verdict that differs from the cited record's; an ID the
state does not hold; and universal wording that no full-scope passed execution backs.

``check_markdown`` may be given the states of other ledgers (``qa report check --ledger [ALIAS=]PATH``), each with an
optional alias. An ID may be cited bare (``GATE-001``) or qualified by the ledger that holds it (``f4:GATE-001``; the alias
``project`` names the report's own ledger). A bare ID resolves in any ledger. A numbered ID (``GATE-``, ``RPT-``, ``AUTH-``,
``PLAN-``, ``FND-``) is handed out by each ledger on its own, so a bare one that more than one ledger holds is ambiguous and
must be qualified; an ``EXE-`` or ``RES-`` ID is unique to its run, so two ledgers that hold it must record it identically.

A verdict word is judged against every cited ``GATE-`` ID and against the gate each cited ``RPT-`` ID names: all must agree,
and a verdict with neither is uncited. A sentence that sets an expectation beside a measurement labels the expected word
``expected: TOKEN``; only that one token is left unjudged.
"""
from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

from .. import state
from ..ledger import Problem

# Problem.kind values: unknown_id, uncited_claim, status_mismatch, universal_claim, conflicting_id, ambiguous_id.

PROJECT_ALIAS = "project"
ALIAS = re.compile(r"[a-z][a-z0-9-]{0,31}")
_B32 = "[0-9A-HJKMNP-TV-Z]{26}"
_ID_TABLE = (  # prefix, pattern, state key (None: state.known_references)
    ("EXE", "EXE-" + _B32, None), ("RES", "RES-" + _B32, "results"), ("FND", r"FND-[0-9]{3,}", "findings"),
    ("AUTH", r"AUTH-[0-9]{3,}", "authorizations"), ("GATE", r"GATE-[0-9]{3,}", "gates"), ("RPT", r"RPT-[0-9]{3,}", "reports"),
    ("PLAN", r"PLAN-[0-9]{4,}", "plans"), ("EV", "EV-[0-9a-f]{12}", None),
)
_NUMBERED = ("GATE-", "RPT-", "AUTH-", "PLAN-", "FND-")
_ID = re.compile(r"(?<![\w:-])(?:([a-z][a-z0-9-]{0,31}):)?(%s)(?![\w-])" % "|".join(pattern for _, pattern, _ in _ID_TABLE))
_STATUS = re.compile(r"(?<!\w)(passed|failed|blocked|inconclusive|not\s+run)(?!\w)", re.IGNORECASE)
_VERDICT = re.compile(r"(?<!\w)(READY_WITH_CONDITIONS|INSUFFICIENT_EVIDENCE|NOT_READY|READY)(?!\w)")
_EXPECTED = re.compile(r"(?i:expected):\s*[*_`]*(?=(?:READY_WITH_CONDITIONS|INSUFFICIENT_EVIDENCE|NOT_READY|READY|passed|failed"
                       r"|blocked|inconclusive|not\s+run)(?!\w))", re.IGNORECASE)
_PERCENT = re.compile(r"\d\s*%")
_UNIVERSAL = re.compile(r"(?<!\w)(?:all\s+tests\s+pass(?:ed)?|every\s+test|fully\s+tested|fully\s+scanned|every\s+endpoint"
                        r"|100%\s+coverage|complete\s+coverage|no\s+bugs|no\s+vulnerabilities)(?!\w)", re.IGNORECASE)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_TITLE = re.compile(r"^# QA report:")

Ledger = Tuple[Optional[str], dict]  # (alias, state)


def _known(st: dict, token: str) -> bool:
    for prefix, _, key in _ID_TABLE:
        if token.startswith(prefix + "-"):
            if key is None:
                return token in state.known_references(st)
            return token in st[key]
    return False


def _status_word(match: str) -> str:
    return "not_run" if match.lower().startswith("not") else match.lower()


def _record(st: dict, token: str) -> dict:
    return st["executions" if token.startswith("EXE-") else "results"][token]


class _Cited:
    """One ID on a line: how it was written, and the states that hold it under that citation."""

    def __init__(self, ledgers: Sequence[Ledger], alias: Optional[str], token: str) -> None:
        self.alias, self.token = alias, token
        self.shown = token if alias is None else "%s:%s" % (alias, token)
        scope = [st for name, st in ledgers if alias is None or name == alias]
        self.holders = [st for st in scope if _known(st, token)]

    @property
    def resolved(self) -> bool:
        return bool(self.holders)

    @property
    def ambiguous(self) -> bool:
        return self.alias is None and self.token.startswith(_NUMBERED) and len(self.holders) > 1

    @property
    def conflicting(self) -> bool:
        if not self.token.startswith(("EXE-", "RES-")):
            return False
        records = [_record(st, self.token) for st in self.holders]
        return any(record != records[0] for record in records[1:])

    @property
    def usable(self) -> bool:
        """Resolved to one record a claim can be judged by."""
        return self.resolved and not self.ambiguous and not self.conflicting

    @property
    def st(self) -> dict:
        return self.holders[0]


def _universal_backed(cited: Sequence[_Cited]) -> bool:
    for item in cited:
        if item.usable and item.token.startswith("EXE-"):
            record = _record(item.st, item.token)
            if record["scope"] == "full" and record["status"] == "passed":
                return True
    return False


def _gate_verdicts(item: _Cited) -> Optional[Tuple[str, str]]:
    """(what to name, recorded verdict) of a usable GATE- ID or of the gate a usable RPT- ID names; None when it names none."""
    if item.token.startswith("GATE-"):
        return item.shown, item.st["gates"][item.token]["verdict"]
    gate = item.st["reports"][item.token].get("gate")
    if gate is None or gate not in item.st["gates"]:
        return None
    return "%s (gate %s)" % (item.shown, gate), item.st["gates"][gate]["verdict"]


def _line_problems(ledgers: Sequence[Ledger], line: str, where: str) -> List[Problem]:
    problems: List[Problem] = []
    seen = list(dict.fromkeys(_ID.findall(line)))
    cited = [_Cited(ledgers, alias or None, token) for alias, token in seen]
    problems += [Problem("unknown_id", where, c.shown) for c in cited if not c.resolved]
    problems += [Problem("ambiguous_id", where, "%s is held by more than one ledger; qualify it with the ledger's alias, as in "
                         "ALIAS:%s" % (c.token, c.token)) for c in cited if c.resolved and c.ambiguous]
    problems += [Problem("conflicting_id", where, "%s differs between the ledgers" % c.shown) for c in cited if c.resolved and c.conflicting]
    judged = _EXPECTED.sub(lambda m: m.group(0) + "\0", line)  # a NUL right before the labelled token hides it from the scans
    judged = re.sub(r"\0\S+", "", judged)
    statuses = [_status_word(m) for m in _STATUS.findall(judged)]
    verdicts = _VERDICT.findall(judged)
    resolved = [c for c in cited if c.resolved]
    if (statuses or verdicts or _PERCENT.search(judged)) and not resolved:
        problems.append(Problem("uncited_claim", where, line.strip()))
    records = [c for c in cited if c.token.startswith(("EXE-", "RES-"))]  # an unknown ID is still a cited one
    if len(records) == 1 and records[0].usable and statuses:
        current = _record(records[0].st, records[0].token)["status"]
        if any(s != current for s in statuses):
            problems.append(Problem("status_mismatch", where, "%s is %s" % (records[0].shown, current)))
    if verdicts and resolved:
        gates = [c for c in cited if c.token.startswith(("GATE-", "RPT-"))]
        judges = [j for j in (_gate_verdicts(c) for c in gates if c.usable) if j is not None]
        if not gates or (all(c.usable for c in gates) and not judges):
            problems.append(Problem("uncited_claim", where, "a verdict needs a GATE- or RPT- ID that names a gate: " + line.strip()))
        for shown, recorded in judges:
            if any(v != recorded for v in verdicts):
                problems.append(Problem("status_mismatch", where, "%s is %s" % (shown, recorded)))
    if _UNIVERSAL.search(line) and not _universal_backed(cited):
        problems.append(Problem("universal_claim", where, line.strip()))
    return problems
def _opening_fence(line: str) -> Optional[str]:
    """The run that opens a fence on ``line``. A backtick run with a backtick after it is inline code, not a fence."""
    match = _FENCE.match(line)
    if match is None or (match.group(1)[0] == "`" and "`" in match.group(2)):
        return None
    return match.group(1)


def _closes(line: str, fence: str) -> bool:
    """A closer is the same character, at least as long as the opener, with nothing after it but spaces."""
    match = _FENCE.match(line)
    return (match is not None and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence)
            and not match.group(2).strip())


def check_markdown(text: str, st: dict, extra: Sequence[dict] = (), aliases: Sequence[Optional[str]] = ()) -> List[Problem]:
    """Problems of ``text`` against ``st`` and the ``extra`` states; ``aliases[i]`` (or None) names ``extra[i]``."""
    names = list(aliases) + [None] * (len(extra) - len(aliases))
    ledgers: List[Ledger] = [(PROJECT_ALIAS, st)] + list(zip(names, extra))
    problems: List[Problem] = []
    fence: Optional[str] = None
    for number, line in enumerate(text.splitlines(), 1):
        if fence is not None:
            if _closes(line, fence):
                fence = None
            continue
        fence = _opening_fence(line)
        if fence is not None or (number == 1 and _TITLE.match(line)):  # the title holds the project's own name
            continue
        problems += _line_problems(ledgers, line, "line %d" % number)
    return problems
