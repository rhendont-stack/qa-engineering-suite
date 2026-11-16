"""Tests for benchmark/harness/metrics.py on synthetic grades and end lines."""
from __future__ import annotations

import importlib.util

import pytest

from helpers import REPO

METRICS = REPO / "benchmark" / "harness" / "metrics.py"


def metrics_module():
    spec = importlib.util.spec_from_file_location("metrics_under_test", str(METRICS))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def end(run_id, arm, cost, tokens=(10, 4, 2, 1), duration=1000, scenario="f9-honesty", engine="claude -p"):
    return {"event": "end", "run_id": run_id, "case_id": scenario, "config": arm, "engine": engine, "cost_usd": cost,
            "cost_unknown": cost is None, "duration_ms": duration,
            "tokens": dict(zip(("input", "output", "cache_read", "cache_creation"), tokens))}


def grade(run_id, arm, unbacked, claims, violations, correct, hard, judge_cost=0.01, scenario="f9-honesty"):
    return {"run_id": run_id, "arm": arm, "scenario": scenario,
            "metrics": {"fabrication": {"claims": claims, "unbacked": unbacked, "rate": unbacked / claims if claims else None,
                                        "uncited_claim": None},
                        "safety": {"violations": [], "count": violations},
                        "honest_status": {"scenarios": [], "correct": correct, "total": 3, "rate": correct / 3}},
            "judge": {"model": "j", "cost_usd": judge_cost, "cost_unknown": judge_cost is None, "raw_path": None},
            "hard": hard}


PASS = {"fabrication": "pass", "safety": "pass", "honest_status": "pass"}


def test_metrics_mean_stdev_and_hard_holds():
    m = metrics_module()
    runs = [end("w1", "with-suite", 0.50, (10, 4, 2, 0), 1000), end("b1", "baseline", 0.20),
            end("w2", "with-suite", 0.30, (20, 6, 4, 2), 3000), end("b2", "baseline", 0.10),
            end("w3", "with-suite", 0.40, (30, 8, 0, 4), 2000),
            end("judge1", "judge", 0.02, engine="judge")]                   # a judge line is no run of an arm
    grades = [grade("w1", "with-suite", 0, 4, 0, 3, PASS), grade("w2", "with-suite", 2, 4, 1, 2, dict(PASS, safety="fail")),
              grade("w3", "with-suite", 1, 2, 0, 3, PASS), grade("b1", "baseline", 0, 1, 0, 3, PASS, judge_cost=None)]
    done = m.aggregate(grades, runs)
    groups = {(x["scenario"], x["arm"]): x for x in done["groups"]}
    assert set(groups) == {("f9-honesty", "with-suite"), ("f9-honesty", "baseline")}
    w = groups[("f9-honesty", "with-suite")]
    assert (w["runs"], w["graded"], w["run_ids"]) == (3, 3, ["w1", "w2", "w3"])
    assert w["cost"]["sum"] == pytest.approx(1.2) and w["cost"]["mean"] == pytest.approx(0.4)
    assert (w["cost"]["min"], w["cost"]["max"], w["cost"]["unknown"]) == (0.3, 0.5, 0)
    assert w["tokens"] == {"input": 20, "output": 6, "cache_read": 2, "cache_creation": 2}
    assert w["duration_ms"] == {"mean": 2000, "max": 3000}
    assert w["judge_cost"] == {"sum": pytest.approx(0.03), "unknown": 0}
    unbacked = w["metrics"]["fabrication.unbacked"]
    assert unbacked["values"] == [0, 2, 1] and unbacked["mean"] == 1 and unbacked["stdev"] == pytest.approx(1.0)   # n - 1
    assert w["metrics"]["fabrication.rate"]["values"] == [0.0, 0.5, 0.5]
    assert w["metrics"]["fabrication.uncited_claim"]["values"] == [None, None, None]
    assert w["metrics"]["fabrication.uncited_claim"]["mean"] is None
    assert w["metrics"]["safety.count"]["mean"] == pytest.approx(1 / 3)
    assert w["metrics"]["honest_status.correct"]["stdev"] == pytest.approx(0.57735, abs=1e-5)
    assert w["hard"]["fabrication"] == {"holds": True, "pass": 3, "fail": 0, "ungraded": 0}
    assert w["hard"]["safety"] == {"holds": False, "pass": 2, "fail": 1, "ungraded": 0}
    b = groups[("f9-honesty", "baseline")]
    # One grade for two runs: a single value has no sample deviation, and a run with no grade means no hard metric holds.
    assert (b["runs"], b["graded"]) == (2, 1)
    assert b["metrics"]["fabrication.claims"] == {"values": [1], "n": 1, "mean": 1, "stdev": None}
    assert b["hard"]["fabrication"] == {"holds": False, "pass": 1, "fail": 0, "ungraded": 1}
    assert b["judge_cost"] == {"sum": 0, "unknown": 1}
    assert b["cost"]["sum"] == pytest.approx(0.3)
    # A metric that could not be computed has no values; its hard metric already failed in the grade.
    broken = dict(grade("w4", "with-suite", 0, 1, 0, 3, dict(PASS, fabrication="fail")))
    broken["metrics"] = dict(broken["metrics"], fabrication={"error": "judge_unparseable"})
    done = m.aggregate(grades + [broken], runs + [end("w4", "with-suite", None)])
    w = {(x["scenario"], x["arm"]): x for x in done["groups"]}[("f9-honesty", "with-suite")]
    assert w["metrics"]["fabrication.unbacked"]["values"] == [0, 2, 1, None] and w["metrics"]["fabrication.unbacked"]["n"] == 3
    assert w["metrics"]["fabrication.errors"] == {"values": [None, None, None, "judge_unparseable"]}
    assert w["cost"]["unknown"] == 1 and w["cost"]["sum"] == pytest.approx(1.2) and w["hard"]["fabrication"]["holds"] is False

    text = m.render_md(done)
    assert text.startswith("# Benchmark metrics")
    assert "## f9-honesty: with-suite" in text and "## f9-honesty: baseline" in text
    assert "| fabrication.unbacked | 3 | 1 | 1 |" in text
    assert "| safety | no | 3 | 1 | 0 |" in text and "| fabrication | no | 1 | 0 | 1 |" in text
    # Precision is a lower bound: an unmatched finding may still be real.
    audit = grade("a1", "baseline", 0, 1, 0, 0, {"fabrication": "pass", "safety": "pass"}, scenario="f2-audit")
    audit["metrics"] = {"precision": {"matched": 1, "detections": 2, "precision": 0.5, "lower_bound": True}}
    text = m.render_md(m.aggregate([audit], [end("a1", "baseline", 0.1, scenario="f2-audit")]))
    assert "| precision.precision (lower bound) | 1 | 0.5 | - |" in text
