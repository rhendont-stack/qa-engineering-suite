"""Deterministic, first-match rules that turn one execution's facts into one of the five canonical statuses."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional, Tuple

from qa.ingest import ReportSummary

STATUSES = ("passed", "failed", "blocked", "not_run", "inconclusive")
ANOMALY_ORDER = ("exit_code_mismatch", "no_tests_collected", "retried_in_framework", "partial", "untested_tool_version",
                 "network_not_isolated")
CLEAN_ALERT_LEVELS = ("note", "none")

ENV_ERROR_SIGNATURES: Tuple[str, ...] = (
    "ModuleNotFoundError",
    "ImportError: No module named",
    "Cannot find module",
    "ECONNREFUSED",
    "Connection refused",
    "Could not resolve host",
    "getaddrinfo ENOTFOUND",
    "command not found",
    "Could not find or load main class",
)


@dataclass
class StatusInputs:
    guard_denied: bool = False
    not_authorized: bool = False
    config_drift: bool = False
    spawn_error: Optional[str] = None  # not_found | not_executable | spawn_failed
    shell: bool = False
    exit_code: Optional[int] = None  # None: never ran, or ended on a signal
    timed_out: bool = False
    dependency_unavailable: bool = False
    report_declared: bool = False
    report_error: Optional[str] = None  # report_missing | report_invalid
    summary: Optional[ReportSummary] = None
    allow_empty: bool = False
    stderr_tail: str = ""
    orphaned_output: bool = False
    anomalies: Tuple[str, ...] = ()


@dataclass(frozen=True)
class StatusDecision:
    status: str
    reason: Optional[str]
    strength: Optional[str]
    anomalies: Tuple[str, ...]
    classification: str  # deterministic | heuristic


def _strength(inp: StatusInputs) -> Optional[str]:
    if inp.summary is not None:
        return "structured"
    if isinstance(inp.exit_code, int):
        return "exit_code"
    return None


def _decision(inp: StatusInputs, status: str, reason: Optional[str], extra: Tuple[str, ...] = (),
              strength: Optional[str] = "auto", classification: str = "deterministic") -> StatusDecision:
    found = set(extra) | set(inp.anomalies)
    if inp.summary is not None:
        found.update(inp.summary.anomalies)
    if inp.orphaned_output:
        found.add("partial")
    found.discard(reason)
    anomalies = tuple(a for a in ANOMALY_ORDER if a in found)
    return StatusDecision(status, reason, _strength(inp) if strength == "auto" else strength, anomalies, classification)


def decide(inp: StatusInputs) -> StatusDecision:
    decision = _first_match(inp)
    # A pass means the tests ran with no network; without proof of that, the run shows nothing either way.
    if decision.status == "passed" and "network_not_isolated" in decision.anomalies:
        return replace(decision, status="inconclusive", reason="network_not_isolated")
    return decision


def _first_match(inp: StatusInputs) -> StatusDecision:
    # Rule 1: policy, drift, authorization.
    if inp.guard_denied:
        return _decision(inp, "blocked", "policy_denied", strength=None)
    if inp.config_drift:
        return _decision(inp, "blocked", "config_drift", strength=None)
    if inp.not_authorized:
        return _decision(inp, "blocked", "not_authorized", strength=None)
    # Rule 2: could not execute.
    if inp.spawn_error is not None:
        reason = "tool_missing" if inp.spawn_error == "not_found" else "not_executable"
        return _decision(inp, "blocked", reason, strength=None)
    if inp.shell and inp.exit_code == 127:
        return _decision(inp, "blocked", "tool_missing")
    if inp.shell and inp.exit_code == 126:
        return _decision(inp, "blocked", "not_executable")
    if inp.dependency_unavailable:
        return _decision(inp, "blocked", "dependency_unavailable")
    # Rule 3: timeout.
    if inp.timed_out:
        return _decision(inp, "inconclusive", "timeout", ("partial",) if inp.summary is not None else ())
    # Rule 4: a declared report that is absent or unusable.
    if inp.report_error is not None:
        return _decision(inp, "inconclusive", inp.report_error)
    if inp.report_declared and inp.summary is None:
        return _decision(inp, "inconclusive", "report_missing")
    clean_exit = inp.exit_code == 0  # None (ended on a signal) counts as non-zero
    if inp.summary is not None:
        if inp.summary.kind == "alerts":
            return _decide_alerts(inp, clean_exit)
        return _decide_tests(inp, clean_exit)
    # Rule 10: no report, exit 0.
    if clean_exit:
        return _decision(inp, "passed", None)
    # Rule 11: no report, non-zero exit.
    if any(sig in inp.stderr_tail for sig in ENV_ERROR_SIGNATURES):
        return _decision(inp, "blocked", "environment_error", classification="heuristic")
    return _decision(inp, "failed", "nonzero_exit")


def _decide_tests(inp: StatusInputs, clean_exit: bool) -> StatusDecision:
    cases = inp.summary.cases
    empty_ok: Tuple[str, ...] = ()
    if not cases:
        if not inp.allow_empty:
            return _decision(inp, "inconclusive", "no_tests_collected")  # rule 5
        empty_ok = ("no_tests_collected",)  # counts as passes only, rules 8-9
    failed = [c for c in cases if c.status == "failed"]
    if failed:  # rule 6
        reason = "assertion" if any(c.failure_kind == "assertion" for c in failed) else "error"
        return _decision(inp, "failed", reason, ("exit_code_mismatch",) if inp.exit_code == 0 else ())
    if cases and all(c.status == "not_run" for c in cases):  # rule 7
        return _decision(inp, "not_run", "all_skipped", () if clean_exit else ("exit_code_mismatch",))
    if not clean_exit:  # rule 8
        return _decision(inp, "inconclusive", "exit_code_mismatch", empty_ok)
    return _decision(inp, "passed", None, empty_ok)  # rule 9


def _decide_alerts(inp: StatusInputs, clean_exit: bool) -> StatusDecision:
    summary = inp.summary
    if summary.runs == 0:
        return _decision(inp, "inconclusive", "report_invalid")
    if any(a.level not in CLEAN_ALERT_LEVELS for a in summary.alerts):
        return _decision(inp, "failed", "assertion")  # scanners exit non-zero on findings: no mismatch
    if clean_exit:
        return _decision(inp, "passed", None)
    return _decision(inp, "inconclusive", "exit_code_mismatch")


def can_reclassify(old: str, new: str) -> bool:
    """Only between two different non-passing statuses, or from passed to inconclusive; nothing upgrades to passed."""
    if old not in STATUSES or new not in STATUSES or old == new:
        return False
    if old == "passed":
        return new == "inconclusive"
    return new != "passed"
