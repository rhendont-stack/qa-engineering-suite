"""k6 summaries (the object k6 passes to ``handleSummary()``): thresholds are the run's tests, metrics are measured."""
from __future__ import annotations

import json
import math
from datetime import timedelta
from pathlib import Path

import pytest

from qa import authz, execution, ids, ingest, schema, state, status

CORPUS = Path(__file__).resolve().parents[1] / "corpus" / "k6" / "k6-2.3.0-thresholds.json"


def summary(metrics=None, duration_ms=1500.0, **over):
    """A k6 summary in the shape k6 2.x writes, with ``metrics`` replacing the default two."""
    doc = {
        "options": {"summaryTrendStats": ["avg", "min", "med", "max", "p(90)", "p(95)"]},
        "state": {"isStdOutTTY": False, "isStdErrTTY": False, "testRunDurationMs": duration_ms},
        "metrics": metrics if metrics is not None else {
            "vus_max": {"type": "gauge", "contains": "default", "values": {"value": 2, "min": 2, "max": 2}},
            "latency_ms": {"type": "trend", "contains": "time", "values": {"avg": 50, "p(95)": 50},
                           "thresholds": {"p(95)<100": {"ok": True}}},
        },
        "root_group": {"name": "", "path": "", "id": "d41d8cd98f00b204e9800998ecf8427e", "groups": [], "checks": []},
    }
    doc.update(over)
    return doc


def parse(doc):
    return ingest.parse_bytes("k6", json.dumps(doc).encode("utf-8") if not isinstance(doc, bytes) else doc)


def test_k6_corpus_counts_match_its_summary():
    raw = json.loads(CORPUS.read_text(encoding="utf-8"))
    s = ingest.parse_report("k6", CORPUS, 10**6)
    assert (s.format, s.kind, s.runs, s.anomalies) == ("k6", "tests", 1, ())
    assert (s.total, s.passed, s.failed, s.errors, s.skipped) == (2, 1, 1, 0, 0)
    by_id = {c.test_id: c for c in s.cases}
    passed, crossed = by_id["k6::qa_sample_ms::p(95)<100"], by_id["k6::qa_sample_ms::avg<10"]
    assert (passed.status, passed.classname, passed.name, passed.failure_kind) == ("passed", "qa_sample_ms", "p(95)<100", None)
    assert (crossed.status, crossed.failure_kind) == ("failed", "assertion")
    metrics = {m.name: m for m in s.metrics}
    assert set(metrics) == set(raw["metrics"])
    assert (metrics["qa_sample_ms"].type, metrics["qa_sample_ms"].values["avg"]) == ("trend", 50)
    assert s.run_duration_ms == math.ceil(raw["state"]["testRunDurationMs"])
    assert ingest.FORMAT_KINDS["k6"] == "tests" and not ingest.is_measurement("k6")


def test_k6_cases_come_in_a_stable_order_and_name_their_metric():
    s = parse(summary({"b": {"type": "rate", "values": {"rate": 1}, "thresholds": {"rate>0.5": {"ok": True}, "rate<2": {"ok": True}}},
                       "a": {"type": "counter", "values": {"count": 3, "rate": 0.5}, "thresholds": {"count>1": {"ok": False}}}}))
    assert [c.test_id for c in s.cases] == ["k6::a::count>1", "k6::b::rate<2", "k6::b::rate>0.5"]
    assert [m.name for m in s.metrics] == ["a", "b"]


HOSTILE = {
    "nan_literal": (b'{"metrics": {"m": {"type": "trend", "values": {"avg": NaN}}}}', "invalid_json"),
    "infinity_literal": (b'{"metrics": {"m": {"type": "trend", "values": {"avg": Infinity}}}}', "invalid_json"),
    "overflow": (b'{"metrics": {"m": {"type": "trend", "values": {"avg": 1e999}}}}', "invalid_json"),
    "nan_as_text": (summary({"m": {"type": "trend", "values": {"avg": "NaN"}}}), "invalid_metric_value"),
    "a_boolean_value": (summary({"m": {"type": "rate", "values": {"rate": True}}}), "invalid_metric_value"),
    "a_null_value": (summary({"m": {"type": "rate", "values": {"rate": None}}}), "invalid_metric_value"),
    "no_values": (summary({"m": {"type": "trend"}}), "invalid_metric_value"),
    "values_not_an_object": (summary({"m": {"type": "trend", "values": [50]}}), "invalid_metric_value"),
    "unknown_type": (summary({"m": {"type": "histogram", "values": {"avg": 1}}}), "invalid_metric_value"),
    "metric_not_an_object": (summary({"m": 5}), "invalid_metric_value"),
    "duration_as_text": (summary(duration_ms="1500"), "invalid_metric_value"),
    "integer_past_float_range": (b'{"metrics": {"m": {"type": "counter", "values": {"count": 1' + b"0" * 400 + b'}}}}',
                                 "invalid_metric_value"),
    "duration_past_float_range": (b'{"state": {"testRunDurationMs": 1' + b"0" * 400 + b'}, "metrics": {}}',
                                  "invalid_metric_value"),
    "ok_as_text": (summary({"m": {"type": "trend", "values": {"avg": 1}, "thresholds": {"avg<2": {"ok": "true"}}}}), "invalid_threshold"),
    "ok_missing": (summary({"m": {"type": "trend", "values": {"avg": 1}, "thresholds": {"avg<2": {}}}}), "invalid_threshold"),
    "thresholds_a_list": (summary({"m": {"type": "trend", "values": {"avg": 1}, "thresholds": ["avg<2"]}}), "invalid_threshold"),
    "metric_name_past_the_cap": (summary({"m" * 201: {"type": "trend", "values": {"avg": 1}}}), "invalid_name"),
    "expression_past_the_cap": (summary({"m": {"type": "trend", "values": {"avg": 1}, "thresholds": {"a" * 201: {"ok": True}}}}),
                                "invalid_name"),
    "stat_past_the_cap": (summary({"m": {"type": "trend", "values": {"p" * 201: 1}}}), "invalid_name"),
    "no_metrics": ({"state": {"testRunDurationMs": 1}}, "not_k6_summary"),
    "metrics_a_list": ({"metrics": []}, "not_k6_summary"),
    "not_an_object": ([1, 2], "not_k6_summary"),
}


