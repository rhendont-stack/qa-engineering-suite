from __future__ import annotations

import copy

import pytest

from qa import compare, state
from qa.errors import QaError

TS = "2026-11-09T12:00:00.000Z"
EXE_A, EXE_B, EXE_EXIT = ("EXE-" + "0" * 25 + c for c in "ABE")


def _execution(exe_id):
    """Complete, schema-valid execution.recorded data: the shape the kernel records."""
    return {
        "id": exe_id, "command": {"argv": ["t"], "shell": False, "cwd": "."},
        "started_at": TS, "ended_at": TS, "duration_ms": 0, "exit_code": 0, "signal": None, "timed_out": False,
        "git": None,
        "environment": {"class": "local", "platform": "p", "python": "3", "implementation": "CPython",
                        "cpu_count": 8, "memory_bytes": None, "tools": {}},
        "environment_id": "local", "env_names": [], "env_values": {}, "isolation": "host",
        "evidence_manifest": None, "reports": [], "report": None,
        "status": "passed", "reason": None, "strength": "exit_code", "anomalies": [], "classification": "deterministic",
        "authorization": "trusted:local", "authorizations": [], "activity": "test-execution-local",
        "scope": "targeted", "category": "functional", "attempt_of": None,
        "config_hash": "sha256:" + "0" * 64, "kernel": {"version": "0.1.0-dev", "manifest": None},
        "actor": {"role": "verifier", "skill": None, "session": "S-" + "0" * 26},
        "independence": "I1", "redactions": 0, "limitations": [],
    }


def _result(number, exe_id, test_id, status, duration_ms=None, flaky=False):
    return {"id": "RES-%026d" % number, "execution": exe_id, "test_id": test_id, "status": status,
            "failure_kind": "assertion" if status == "failed" else None, "skip_reason": None, "flaky": flaky,
            "duration_ms": duration_ms}


BASE = [  # test, status, duration_ms
    ("t_old", "failed", 100), ("t_new", "passed", 100), ("t_fixed", "failed", 100), ("t_ok", "passed", 100),
    ("t_gone", "passed", 100), ("t_unrun", "failed", 100), ("t_flaky", "passed", 100),
    ("t_slow", "passed", 1000), ("t_ratio_only", "passed", 100), ("t_delta_only", "passed", 20000),
]
HEAD = [
    ("t_old", "failed", 100), ("t_new", "failed", 100), ("t_fixed", "passed", 100), ("t_ok", "passed", 100),
    ("t_appeared", "passed", 100), ("t_unrun", "not_run", None), ("t_flaky", "passed", 100),
    ("t_slow", "passed", 2500), ("t_ratio_only", "passed", 900), ("t_delta_only", "passed", 22000),
]


@pytest.fixture
def st_two_runs():
    events = [("execution.recorded", _execution(exe)) for exe in (EXE_A, EXE_B, EXE_EXIT)]
    number = 0
    for exe, rows in ((EXE_A, BASE), (EXE_B, HEAD)):
        for test_id, status, duration in rows:
            number += 1
            events.append(("result.recorded", _result(number, exe, test_id, status, duration, flaky=(test_id == "t_flaky" and exe == EXE_B))))
    return state.fold([{"id": "EVT-%026d" % (i + 1), "type": t, "data": d} for i, (t, d) in enumerate(events)])


def test_classifies_each_test_once(st_two_runs):
    d = compare.compare(st_two_runs, EXE_A, EXE_B)
    assert d["comparable"] is True and d["comparable_reason"] is None
    assert d["new_failures"] == ["t_new"] and d["fixed"] == ["t_fixed"] and d["still_failing"] == ["t_old"]
    assert d["appeared"] == ["t_appeared"] and d["disappeared"] == ["t_gone"]
    listed = d["new_failures"] + d["fixed"] + d["still_failing"] + d["appeared"] + d["disappeared"]
    assert len(listed) == len(set(listed))


def test_not_run_is_never_a_fix(st_two_runs):
    d = compare.compare(st_two_runs, EXE_A, EXE_B)  # t_unrun failed, then not_run
    assert "t_unrun" not in d["fixed"] + d["still_failing"] + d["new_failures"]


