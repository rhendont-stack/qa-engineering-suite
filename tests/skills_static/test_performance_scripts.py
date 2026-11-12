"""qa-performance's scripts: k6_script.py writes a k6 script only within the limits it is given, with no address in it and every
objective as a threshold that aborts the run; perf_table.py lists a recorded run's numbers with their labels and nothing for a
run that measured nothing. Hostile input ends in one JSON line and exit 2, never a traceback."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys

import pytest

from helpers import REPO

SKILL = REPO / "skills" / "qa-performance"
K6_SCRIPT = SKILL / "scripts" / "k6_script.py"
PERF_TABLE = SKILL / "scripts" / "perf_table.py"
TEMPLATE = SKILL / "assets" / "k6-template.js"
WORKLOAD_MD = SKILL / "references" / "workload-model.md"
GRADER = SKILL / "evals" / "grade.py"
_spec = importlib.util.spec_from_file_location("qa_performance_grade_for_script_tests", str(GRADER))
grade = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grade)


def leg_workload():
    blocks = re.findall(r"^```qa-workload (\S+)\n(.*?)\n```$", WORKLOAD_MD.read_text(encoding="utf-8"), re.M | re.S)
    assert [name for name, _ in blocks] == ["f2-orders-listing"]
    return json.loads(blocks[0][1])


def project(tmp_path):
    root = tmp_path / "project"
    (root / ".qa" / "work").mkdir(parents=True)
    return root


def script(root, *args):
    done = subprocess.run([sys.executable, "-I", "-B", str(K6_SCRIPT), *args], cwd=str(root), capture_output=True, text=True, timeout=60)
    assert done.stderr == "", done.stderr
    lines = done.stdout.strip().splitlines()
    assert len(lines) == 1, done.stdout
    return done.returncode, json.loads(lines[0])


def write_workload(root, doc, name="w.json", raw=None):
    folder = root / ".qa" / "work" / "performance"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(raw if raw is not None else json.dumps(doc))
    return ".qa/work/performance/" + name


LIMITS = ["--max-vus", "20", "--max-duration-s", "90", "--max-rps", "25"]


def objective_workload(**changes):
    doc = {"name": "orders-listing", "model": "closed", "vus": 20, "duration_s": 60, "think_time_s": 1,
           "requests": [{"name": "list orders", "method": "GET", "path": "/orders", "headers": {"Authorization": "Bearer user-a-token"},
                         "expect": 200}],
           "thresholds": {"http_req_duration": ["p(95)<200"], "http_req_failed": ["rate<0.01"]}}
    doc.update(changes)
    return doc


# --------------------------------------------------------------------------- k6_script.py

def test_the_template_holds_no_address_and_writes_the_summary_where_the_run_says():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "://" not in text and text.count("__QA_WORKLOAD__") == 1 and text.count("__QA_OPTIONS__") == 1
    assert sorted(set(re.findall(r"from '([^']+)'", text))) == ["k6", "k6/execution", "k6/http"]
    assert "__ENV.BASE_URL" in text and "out[__ENV.SUMMARY_PATH]" in text and "export function handleSummary" in text


def test_the_leg_workload_becomes_a_script_with_aborting_thresholds_and_no_address(tmp_path):
    root = project(tmp_path)
    path = write_workload(root, leg_workload(), "f2-orders-listing.json")
    code, out = script(root, "--workload", path, "--max-vus", "5", "--max-duration-s", "60", "--max-rps", "100",
                       "--out", ".qa/work/performance/f2-orders-listing.js")
    assert code == 0 and out["ok"] is True, out
    assert out["input"] == "/work/.qa-input/performance/f2-orders-listing.js" and out["workload"]["most_requests_per_s"] == 25.0
    text = (root / out["written"]).read_text(encoding="utf-8")
    assert "://" not in text and "__QA_" not in text
    options = json.loads(re.search(r"export const options = (\{.*?\n\});", text, re.S).group(1))
    assert options["scenarios"] == {"load": {"executor": "constant-vus", "vus": 5, "duration": "30s", "gracefulStop": "2s"}}
    assert options["thresholds"]["http_req_duration"] == [{"threshold": "p(95)<200", "abortOnFail": True, "delayAbortEval": "5s"}]
    assert options["summaryTrendStats"][-1] == "p(99)" and options["discardResponseBodies"] is True


def test_a_script_for_the_load_objective_meets_the_graders_reading(tmp_path):
    root = project(tmp_path)
    code, out = script(root, "--workload", write_workload(root, objective_workload()), *LIMITS, "--out", ".qa/work/performance/orders.js")
    assert code == 0, out
    assert grade.script_problems((root / out["written"]).read_text(encoding="utf-8")) == []
    open_model = objective_workload(model="open", rate_per_s=25, think_time_s=0)
    code, out = script(root, "--workload", write_workload(root, open_model, "o.json"), *LIMITS, "--out", ".qa/work/performance/open.js")
    assert code == 0, out
    text = (root / out["written"]).read_text(encoding="utf-8")
    assert grade.script_problems(text) == [] and '"maxVUs": 20' in text and '"rate": 25' in text


def test_a_workload_the_documented_leg_cannot_finish_is_refused_whatever_the_grant(tmp_path):
    """The leg's --timeout 300 also holds its sleep 3, the 5 s stop and 20 s for the container: 272 s of load at most."""
    root = project(tmp_path)
    wide = ["--max-vus", "20", "--max-duration-s", "4000", "--max-rps", "25"]
    tools = (SKILL / "references" / "tools.md").read_text(encoding="utf-8")
    leg = re.search(r"^```qa-leg k6 [^\n]*\n(.*?)\n```$", tools, re.M | re.S).group(1)
    assert "--timeout 300 " in leg and "'sleep 3; k6 run " in leg
    code, out = script(root, "--workload", write_workload(root, objective_workload(duration_s=272)), *wide, "--out", OUT)
    assert code == 0 and out["limits"]["max_leg_duration_s"] == 272, out
    for duration in (273, 3600):
        code, out = script(root, "--workload", write_workload(root, objective_workload(duration_s=duration)), *wide,
                           "--out", ".qa/work/performance/long.js")
        assert (code, out["ok"]) == (2, False) and "--timeout 300" in out["error"], (duration, out)
        assert not (root / ".qa" / "work" / "performance" / "long.js").exists()


