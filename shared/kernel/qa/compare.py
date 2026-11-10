"""Compare two recorded runs test by test: what broke, what got fixed, what is new or gone.

A pure function over the state ``state.fold`` builds. Only structured per-test results count: a run known only by its
exit code says nothing about individual tests, so such a pair is not comparable and nothing is classified.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .errors import QaError

DURATION_RATIO = 1.5  # head at least this many times slower than base ...
DURATION_DELTA_MS = 1000  # ... and at least this much slower in absolute terms; both must hold
_RANK = {"failed": 3, "passed": 2}  # a test repeated in one run: any failure wins, then any pass


_BUNDLE_REF = re.compile(r"(BUNDLE-[0-9]{3,}):(.*)", re.DOTALL)


def _resolve(st: dict, run_id: str) -> str:
    """The id a user-given run id names: an execution of this state, or ``<BUNDLE-ID>:<EXE-ID>`` in an ingested bundle."""
    ref = _BUNDLE_REF.fullmatch(run_id) if isinstance(run_id, str) else None
    if ref is None:
        if run_id not in st["executions"]:
            raise QaError("unknown_execution", "no such execution in the state", id=run_id)
        return run_id
    bundle = st["bundles"].get(ref.group(1))
    if bundle is None:
        raise QaError("unknown_bundle", "no such ingested bundle in the state", id=run_id)
    if ref.group(2) not in bundle["executions"]:
        raise QaError("unknown_execution", "no such execution in that bundle", id=run_id)
    return run_id


def tests_of(st: dict, execution_id: str) -> Dict[str, dict]:
    """{test id: {"status", "flaky", "duration_ms"}} from the execution's recorded results.

    For ``<BUNDLE-ID>:<EXE-ID>``, from what the bundle's own ledger recorded, as ingested: data, never a local result.
    """
    ref = _BUNDLE_REF.fullmatch(execution_id)
    if ref is not None:
        return {test_id: dict(entry) for test_id, entry in st["bundles"][ref.group(1)]["executions"][ref.group(2)]["tests"].items()}
    tests: Dict[str, dict] = {}
    for result in st["results"].values():
        if result["execution"] != execution_id:
            continue
        entry = tests.setdefault(result["test_id"], {"status": result["status"], "flaky": False, "duration_ms": None})
        if _RANK.get(result["status"], 0) > _RANK.get(entry["status"], 0):
            entry["status"] = result["status"]
        entry["flaky"] = entry["flaky"] or result["flaky"]
        duration = result["duration_ms"]
        if duration is not None and (entry["duration_ms"] is None or duration > entry["duration_ms"]):
            entry["duration_ms"] = duration
    return tests


def _slower(test_id: str, base_ms: Optional[int], head_ms: Optional[int]) -> Optional[dict]:
    if base_ms is None or head_ms is None or head_ms - base_ms < DURATION_DELTA_MS:
        return None
    if base_ms > 0 and head_ms / base_ms < DURATION_RATIO:
        return None
    return {"test_id": test_id, "base_ms": base_ms, "head_ms": head_ms,
            "ratio": round(head_ms / base_ms, 2) if base_ms > 0 else None, "delta_ms": head_ms - base_ms}


def compare(st: dict, base_id: str, head_id: str) -> dict:
    base_exe, head_exe = _resolve(st, base_id), _resolve(st, head_id)
    base, head = tests_of(st, base_exe), tests_of(st, head_exe)
    out: Dict[str, object] = {"base": base_exe, "head": head_exe, "new_failures": [], "fixed": [], "still_failing": [],
                              "appeared": [], "disappeared": [], "flaky": [], "duration_changes": [],
                              "comparable": True, "comparable_reason": None}
    missing = [name for name, tests in (("base", base), ("head", head)) if not tests]
    if missing:
        out["comparable"] = False
        out["comparable_reason"] = "no structured per-test results for the %s run" % " and ".join(missing)
        return out
    buckets: Dict[Tuple[str, str], str] = {("passed", "failed"): "new_failures", ("failed", "passed"): "fixed",
                                           ("failed", "failed"): "still_failing"}
    lists: Dict[str, List[str]] = {key: [] for key in ("new_failures", "fixed", "still_failing", "appeared", "disappeared", "flaky")}
    durations: List[dict] = []
    for test_id in sorted(set(base) | set(head)):
        before, after = base.get(test_id), head.get(test_id)
        if before is None:
            lists["appeared"].append(test_id)
        elif after is None:
            lists["disappeared"].append(test_id)
        else:
            bucket = buckets.get((before["status"], after["status"]))  # not_run, blocked, inconclusive conclude nothing
            if bucket is not None:
                lists[bucket].append(test_id)
            change = _slower(test_id, before["duration_ms"], after["duration_ms"])
            if change is not None:
                durations.append(change)
        if (before or {}).get("flaky") or (after or {}).get("flaky"):
            lists["flaky"].append(test_id)
    out.update(lists)
    out["duration_changes"] = durations
    return out
