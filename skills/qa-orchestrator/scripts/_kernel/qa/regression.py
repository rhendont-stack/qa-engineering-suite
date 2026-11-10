"""FAIL_TO_PASS and PASS_TO_PASS: what a revalidation's outcome is, derived from recorded runs alone (§6.4 G).

A pure function over the state ``state.fold`` builds, shared by ``fixes.revalidate`` (which records the answer) and the fold
(which recomputes it, so a hand-written ``passed`` verifies nothing).

``check_inputs`` refuses what cannot stand for the fix: an execution the state does not hold, one at a commit other than the
fix's ``base_commit`` (base side) or ``fix_commit`` (head side), or an attempt chain that crosses commits. ``derive`` then
answers ``passed``, ``failed`` or ``inconclusive``:

Precedence, first match: (1) any of the four runs on a dirty tree: ``inconclusive``; (2) any definite failure in a pair
that concluded: ``failed``; (3) any condition that concludes nothing: ``inconclusive``; (4) ``passed``.

* FAIL_TO_PASS, per defect test. Satisfied: failed by assertion in the base run and passed in the head run. Definite failure:
  passed in the base run, or failed by assertion in the head run. Anything else concludes nothing: absent, not_run, blocked or
  inconclusive on either side, a ``failure_kind`` other than assertion, or a flaky result.
* PASS_TO_PASS is every test outside the defect tests in the two runs. Passed on both: fine. Definite failure: gone from the head
  run, passed then failed, failing on both, or present only at the head and failed there. A test present only at the head and not
  failed is ignored, and one that failed at the base and passes at the head is not a regression. not_run, blocked or
  inconclusive on either side concludes nothing. With no regression test passed on both commits there is no evidence, so
  ``inconclusive``. The FAIL_TO_PASS runs may serve as the regression runs.
* A pair whose run did not end passed or failed, or that holds no per-test results, concludes nothing.
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


def _fail_to_pass(st: dict, f2p: Sequence[str], defect_tests: Sequence[str]) -> Tuple[List[str], List[str]]:
    """``(definite failures, what concluded nothing)`` for the defect tests."""
    base_tests, head_tests = compare.tests_of(st, f2p[0]), compare.tests_of(st, f2p[1])
    failed: List[str] = []
    open_: List[str] = []
    for test_id in defect_tests:
        before, after = base_tests.get(test_id), head_tests.get(test_id)
        if before is None:
            open_.append("defect test %s is absent on the base" % test_id)
        elif before["status"] == "passed":
            failed.append("defect test %s passed on the base, so it does not show the defect" % test_id)
        elif before["status"] != "failed":
            open_.append("defect test %s was %s on the base" % (test_id, before["status"]))
        elif any(kind != "assertion" for kind in _failures_of(st, f2p[0], test_id)):
            open_.append("defect test %s did not fail by assertion on the base (an error is not a FAIL_TO_PASS)" % test_id)
        elif before["flaky"]:
            open_.append("defect test %s is flaky on the base" % test_id)
        if after is None:
            open_.append("defect test %s is absent on the fix" % test_id)
        elif after["status"] == "failed":
            if any(kind != "assertion" for kind in _failures_of(st, f2p[1], test_id)):
                open_.append("defect test %s did not fail by assertion on the fix (an error, not an assertion)" % test_id)
            else:
                failed.append("defect test %s still fails by assertion on the fix" % test_id)
        elif after["status"] != "passed":
            open_.append("defect test %s was %s on the fix" % (test_id, after["status"]))
        elif after["flaky"]:
            open_.append("defect test %s is flaky on the fix" % test_id)
    return failed, open_


def _pass_to_pass(st: dict, p2p: Sequence[str], defect_tests: Sequence[str]) -> Tuple[List[str], List[str]]:
    """``(definite failures, what concluded nothing)`` for the regression set."""
    base_tests, head_tests = compare.tests_of(st, p2p[0]), compare.tests_of(st, p2p[1])
    failed: List[str] = []
    open_: List[str] = []
    passed_on_both = 0
    for test_id in sorted((set(base_tests) | set(head_tests)) - set(defect_tests)):
        before, after = base_tests.get(test_id), head_tests.get(test_id)
        if before is None:
            if after["status"] == "failed":
                failed.append("%s fails on the fix and is not in the base run" % test_id)
        elif after is None:
            failed.append("%s is gone from the fix's run" % test_id)
        elif (before["status"], after["status"]) == ("passed", "passed"):
            if before["flaky"] or after["flaky"]:
                open_.append("regression test %s is flaky" % test_id)
            else:
                passed_on_both += 1
        elif (before["status"], after["status"]) == ("passed", "failed"):
            failed.append("%s passed on the base and fails on the fix" % test_id)
        elif (before["status"], after["status"]) == ("failed", "failed"):
            failed.append("%s fails on both commits" % test_id)
        elif (before["status"], after["status"]) != ("failed", "passed"):
            open_.append("regression test %s was %s on the base and %s on the fix" % (test_id, before["status"], after["status"]))
    if not failed and not open_ and passed_on_both == 0:
        open_.append("no regression evidence: no test outside the defect tests passed on both commits")
    return failed, open_


def derive(st: dict, fix: dict, f2p: Sequence[str], p2p: Sequence[str], defect_tests: Sequence[str]) -> Dict[str, Optional[str]]:
    """``{"outcome", "reason"}``; ``check_inputs`` has passed. The reason is None only for ``passed``."""
    dirty = ["%s %s ran on a dirty tree and cannot stand for a commit" % (label, exe) for label, exe, _side in _slots(f2p, p2p)
             if (st["executions"][exe].get("git") or {}).get("dirty")]
    if dirty:
        return {"outcome": "inconclusive", "reason": "; ".join(dirty)}
    failed: List[str] = []
    open_: List[str] = []
    for name, pair, judge in (("FAIL_TO_PASS", f2p, lambda: _fail_to_pass(st, f2p, defect_tests)),
                              ("PASS_TO_PASS", p2p, lambda: _pass_to_pass(st, p2p, defect_tests))):
        compared = compare.compare(st, pair[0], pair[1])
        if not compared["comparable"]:
            open_.append("%s: %s" % (name, compared["comparable_reason"]))
            continue
        found_failed, found_open = judge()
        failed += ["%s: %s" % (name, text) for text in found_failed]
        open_ += ["%s: %s" % (name, text) for text in found_open]
    if failed:
        return {"outcome": "failed", "reason": "; ".join(failed)}
    if open_:
        return {"outcome": "inconclusive", "reason": "; ".join(open_)}
    return {"outcome": "passed", "reason": None}