REFUSED = [
    ("vus-over", objective_workload(vus=21), "above --max-vus"),
    ("duration-over", objective_workload(duration_s=86), "--max-duration-s"),
    ("closed-rate-over", objective_workload(think_time_s=0.5), "above --max-rps"),
    ("open-rate-over", objective_workload(model="open", rate_per_s=26, think_time_s=0), "above --max-rps"),
    ("no-think-time", objective_workload(think_time_s=0), "think_time_s must be"),
    ("closed-with-rate", objective_workload(rate_per_s=5), "no rate_per_s"),
    ("open-with-think", objective_workload(model="open", rate_per_s=5), "no think time"),
    ("url-path", objective_workload(requests=[{"name": "x", "method": "GET", "path": "http://shop.example.com/", "expect": 200}]), "URL"),
    ("url-header", objective_workload(requests=[{"name": "x", "method": "GET", "path": "/", "expect": 200,
                                                  "headers": {"Referer": "https://shop.example.com"}}]), "URL"),
    ("url-escaped", objective_workload(name="x"), None),   # replaced below by a raw text with an escaped slash
    ("protocol-relative", objective_workload(requests=[{"name": "x", "method": "GET", "path": "//shop.example.com/", "expect": 200}]),
     "URL"),
    ("relative-path", objective_workload(requests=[{"name": "x", "method": "GET", "path": "orders", "expect": 200}]), "path"),
    ("host-header", objective_workload(requests=[{"name": "x", "method": "GET", "path": "/", "expect": 200,
                                                   "headers": {"Host": "shop.example.com"}}]), "header"),
    ("line-break", objective_workload(requests=[{"name": "x", "method": "GET", "path": "/", "expect": 200,
                                                  "headers": {"X-A": "a\r\nHost: b"}}]), "single-line"),
    ("get-body", objective_workload(requests=[{"name": "x", "method": "GET", "path": "/", "expect": 200, "body": "a"}]), "body"),
    ("no-thresholds", objective_workload(thresholds={}), "thresholds"),
    ("bad-expression", objective_workload(thresholds={"http_req_duration": ["p(95)<200; while(true){}"]}), "one comparison"),
    ("unknown-metric", objective_workload(thresholds={"cpu": ["value<1"]}), "metric"),
    ("unknown-key", objective_workload(base_url="x"), "unknown workload keys"),
    ("bool-vus", objective_workload(vus=True), "whole number"),
    ("bad-method", objective_workload(requests=[{"name": "x", "method": "TRACE", "path": "/", "expect": 200}]), "method"),
    ("bad-name", objective_workload(name="../x"), "name"),
]


