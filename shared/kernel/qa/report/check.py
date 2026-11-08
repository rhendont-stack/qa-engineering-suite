"""``check_markdown``: every claim in a report must cite a recorded run (§6.4 P, card P).

It reads lines, skips fenced code and headings, and flags what a reader could mistake for evidence: a status word, a
verdict or a percentage with no ID that resolves; a status or verdict that differs from the cited record's; an ID the
state does not hold; and universal wording that no full-scope passed execution backs.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from .. import state
from ..ledger import Problem

# Problem.kind values: unknown_id, uncited_claim, status_mismatch, universal_claim.

_B32 = "[0-9A-HJKMNP-TV-Z]{26}"
_ID_TABLE = (  # prefix, pattern, state key (None: state.known_references)
    ("EXE", "EXE-" + _B32, None), ("RES", "RES-" + _B32, "results"), ("FND", r"FND-[0-9]{3,}", "findings"),
    ("AUTH", r"AUTH-[0-9]{3,}", "authorizations"), ("GATE", r"GATE-[0-9]{3,}", "gates"), ("RPT", r"RPT-[0-9]{3,}", "reports"),
    ("EV", "EV-[0-9a-f]{12}", None),
)
_ID = re.compile(r"(?<![\w-])(?:%s)(?![\w-])" % "|".join(pattern for _, pattern, _ in _ID_TABLE))
_STATUS = re.compile(r"(?<!\w)(passed|failed|blocked|inconclusive|not\s+run)(?!\w)", re.IGNORECASE)
_VERDICT = re.compile(r"(?<!\w)(READY_WITH_CONDITIONS|INSUFFICIENT_EVIDENCE|NOT_READY|READY)(?!\w)")
_PERCENT = re.compile(r"\d\s*%")
_UNIVERSAL = re.compile(r"(?<!\w)(?:all\s+tests\s+pass(?:ed)?|every\s+test|fully\s+tested|fully\s+scanned|every\s+endpoint"
                        r"|100%\s+coverage|complete\s+coverage|no\s+bugs|no\s+vulnerabilities)(?!\w)", re.IGNORECASE)
_FENCE = re.compile(r"^ {0,3}(```|~~~)")
_HEADING = re.compile(r"^ {0,3}#{1,6}(\s|$)")


def _known(st: dict, token: str) -> bool:
    for prefix, _, key in _ID_TABLE:
        if token.startswith(prefix + "-"):
            if key is None:
                return token in state.known_references(st)
            return token in st[key]
    return False


def _status_word(match: str) -> str:
    return "not_run" if match.lower().startswith("not") else match.lower()


def _universal_backed(st: dict, cited: List[str]) -> bool:
    for token in cited:
        record = st["executions"].get(token)
        if record is not None and record["scope"] == "full" and record["status"] == "passed":
            return True
    return False


def _line_problems(st: dict, line: str, where: str) -> List[Problem]:
    problems: List[Problem] = []
    tokens = list(dict.fromkeys(_ID.findall(line)))
    resolved = [t for t in tokens if _known(st, t)]
    problems += [Problem("unknown_id", where, t) for t in tokens if t not in resolved]
    statuses = [_status_word(m) for m in _STATUS.findall(line)]
    verdicts = _VERDICT.findall(line)
    if (statuses or verdicts or _PERCENT.search(line)) and not resolved:
        problems.append(Problem("uncited_claim", where, line.strip()))
    records = [t for t in tokens if t.startswith(("EXE-", "RES-"))]  # an unknown ID is still a cited one
    gates = [t for t in tokens if t.startswith("GATE-")]
    if len(records) == 1 and records[0] in resolved and statuses:
        token = records[0]
        current = (st["executions"] if token.startswith("EXE-") else st["results"])[token]["status"]
        if any(s != current for s in statuses):
            problems.append(Problem("status_mismatch", where, "%s is %s" % (token, current)))
    if len(gates) == 1 and gates[0] in resolved and verdicts and any(v != st["gates"][gates[0]]["verdict"] for v in verdicts):
        problems.append(Problem("status_mismatch", where, "%s is %s" % (gates[0], st["gates"][gates[0]]["verdict"])))
    if _UNIVERSAL.search(line) and not _universal_backed(st, resolved):
        problems.append(Problem("universal_claim", where, line.strip()))
    return problems


def check_markdown(text: str, st: dict) -> List[Problem]:
    problems: List[Problem] = []
    fence: Optional[str] = None
    for number, line in enumerate(text.splitlines(), 1):
        opened = _FENCE.match(line)
        if fence is not None:
            if opened and opened.group(1) == fence:
                fence = None
            continue
        if opened:
            fence = opened.group(1)
            continue
        if _HEADING.match(line):
            continue
        problems += _line_problems(st, line, "line %d" % number)
    return problems