@pytest.mark.parametrize("hostile", HOSTILE.values(), ids=HOSTILE)
def test_k6_parser_refuses_hostile_input(hostile):
    doc, detail = hostile
    with pytest.raises(ingest.ReportInvalid) as refused:
        parse(doc)
    assert refused.value.details["detail"] == detail


def test_names_at_the_cap_are_kept():
    s = parse(summary({"m" * 200: {"type": "trend", "values": {"p" * 200: 1}, "thresholds": {"a" * 200: {"ok": True}}}}))
    assert s.cases[0].test_id == "k6::%s::%s" % ("m" * 200, "a" * 200)


# --------------------------------------------------------------------------- runs that declare a k6 report

def k6_run(ws, actor, tmp_path, doc, exit_code=0, stub_name="k6", **kw):
    """Run a stand-in for k6 on the host: it writes ``doc`` where the run's report is declared and exits ``exit_code``."""
    source = tmp_path / "summary.json"
    source.write_text(json.dumps(doc))
    program = tmp_path / stub_name
    program.write_text("#!/bin/sh\ncp '%s' \"$2\"\nexit %d\n" % (source, exit_code))
    program.chmod(0o755)
    request = execution.RunRequest(command=[str(program), "run", "{evidence}/k6-summary.json"],
                                   reports=(("k6", "{evidence}/k6-summary.json"),), **kw)
    result = execution.execute(ws, actor, request)
    return result, state.load(ws)["executions"][result["execution"]]


def load_test_grant(ws, actor, max_vus=10, max_rps=100, max_duration_s=60):
    return authz.grant(ws, actor, environment_id="local", targets=[], activities=["load-test"],
                       limits={"load-test": {"max_vus": max_vus, "max_rps": max_rps, "max_duration_s": max_duration_s}},
                       approval="ok", expires_at=ids.now_utc() + timedelta(days=1))


def test_k6_summary_without_thresholds_is_no_tests_collected(trusted, actor, tmp_path):
    doc = summary({"iterations": {"type": "counter", "values": {"count": 5, "rate": 1.0}}})
    result, record = k6_run(trusted, actor, tmp_path, doc, stub_name="loadtool")
    assert (result["status"], result["reason"]) == ("inconclusive", "no_tests_collected")
    assert record["report"]["metrics"] == {"iterations": {"count": 5, "rate": 1.0}}


def test_k6_failed_threshold_with_exit_99_is_failed_without_mismatch(trusted, actor, tmp_path):
    doc = json.loads(CORPUS.read_text(encoding="utf-8"))
    result, record = k6_run(trusted, actor, tmp_path, doc, exit_code=99, stub_name="loadtool")
    assert (result["status"], result["reason"], result["strength"], result["anomalies"]) == ("failed", "assertion", "structured", [])
    assert result["tests"] == {"total": 2, "passed": 1, "failed": 1, "errors": 0, "skipped": 0}
    report = record["report"]
    assert (report["format"], report["kind"], report["tests"], report["failures"], report["label"]) == ("k6", "tests", 2, 1, "measured")
    assert report["metrics"]["qa_sample_ms"] == doc["metrics"]["qa_sample_ms"]["values"]
    assert record["reports"] == [{"format": "k6", "pattern": "{evidence}/k6-summary.json", "files": ["reports/k6-summary.json"], "error": None}]
    schema.validate({key: value for key, value in record.items() if key != "ingested"}, "events/execution.recorded.schema.json")