def test_flaky_listed_even_when_head_passes(st_two_runs):
    d = compare.compare(st_two_runs, EXE_A, EXE_B)
    assert d["flaky"] == ["t_flaky"] and "t_flaky" not in d["new_failures"] + d["fixed"] + d["still_failing"]


def test_duration_change_needs_both_thresholds(st_two_runs):
    d = compare.compare(st_two_runs, EXE_A, EXE_B)
    # t_slow: x2.5 and +1500 ms. t_ratio_only: x9 but +800 ms. t_delta_only: +2000 ms but x1.1.
    assert d["duration_changes"] == [{"test_id": "t_slow", "base_ms": 1000, "head_ms": 2500, "ratio": 2.5, "delta_ms": 1500}]


def test_run_without_structured_results_is_not_comparable(st_two_runs):
    for pair in ((EXE_A, EXE_EXIT), (EXE_EXIT, EXE_B)):
        d = compare.compare(st_two_runs, *pair)
        assert d["comparable"] is False and d["comparable_reason"]
        assert all(d[key] == [] for key in ("new_failures", "fixed", "still_failing", "appeared", "disappeared", "flaky", "duration_changes"))


def test_unknown_id_is_a_structured_refusal(st_two_runs):
    with pytest.raises(QaError) as e:
        compare.compare(st_two_runs, EXE_A, "EXE-" + "0" * 25 + "Z")
    assert e.value.code == "unknown_execution"


def test_compare_does_not_change_the_state(st_two_runs):
    before = state.state_hash(st_two_runs)
    compare.compare(st_two_runs, EXE_A, EXE_B)
    assert state.state_hash(st_two_runs) == before


EXE_DOWN = "EXE-" + "0" * 25 + "D"


@pytest.fixture
def st_downgraded(st_two_runs):
    """EXE_DOWN: per-test results all passed, but the run itself was downgraded to inconclusive."""
    st = copy.deepcopy(st_two_runs)
    st["executions"][EXE_DOWN] = dict(st["executions"][EXE_A], id=EXE_DOWN, status="inconclusive", reason="network_not_isolated")
    st["results"]["RES-%026d" % 900] = _result(900, EXE_DOWN, "t_old", "passed")
    st["results"]["RES-%026d" % 901] = _result(901, EXE_DOWN, "t_fixed", "passed")
    return st


def test_a_downgraded_run_is_not_comparable(st_downgraded):
    d = compare.compare(st_downgraded, EXE_A, EXE_DOWN)
    assert d["comparable"] is False and d["comparable_reason"] == "the head run is inconclusive (network_not_isolated)"
    assert all(d[key] == [] for key in ("new_failures", "fixed", "still_failing", "appeared", "disappeared", "flaky", "duration_changes"))
    d = compare.compare(st_downgraded, EXE_DOWN, EXE_B)
    assert d["comparable"] is False and d["comparable_reason"].startswith("the base run is inconclusive")


@pytest.mark.parametrize("status", ["blocked", "not_run", "inconclusive"])
def test_only_passed_or_failed_runs_are_comparable(st_downgraded, status):
    st_downgraded["executions"][EXE_B]["status"] = status
    d = compare.compare(st_downgraded, EXE_A, EXE_B)
    assert d["comparable"] is False and status in d["comparable_reason"] and d["new_failures"] == []


def test_a_blocked_bundle_run_is_not_comparable(st_two_runs):
    remote = "EXE-" + "0" * 25 + "R"
    tests = {"t_old": {"status": "passed", "flaky": False, "duration_ms": 1}}
    st = copy.deepcopy(st_two_runs)
    st["bundles"]["BUNDLE-001"] = {"executions": {remote: {"status": "blocked", "reason": "sandbox_unavailable", "tests": tests}}}
    d = compare.compare(st, EXE_A, "BUNDLE-001:" + remote)
    assert d["comparable"] is False and d["comparable_reason"] == "the head run is blocked (sandbox_unavailable)" and d["fixed"] == []
    st["bundles"]["BUNDLE-001"]["executions"][remote]["status"] = "passed"
    assert compare.compare(st, EXE_A, "BUNDLE-001:" + remote)["comparable"] is True