@pytest.mark.parametrize("label, doc, reason", REFUSED, ids=[r[0] for r in REFUSED])
def test_a_workload_outside_its_limits_or_holding_an_address_is_refused(tmp_path, label, doc, reason):
    root = project(tmp_path)
    raw = None
    if label == "url-escaped":
        raw = json.dumps(objective_workload()).replace('"/orders"', '"http:\\/\\/shop.example.com/orders"')
        reason = "URL"
    code, out = script(root, "--workload", write_workload(root, doc, raw=raw), *LIMITS, "--out", ".qa/work/performance/x.js")
    assert (code, out["ok"]) == (2, False) and reason in out["error"], (label, out)
    assert not (root / ".qa" / "work" / "performance" / "x.js").exists()


@pytest.mark.parametrize("label, raw", [
    ("duplicate-key", '{"name": "a", "name": "b"}'),
    ("nan", json.dumps(objective_workload()).replace('"vus": 20', '"vus": NaN')),
    ("deep", "[" * 7 + "]" * 7),
    ("very-deep", "[" * 5000 + "]" * 5000),
    ("not-json", "{"),
    ("not-utf8", None),
    ("not-an-object", "[]"),
    ("large", " " * (64 * 1024 + 1) + "{}"),
])
def test_hostile_workload_text_ends_in_a_json_refusal(tmp_path, label, raw):
    root = project(tmp_path)
    folder = root / ".qa" / "work" / "performance"
    folder.mkdir(parents=True)
    if raw is None:
        (folder / "w.json").write_bytes(b"\xff\xfe{}")
    else:
        (folder / "w.json").write_text(raw)
    code, out = script(root, "--workload", ".qa/work/performance/w.json", *LIMITS, "--out", ".qa/work/performance/x.js")
    assert (code, out["ok"]) == (2, False), (label, out)


W, OUT = ".qa/work/performance/w.json", ".qa/work/performance/x.js"


@pytest.mark.parametrize("argv", [
    ["--workload", "w.json", *LIMITS, "--out", OUT],
    ["--workload", ".qa/work/other/w.json", *LIMITS, "--out", OUT],
    ["--workload", ".qa/work/performance/../performance/w.json", *LIMITS, "--out", OUT],
    ["--workload", "/tmp/w.json", *LIMITS, "--out", OUT],
    ["--workload", W, *LIMITS, "--out", ".qa/work/performance/x.py"],
    ["--workload", W, *LIMITS, "--out", "x.js"],
    ["--workload", W, *LIMITS, "--out", ".qa/work/performance/sub/x.js"],
    ["--workload", W, *LIMITS, "--out", "/qa-evidence/x.js"],
    ["--workload", W, "--max-vus", "0", "--max-duration-s", "90", "--out", OUT],
    ["--workload", W, "--max-vus", "-1", "--max-duration-s", "90", "--out", OUT],
    ["--workload", W, "--max-vus", "1e3", "--max-duration-s", "90", "--out", OUT],
    ["--workload", W, "--max-duration-s", "90", "--out", OUT],
    ["--workload", W, *LIMITS],
], ids=["bare-name", "other-folder", "dot-dot", "absolute", "out-suffix", "out-bare", "out-sub", "out-evidence", "vus-zero", "vus-negative",
        "vus-float", "vus-missing", "out-missing"])
def test_paths_outside_the_folder_and_bad_limits_are_refused(tmp_path, argv):
    root = project(tmp_path)
    write_workload(root, objective_workload())
    code, out = script(root, *argv)
    assert (code, out["ok"]) == (2, False), (argv, out)
    assert sorted(p.name for p in (root / ".qa" / "work" / "performance").iterdir()) == ["w.json"]