@pytest.mark.parametrize("hostile", ["integer_past_float_range", "duration_past_float_range"])
def test_a_run_whose_summary_holds_an_integer_past_float_range_is_still_recorded(trusted, actor, tmp_path, hostile):
    """The run has already executed when its report is read, so a refusal must reach the record, not escape it."""
    source = tmp_path / "summary.json"
    source.write_bytes(HOSTILE[hostile][0])
    program = tmp_path / "loadtool"
    program.write_text("#!/bin/sh\ncp '%s' \"$2\"\n" % source)
    program.chmod(0o755)
    request = execution.RunRequest(command=[str(program), "run", "{evidence}/k6-summary.json"],
                                   reports=(("k6", "{evidence}/k6-summary.json"),))
    result = execution.execute(trusted, actor, request)
    record = state.load(trusted)["executions"][result["execution"]]
    assert record["report"] is None
    assert any(text.startswith("report_invalid: ") and "(invalid_metric_value)" in text for text in record["limitations"])


def test_a_k6_summary_past_the_byte_cap_is_refused(tmp_path):
    path = tmp_path / "k6-summary.json"
    path.write_text(json.dumps(summary()))
    cap = path.stat().st_size - 1
    with pytest.raises(ingest.ReportInvalid) as refused:
        ingest.parse_report("k6", path, cap)
    assert refused.value.details["detail"] == "too_large"
    assert ingest.parse_report("k6", path, cap + 1).total == 1


EXCEEDED = {
    "vus": ({"vus_max": {"type": "gauge", "values": {"value": 12, "min": 12, "max": 12}}}, 1000.0, "vus_max.max 12 > max_vus 10"),
    "rps": ({"http_reqs": {"type": "counter", "values": {"count": 6060, "rate": 101.0}}}, 1000.0, "http_reqs.rate 101.0 > max_rps 100"),
    "duration": ({"vus_max": {"type": "gauge", "values": {"value": 1, "min": 1, "max": 1}}}, 60000.5,
                 "run_duration_ms / 1000 60.001 > max_duration_s 60"),
}


@pytest.mark.parametrize("metrics, duration_ms, passed", EXCEEDED.values(), ids=EXCEEDED)
def test_load_limit_exceeded_is_an_anomaly(trusted, actor, tmp_path, metrics, duration_ms, passed):
    grant = load_test_grant(trusted, actor)
    passing = dict(metrics, latency_ms={"type": "trend", "values": {"avg": 5}, "thresholds": {"avg<10": {"ok": True}}})
    result, record = k6_run(trusted, actor, tmp_path, summary(passing, duration_ms=duration_ms))
    assert {"activity": "load-test", "environment": "local", "authorization": grant["data"]["id"]} in record["authorizations"]
    assert (result["status"], result["anomalies"]) == ("passed", ["load_limit_exceeded"])
    assert record["anomalies"] == ["load_limit_exceeded"]
    assert [text for text in record["limitations"] if text.startswith("load_limit_exceeded: ")] == [
        "load_limit_exceeded: %s of %s" % (passed, grant["data"]["id"])]
    schema.validate({key: value for key, value in record.items() if key != "ingested"}, "events/execution.recorded.schema.json")


def test_a_load_test_within_its_grant_has_no_anomaly(trusted, actor, tmp_path):
    load_test_grant(trusted, actor)
    metrics = {"vus_max": {"type": "gauge", "values": {"value": 10, "min": 10, "max": 10}},
               "http_reqs": {"type": "counter", "values": {"count": 6000, "rate": 100.0}},
               "latency_ms": {"type": "trend", "values": {"avg": 5}, "thresholds": {"avg<10": {"ok": True}}}}
    result, _record = k6_run(trusted, actor, tmp_path, summary(metrics, duration_ms=60000.0))
    assert (result["status"], result["anomalies"]) == ("passed", [])


def test_load_limit_exceeded_is_ordered_among_the_anomalies():
    assert "load_limit_exceeded" in status.ANOMALY_ORDER
    enum = schema.load_schema("defs.schema.json")["$defs"]["anomaly"]["enum"]
    assert list(status.ANOMALY_ORDER) == enum


def test_cli_runs_and_ingests_k6_summaries(trusted, actor, tmp_path, qa_cli):
    source = tmp_path / "summary.json"
    source.write_text(CORPUS.read_text(encoding="utf-8"))
    program = tmp_path / "loadtool"
    program.write_text("#!/bin/sh\ncp '%s' \"$2\"\nexit 99\n" % source)
    program.chmod(0o755)
    code, ran = qa_cli("run", "--report", "k6:{evidence}/k6-summary.json", "--", str(program), "run", "{evidence}/k6-summary.json")
    assert (code, ran["status"], ran["reason"], ran["tests"]["total"]) == (1, "failed", "assertion", 2)
    code, attached = qa_cli("ingest", "k6", str(CORPUS), "--from", ran["execution"])
    # The same bytes the run stored under its manifest: attaching them again is structured evidence.
    assert (code, attached["kind"], attached["total"], attached["strength"]) == (0, "tests", 2, "structured")
