"""Lighthouse results: category scores and the lab metrics, recorded as measured numbers that never decide a status."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from qa import execution, ingest, schema, state

CORPUS = Path(__file__).resolve().parents[1] / "corpus" / "lighthouse" / "lighthouse-13.6.0-fixture-page.json"
MANIFEST = json.loads((Path(__file__).resolve().parents[1] / "corpus" / "MANIFEST.json").read_text(encoding="utf-8"))

LAB = {"first-contentful-paint": (1200.5, "millisecond"), "largest-contentful-paint": (1800.0, "millisecond"),
       "total-blocking-time": (0, "millisecond"), "cumulative-layout-shift": (0.01, "unitless"),
       "speed-index": (1300, "millisecond"), "interactive": (1900, "millisecond")}


def lhr(scores=None, audits=None, **over):
    doc = {"lighthouseVersion": "13.6.0", "requestedUrl": "http://127.0.0.1:8000/", "finalDisplayedUrl": "http://127.0.0.1:8000/",
           "categories": {name: {"id": name, "title": name, "score": score} for name, score in (scores or {"performance": 0.98}).items()},
           "audits": audits if audits is not None else {name: {"id": name, "score": 1, "numericValue": value, "numericUnit": unit}
                                                        for name, (value, unit) in LAB.items()}}
    doc.update(over)
    return doc


def parse(doc):
    return ingest.parse_bytes("lighthouse", json.dumps(doc).encode("utf-8") if not isinstance(doc, bytes) else doc)


def test_lighthouse_corpus_matches_the_manifest():
    entry = next(e for e in MANIFEST if e["file"] == "lighthouse/lighthouse-13.6.0-fixture-page.json")
    s = ingest.parse_report("lighthouse", CORPUS, 50 * 2 ** 20)
    assert (s.format, s.kind, s.anomalies, s.tool_version) == ("lighthouse", "metrics", (), entry["version"])
    assert s.tool_version == "13.6.0"
    names = {m.name for m in s.metrics}
    assert names == set(entry["expected"]["metrics"])
    assert len(s.metrics) == entry["expected"]["count"]
    assert ingest.FORMAT_KINDS["lighthouse"] == "metrics" and ingest.is_measurement("lighthouse")
    assert "metrics" in ingest.MEASUREMENT_KINDS and not s.cases and not s.alerts


def test_lighthouse_scores_and_lab_audits_become_metrics():
    s = parse(lhr({"performance": 0.5, "accessibility": 1, "seo": None}))
    by_name = {m.name: m for m in s.metrics}
    assert by_name["category:performance"] == ingest.Metric("category:performance", "score", {"score": 0.5})
    assert by_name["category:accessibility"].values == {"score": 1}
    assert "category:seo" not in by_name  # a null score is left out
    assert by_name["largest-contentful-paint"] == ingest.Metric("largest-contentful-paint", "lab:millisecond", {"value": 1800.0})
    assert by_name["cumulative-layout-shift"].type == "lab:unitless"
    assert len(s.metrics) == 2 + len(LAB)


def test_lighthouse_audits_that_are_not_lab_metrics_are_left_out():
    audits = {"first-contentful-paint": {"numericValue": 10, "numericUnit": "millisecond"},
              "server-response-time": {"numericValue": 5, "numericUnit": "millisecond"},
              "interactive": {"scoreDisplayMode": "error", "errorMessage": "x"}}
    assert [m.name for m in parse(lhr(audits=audits)).metrics] == ["category:performance", "first-contentful-paint"]


HOSTILE = {
    "not_lighthouse": ({"categories": {}, "audits": {}}, "not_lighthouse_result"),
    "version_not_text": (lhr(lighthouseVersion=13), "not_lighthouse_result"),
    "no_categories": ({"lighthouseVersion": "13.6.0", "audits": {}}, "not_lighthouse_result"),
    "audits_a_list": (lhr(audits=[]), "not_lighthouse_result"),
    "not_an_object": ([1], "not_lighthouse_result"),
    "runtime_error": (lhr(runtimeError={"code": "NO_FCP", "message": "The page did not paint any content."}), "lighthouse_runtime_error"),
    "runtime_error_any_code": (lhr(runtimeError={"code": "PROTOCOL_TIMEOUT"}), "lighthouse_runtime_error"),
    "score_above_one": (lhr({"performance": 1.2}), "invalid_metric_value"),
    "score_below_zero": (lhr({"performance": -0.1}), "invalid_metric_value"),
    "score_text": (lhr({"performance": "0.9"}), "invalid_metric_value"),
    "score_boolean": (lhr({"performance": True}), "invalid_metric_value"),
    "value_text": (lhr(audits={"interactive": {"numericValue": "5", "numericUnit": "millisecond"}}), "invalid_metric_value"),
    "value_past_float_range": (b'{"lighthouseVersion": "1", "categories": {}, "audits": {"interactive": {"numericValue": 1'
                               + b"0" * 400 + b', "numericUnit": "millisecond"}}}', "invalid_metric_value"),
    "value_without_unit": (lhr(audits={"interactive": {"numericValue": 5}}), "invalid_metric_value"),
    "audit_not_an_object": (lhr(audits={"interactive": 5}), "invalid_metric_value"),
    "nan_literal": (b'{"lighthouseVersion": "1", "categories": {}, "audits": {"interactive": {"numericValue": NaN, "numericUnit": "x"}}}',
                    "invalid_json"),
}


@pytest.mark.parametrize("hostile", HOSTILE.values(), ids=HOSTILE)
def test_lighthouse_parser_refuses_hostile_input(hostile):
    doc, detail = hostile
    with pytest.raises(ingest.ReportInvalid) as refused:
        parse(doc)
    assert refused.value.details["detail"] == detail


def test_lighthouse_runtime_error_is_report_invalid():
    with pytest.raises(ingest.ReportInvalid) as refused:
        parse(lhr(runtimeError={"code": "NO_FCP"}))
    assert (refused.value.code, refused.value.details["detail"]) == ("report_invalid", "lighthouse_runtime_error")


@pytest.mark.parametrize("score", [1.0000001, -0.0000001, 2, -1])
def test_lighthouse_scores_outside_unit_interval_are_refused(score):
    with pytest.raises(ingest.ReportInvalid):
        parse(lhr({"performance": score}))


@pytest.mark.parametrize("score", [0, 1, 0.0, 1.0])
def test_lighthouse_scores_at_the_interval_ends_are_kept(score):
    assert parse(lhr({"performance": score})).metrics[0].values == {"score": score}


# --------------------------------------------------------------------------- runs and ingest

def lighthouse_run(ws, actor, tmp_path, doc, exit_code=0, declare=True):
    source = tmp_path / "lighthouse.json"
    source.write_text(json.dumps(doc))
    program = tmp_path / "lhtool"
    program.write_text("#!/bin/sh\n%s\nexit %d\n" % ("cp '%s' \"$1\"" % source if declare else "true", exit_code))
    program.chmod(0o755)
    request = execution.RunRequest(command=[str(program), "{evidence}/lighthouse.json"], category="performance",
                                   reports=(("lighthouse", "{evidence}/lighthouse.json"),) if declare else ())
    ran = execution.execute(ws, actor, request)
    return ran, state.load(ws)["executions"][ran["execution"]]


def test_lighthouse_metrics_never_decide_status(trusted, actor, tmp_path):
    ran, record = lighthouse_run(trusted, actor, tmp_path, lhr({"performance": 0.01}))
    bare, bare_record = lighthouse_run(trusted, actor, tmp_path, lhr(), declare=False)
    assert (ran["status"], ran["reason"], ran["anomalies"]) == ("passed", None, [])  # a failing score is still a pass by exit code
    assert ran["strength"] == bare["strength"] and ran["tests"] == bare["tests"]
    assert record["report"] is None and len(record["measurements"]) == 1
    (item,) = record["measurements"]
    assert (item["format"], item["kind"], item["label"], item["tool_version"], item["totals"]) == (
        "lighthouse", "metrics", "measured", "13.6.0", {"metrics": 1 + len(LAB)})
    assert item["metrics"]["category:performance"] == {"score": 0.01}
    assert item["metrics"]["interactive"] == {"value": 1900}
    schema.validate({key: value for key, value in record.items() if key != "ingested"}, "events/execution.recorded.schema.json")
    assert bare_record["status"] == record["status"]


def test_a_failed_run_stays_failed_with_lighthouse_metrics(trusted, actor, tmp_path):
    ran, record = lighthouse_run(trusted, actor, tmp_path, lhr({"performance": 1.0}), exit_code=1)
    assert (ran["status"], ran["reason"]) == ("failed", "nonzero_exit") and record["measurements"][0]["metrics"]


def test_a_lighthouse_runtime_error_is_noted_and_not_recorded(trusted, actor, tmp_path):
    """The only report the run declared is invalid, which a run reads as it reads an invalid coverage file: nothing measured."""
    ran, record = lighthouse_run(trusted, actor, tmp_path, lhr(runtimeError={"code": "NO_FCP"}))
    assert (ran["status"], ran["reason"]) == ("inconclusive", "report_invalid") and "measurements" not in record
    assert any("report_invalid" in text and "lighthouse_runtime_error" in text for text in record["limitations"])


def test_cli_ingests_lighthouse_metrics_without_changing_the_status(trusted, actor, tmp_path, qa_cli):
    program = tmp_path / "tool"
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(0o755)
    code, ran = qa_cli("run", "--", str(program))
    assert code == 0
    code, attached = qa_cli("ingest", "lighthouse", str(CORPUS), "--from", ran["execution"])
    assert code == 0 and (attached["kind"], attached["total"]) == ("metrics", len(parse(CORPUS.read_bytes()).metrics))
    folded = state.load(trusted)["executions"][ran["execution"]]
    assert (folded["status"], folded["reason"]) == ("passed", None)
    (item,) = folded["ingested"]
    assert item["report"]["label"] == "measured" and item["report"]["tool_version"] == "13.6.0"
    assert item["totals"] == {"metrics": item["total"]}
    schema.validate({key: value for key, value in item.items() if key != "event"}, "events/report.ingested.schema.json")


# --------------------------------------------------------------------------- the corpus file carries redactions

MARKER = "[REDACTED:password_assignment:00000000]"


def test_the_corpus_file_holds_the_manifest_s_redactions_and_still_yields_its_metrics():
    """The stored bytes are what a run's evidence holds: 13 values in fields the parser does not read were replaced by the
    redactor, and the parser reads the file all the same."""
    entry = next(e for e in MANIFEST if e["file"] == "lighthouse/lighthouse-13.6.0-fixture-page.json")
    text = CORPUS.read_text(encoding="utf-8")
    assert text.count("[REDACTED:password_assignment:") == 13
    assert "13 values" in entry["notes"]
    assert all(path in entry["notes"] for path in ("configSettings/auditMode", "i18n/rendererFormattedStrings/passedAuditsGroupTitle"))
    s = ingest.parse_report("lighthouse", CORPUS, 50 * 2 ** 20)
    assert [m.name for m in s.metrics] == entry["expected"]["metrics"]
    assert all(isinstance(v, (int, float)) for m in s.metrics for v in m.values.values())


def test_a_redaction_marker_in_a_field_the_parser_does_not_read_is_no_refusal():
    doc = lhr()
    doc["configSettings"] = {"auditMode": MARKER}
    doc["i18n"] = {"rendererFormattedStrings": {"passedAuditsGroupTitle": MARKER}}
    doc["audits"]["interactive"]["details"] = {"items": [{"lcpInvalidated": MARKER}]}
    assert len(parse(doc).metrics) == 1 + len(LAB)


@pytest.mark.parametrize("where", ["numericValue", "score"])
def test_a_redaction_marker_in_a_field_the_parser_reads_is_refused_not_read_as_a_number(where):
    doc = lhr()
    if where == "numericValue":
        doc["audits"]["interactive"]["numericValue"] = MARKER
    else:
        doc["categories"]["performance"]["score"] = MARKER
    with pytest.raises(ingest.ReportInvalid) as refused:
        parse(doc)
    assert refused.value.details["detail"] == "invalid_metric_value"