def test_links_are_never_followed_on_read_or_write(tmp_path):
    root = project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "w.json").write_text(json.dumps(objective_workload()))
    folder = root / ".qa" / "work" / "performance"
    folder.mkdir()
    (folder / "w.json").symlink_to(outside / "w.json")
    code, out = script(root, "--workload", ".qa/work/performance/w.json", *LIMITS, "--out", ".qa/work/performance/x.js")
    assert (code, out["ok"]) == (2, False), out
    (folder / "w.json").unlink()
    write_workload(root, objective_workload())
    (outside / "victim.js").write_text("keep")
    (folder / "x.js").symlink_to(outside / "victim.js")
    code, out = script(root, "--workload", ".qa/work/performance/w.json", *LIMITS, "--out", ".qa/work/performance/x.js")
    assert code == 0, out
    assert (outside / "victim.js").read_text() == "keep" and not (folder / "x.js").is_symlink()
    linked = tmp_path / "linked"
    (linked / ".qa").mkdir(parents=True)
    (linked / ".qa" / "work").symlink_to(outside, target_is_directory=True)
    code, out = script(linked, "--workload", ".qa/work/performance/w.json", *LIMITS, "--out", ".qa/work/performance/x.js")
    assert (code, out["ok"]) == (2, False), out


def test_without_init_nothing_is_created(tmp_path):
    root = tmp_path / "bare"
    root.mkdir()
    code, out = script(root, "--workload", ".qa/work/performance/w.json", *LIMITS, "--out", ".qa/work/performance/x.js")
    assert (code, out["ok"]) == (2, False) and not (root / ".qa").exists(), out


# --------------------------------------------------------------------------- perf_table.py

def table(root, execution):
    done = subprocess.run([sys.executable, "-I", "-B", str(PERF_TABLE), "--execution", execution], cwd=str(root), capture_output=True,
                          text=True, timeout=180)
    assert done.stderr == "", done.stderr
    return done.returncode, json.loads(done.stdout)


def summary(p95=143.2, failed=0.0, rate=21.5, thresholds_ok=True):
    trend = {"avg": 80.1, "min": 5.0, "med": 70.0, "max": 260.0, "p(90)": 120.0, "p(95)": p95, "p(99)": 240.0}
    return {"metrics": {
        "http_req_duration": {"type": "trend", "contains": "time", "values": trend,
                              "thresholds": {"p(95)<200": {"ok": thresholds_ok}}},
        "http_req_failed": {"type": "rate", "contains": "default", "values": {"rate": failed, "passes": 0, "fails": 600}},
        "http_reqs": {"type": "counter", "contains": "default", "values": {"count": 645, "rate": rate}},
        "iterations": {"type": "counter", "contains": "default", "values": {"count": 645, "rate": rate}},
        "vus_max": {"type": "gauge", "contains": "default", "values": {"value": 5, "min": 5, "max": 5}}},
        "state": {"testRunDurationMs": 30012.5}}


COPY = "import shutil, sys; shutil.copyfile(sys.argv[1], sys.argv[2])"


def test_a_run_with_a_k6_report_is_listed_with_labels_simulated_on_a_local_class(qa_on_fixture):
    fixture = qa_on_fixture("rest-api")
    (fixture.root / "summary.json").write_text(json.dumps(summary(thresholds_ok=False, p95=230.0)))
    code, ran = fixture.cli("run", "--category", "performance", "--report", "k6:{evidence}/k6-summary.json", "--", sys.executable, "-c", COPY,
                            "summary.json", "{evidence}/k6-summary.json")
    assert ran["status"] == "failed", ran
    code, out = table(fixture.root, ran["execution"])
    assert code == 0 and out["measured"] is True and out["environment_class"] == "local", out
    rows = {(r["metric"], r["stat"]): r for r in out["rows"]}
    assert rows[("http_req_duration", "p(95)")]["value"] == 230.0 and rows[("http_req_duration", "p(95)")]["label"] == "simulated"
    assert rows[("http_reqs", "rate")]["value"] == 21.5 and rows[("vus_max", "max")]["value"] == 5
    assert rows[("successful requests", "rate")]["label"] == "derived" and rows[("error rate", "percent")]["value"] == 0.0
    assert {r["label"] for r in out["rows"]} == {"simulated", "derived"} and all(r["execution"] == ran["execution"] for r in out["rows"])
    assert out["thresholds"] == [{"metric": "http_req_duration", "threshold": "p(95)<200", "ok": False}]
    written = json.loads((fixture.root / out["written"]).read_text(encoding="utf-8"))
    assert written["rows"] == out["rows"]


