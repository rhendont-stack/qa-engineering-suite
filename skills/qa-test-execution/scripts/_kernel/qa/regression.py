"""FAIL_TO_PASS and PASS_TO_PASS: what a revalidation's outcome is, derived from recorded runs alone (§6.4 G).

A pure function over the state ``state.fold`` builds, shared by ``fixes.revalidate`` (which records the answer) and the fold
(which recomputes it, so a hand-written ``passed`` verifies nothing).

``check_inputs`` refuses what cannot stand for the fix: an execution the state does not hold, one at a commit other than the
fix's ``base_commit`` (base side) or ``fix_commit`` (head side), or an attempt chain that crosses commits. ``derive`` then
answers ``passed``, ``failed`` or ``inconclusive``:

* a run on a dirty tree cannot stand for a commit, and a run that did not conclude says nothing: ``inconclusive``, unless a
  comparable pair already shows a definite failure;
* FAIL_TO_PASS: every defect test is ``failed`` with ``failure_kind: assertion`` in the base run and ``passed`` in the head run;
* PASS_TO_PASS: the pair is comparable and has no ``new_failures``, no ``disappeared`` and no ``still_failing`` outside the
  defect tests.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from . import compare


class Refused(Exception):
    """The inputs cannot be judged. ``code`` is the fold's violation kind, ``why`` the reason ``revalidate`` reports."""

    def __init__(self, code: str, why: str, detail: str) -> None:
        super().__init__(detail)
        self.code, self.why, self.detail = code, why, detail


def _slots(f2p: Sequence[str], p2p: Sequence[str]) -> List[Tuple[str, str, str]]:
    return [("FAIL_TO_PASS base", f2p[0], "base"), ("FAIL_TO_PASS head", f2p[1], "head"),
            ("PASS_TO_PASS base", p2p[0], "base"), ("PASS_TO_PASS head", p2p[1], "head")]


def _commit(record: dict) -> Optional[str]:
    git = record.get("git")
    return git.get("commit") if isinstance(git, dict) else None


def check_inputs(st: dict, fix: dict, f2p: Sequence[str], p2p: Sequence[str]) -> None:
    """Raise ``Refused`` unless all four executions exist, sit at the right commits, and keep each attempt chain on one commit."""
    wanted = {"base": fix["base_commit"], "head": fix["fix_commit"]}
    for label, exe, side in _slots(f2p, p2p):
        if exe not in st["executions"]:
            raise Refused("unknown_execution", "unknown_execution", "%s %s is not in the state" % (label, exe))
    for label, exe, side in _slots(f2p, p2p):
        if _commit(st["executions"][exe]) != wanted[side]:
            raise Refused("commit_mismatch", "commit_mismatch", "%s %s is not at the fix's %s commit" % (
                label, exe, "base" if side == "base" else "fix"))
        seen, current = {exe}, st["executions"][exe].get("attempt_of")
        while current is not None and current in st["executions"] and current not in seen:
            seen.add(current)
            if _commit(st["executions"][current]) != wanted[side]:
                raise Refused("mixed_attempt_chain", "mixed_attempt_chain",
                              "%s %s is an attempt of %s, which is at another commit" % (label, exe, current))
            current = st["executions"][current].get("attempt_of")


def _failures_of(st: dict, execution_id: str, test_id: str) -> List[Optional[str]]:
    return [r.get("failure_kind") for r in st["results"].values()
            if r["execution"] == execution_id and r["test_id"] == test_id and r["status"] == "failed"]


def _fail_to_pass(st: dict, f2p: Sequence[str], defect_tests: Sequence[str]) -> List[str]:
    """The reasons FAIL_TO_PASS does not hold; empty when it does."""
    base_tests, head_tests = compare.tests_of(st, f2p[0]), compare.tests_of(st, f2p[1])
    reasons: List[str] = []
    for test_id in defect_tests:
        before, after = base_tests.get(test_id), head_tests.get(test_id)
        if before is None or before["status"] != "failed":
            reasons.append("defect test %s did not fail on the base (%s)" % (test_id, "absent" if before is None else before["status"]))
        elif any(kind != "assertion" for kind in _failures_of(st, f2p[0], test_id)):
            reasons.append("defect test %s did not fail by assertion on the base (an error is not a FAIL_TO_PASS)" % test_id)
        if after is None or after["status"] != "passed":
            reasons.append("defect test %s did not pass on the fix (%s)" % (test_id, "absent" if after is None else after["status"]))
    return reasons


def derive(st: dict, fix: dict, f2p: Sequence[str], p2p: Sequence[str], defect_tests: Sequence[str]) -> Dict[str, Optional[str]]:
    """``{"outcome", "reason"}``; ``check_inputs`` has passed. The reason is None only for ``passed``."""
    inconclusive: List[str] = []
    for label, exe, _side in _slots(f2p, p2p):
        git = st["executions"][exe].get("git") or {}
        if git.get("dirty"):
            inconclusive.append("%s %s ran on a dirty tree and cannot stand for a commit" % (label, exe))
    if inconclusive:
        return {"outcome": "inconclusive", "reason": "; ".join(inconclusive)}
    failed: List[str] = []
    pair = compare.compare(st, f2p[0], f2p[1])
    if pair["comparable"]:
        failed += _fail_to_pass(st, f2p, defect_tests)
    else:
        inconclusive.append("FAIL_TO_PASS: " + str(pair["comparable_reason"]))
    regression = compare.compare(st, p2p[0], p2p[1])
    if regression["comparable"]:
        failed += ["PASS_TO_PASS: %s newly failing" % ", ".join(regression["new_failures"])] if regression["new_failures"] else []
        failed += ["PASS_TO_PASS: %s gone from the fix's run" % ", ".join(regression["disappeared"])] if regression["disappeared"] else []
        stuck = [t for t in regression["still_failing"] if t not in defect_tests]
        failed += ["PASS_TO_PASS: %s failing on both commits" % ", ".join(stuck)] if stuck else []
    else:
        inconclusive.append("PASS_TO_PASS: " + str(regression["comparable_reason"]))
    if failed:
        return {"outcome": "failed", "reason": "; ".join(failed)}
    if inconclusive:
        return {"outcome": "inconclusive", "reason": "; ".join(inconclusive)}
    return {"outcome": "passed", "reason": None}