def test_an_attested_summary_is_never_the_run_s_numbers_and_a_blocked_run_measures_nothing(qa_on_fixture):
    """A k6 summary attached to a run that did not produce it is ``attested``: no row, no label, no threshold, no EXE."""
    fixture = qa_on_fixture("rest-api")
    code, ran = fixture.cli("run", "--", sys.executable, "-c", "pass")
    assert ran["status"] == "passed", ran
    (fixture.root / "later.json").write_text(json.dumps(summary()))
    code, ingested = fixture.cli("ingest", "k6", "later.json", "--from", ran["execution"])
    assert code == 0 and ingested["strength"] == "attested", ingested
    code, out = table(fixture.root, ran["execution"])
    assert code == 0 and out["measured"] is False and out["rows"] == [] and "thresholds" not in out, out
    assert len(out["attested"]) == 1 and out["attested"][0].startswith("ingested/") and "attested" in out["why"], out
    written = json.loads((fixture.root / ".qa" / "work" / "performance" / ("perf-%s.json" % ran["execution"])).read_text(encoding="utf-8"))
    assert written["measured"] is False and written["rows"] == []
    code, blocked = fixture.cli("run", "--container", "grafana/k6@sha256:" + "0" * 64, "--shell", "--", "k6 run x.js")
    assert blocked["status"] == "blocked", blocked
    code, out = table(fixture.root, blocked["execution"])
    assert code == 0 and out["measured"] is False and out["rows"] == [] and "blocked" in out["why"], out


def test_a_structured_ingest_of_the_run_s_own_report_is_read_beside_it(qa_on_fixture):
    """The run's own stored report, attached again, is ``structured`` (its evidence manifest lists it): it stays the run's."""
    fixture = qa_on_fixture("rest-api")
    (fixture.root / "summary.json").write_text(json.dumps(summary(thresholds_ok=False, p95=230.0)))
    code, ran = fixture.cli("run", "--category", "performance", "--report", "k6:{evidence}/k6-summary.json", "--", sys.executable, "-c", COPY,
                            "summary.json", "{evidence}/k6-summary.json")
    assert ran["status"] == "failed", ran
    stored = fixture.root / ".qa" / "evidence" / ran["execution"] / "reports" / "k6-summary.json"
    (fixture.root / "own.json").write_bytes(stored.read_bytes())
    code, ingested = fixture.cli("ingest", "k6", "own.json", "--from", ran["execution"])
    assert code == 0 and ingested["strength"] == "structured", ingested
    code, out = table(fixture.root, ran["execution"])
    assert code == 0 and out["measured"] is True and out["attested"] == [], out
    assert {(r["metric"], r["stat"]): (r["value"], r["label"]) for r in out["rows"]}[("http_req_duration", "p(95)")] == (230.0, "simulated")
    assert out["thresholds"] == [{"metric": "http_req_duration", "threshold": "p(95)<200", "ok": False}]


@pytest.mark.parametrize("execution", ["x", "EXE-1", "EXE-01AAAAAAAAAAAAAAAAAAAAAAAA; ls", "EXE-01AAAAAAAAAAAAAAAAAAAAAAAA"])
def test_an_unknown_or_malformed_execution_is_refused(qa_on_fixture, execution):
    fixture = qa_on_fixture("rest-api")
    code, out = table(fixture.root, execution)
    assert (code, out["ok"]) == (2, False), out


def test_a_stored_summary_reached_through_a_link_is_not_read(qa_on_fixture, tmp_path):
    fixture = qa_on_fixture("rest-api")
    (fixture.root / "summary.json").write_text(json.dumps(summary(thresholds_ok=False, p95=230.0)))
    code, ran = fixture.cli("run", "--category", "performance", "--report", "k6:{evidence}/k6-summary.json", "--", sys.executable, "-c", COPY,
                            "summary.json", "{evidence}/k6-summary.json")
    assert ran["status"] == "failed", ran
    stored = fixture.root / ".qa" / "evidence" / ran["execution"] / "reports" / "k6-summary.json"
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps(summary(p95=999.0, thresholds_ok=True)))
    os.replace(str(stored), str(tmp_path / "moved.json"))
    stored.symlink_to(elsewhere)
    code, out = table(fixture.root, ran["execution"])
    # the numbers are the record's own (the kernel read them at the run); the linked file gives no threshold verdict
    assert code == 0 and out["measured"] is True and out["thresholds"] == [], out
    assert {(r["metric"], r["stat"]): r["value"] for r in out["rows"]}[("http_req_duration", "p(95)")] == 230.0
