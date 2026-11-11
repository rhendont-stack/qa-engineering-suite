from __future__ import annotations

import json
import sys

import pytest

from qa import execution, gate, ingest, redact, state
from qa.canonical import sha256_bytes
from qa.ingest import mutation

STATUSES = ("Killed", "Survived", "NoCoverage", "Timeout", "CompileError", "RuntimeError", "Ignored", "Pending")


def mutant(mid="1", status="Killed", line=3, mutator="ConditionalExpression", killed_by=None, **extra):
    item = {"id": mid, "mutatorName": mutator, "replacement": "true", "status": status,
            "location": {"start": {"line": line, "column": 1}, "end": {"line": line, "column": 9}}}
    if killed_by is not None:
        item["killedBy"] = killed_by
    item.update(extra)
    return item


def report(files=None, version="1.0", **top):
    doc = {"schemaVersion": version, "thresholds": {"high": 80, "low": 60},
           "files": files if files is not None else {"src/a.js": {"language": "javascript", "source": "x", "mutants": [mutant()]}}}
    doc.update(top)
    return doc


def parse(doc):
    """A document as JSON, or a string as the bytes themselves."""
    return ingest.parse_bytes("mutation", (doc if isinstance(doc, str) else json.dumps(doc)).encode("utf-8"))


def of(*pairs):
    """A report whose one file holds a mutant per status given."""
    return report({"src/a.js": {"language": "javascript", "source": "x", "mutants": [mutant(str(i), s) for i, s in enumerate(pairs)]}})


# --------------------------------------------------------------------------- the format

def test_mutation_is_a_measurement_of_kind_mutants():
    assert ingest.FORMAT_KINDS["mutation"] == "mutants" and ingest.is_measurement("mutation")
    s = parse(report())
    assert (s.format, s.kind, s.cases, s.alerts, s.files, s.anomalies) == ("mutation", "mutants", (), (), (), ())


def test_a_mutant_carries_what_the_report_says():
    doc = report({"src/a.js": {"language": "javascript", "source": "x", "mutants": [mutant("7", "Killed", 12, "EqualityOperator", ["0", "2"])]}})
    assert parse(doc).mutants == (ingest.Mutant("src/a.js", "7", "EqualityOperator", 12, "Killed", ("0", "2")),)
    assert parse(report()).mutants[0].killed_by == ()  # no killedBy: killed by nobody recorded


@pytest.mark.parametrize("version", ["1", "1.0", "1.2.3", "2", "2.0"])
def test_schema_versions_one_and_two_are_read(version):
    assert len(parse(report(version=version)).mutants) == 1


@pytest.mark.parametrize("version", ["0", "3", "3.0", "", "one", "1.", ".1", "1.x", 1, 2.0, None, ["1"]])
def test_other_schema_versions_are_refused(version):
    with pytest.raises(ingest.ReportInvalid) as e:
        parse(report(version=version))
    assert e.value.details["detail"] == "unsupported_schema_version"


def test_a_report_without_a_schema_version_is_not_a_mutation_report():
    doc = report()
    del doc["schemaVersion"]
    with pytest.raises(ingest.ReportInvalid) as e:
        parse(doc)
    assert e.value.details["detail"] == "not_mutation_report"


@pytest.mark.parametrize("status", STATUSES)
def test_every_status_of_the_schema_is_read(status):
    assert parse(of(status)).mutants[0].status == status


def test_an_empty_report_is_a_report_with_no_mutants_and_not_an_error():
    assert parse(report({})).mutants == () and parse(report({"a.js": {"language": "javascript", "source": "", "mutants": []}})).mutants == ()


def test_pending_mutants_make_the_report_partial():
    assert parse(of("Killed", "Pending")).anomalies == ("partial",)
    assert parse(of("Killed", "Ignored")).anomalies == ()


# --------------------------------------------------------------------------- the score

def test_score_formula_excludes_errors_and_ignored():
    sample = parse(of("Killed", "Killed", "Timeout", "Survived", "CompileError", "RuntimeError", "Ignored", "Pending")).mutants
    assert mutation.score(sample) == pytest.approx(75.0)  # 100 * (2 + 1) / (2 + 1 + 1 + 0)


def test_no_coverage_counts_against_the_score():
    assert mutation.score(parse(of("Killed", "NoCoverage")).mutants) == pytest.approx(50.0)


@pytest.mark.parametrize("statuses", [(), ("CompileError",), ("Ignored", "Pending", "RuntimeError")])
def test_score_is_none_without_valid_mutants(statuses):
    assert mutation.score(parse(of(*statuses)).mutants) is None


def test_the_score_of_counts_is_the_same_formula():
    assert mutation.score_of_counts({"Killed": 699, "Survived": 301, "Ignored": 40}) == pytest.approx(69.9)
    assert mutation.score_of_counts({}) is None


def test_the_totals_a_run_records_list_every_file():
    doc = report({"src/a.js": {"language": "javascript", "source": "", "mutants": [mutant("1"), mutant("2", "Survived")]},
                  "src/b.js": {"language": "javascript", "source": "", "mutants": [mutant("1", "NoCoverage")]}})
    assert parse(doc).measurement_totals() == {
        "files": 2, "mutants": 3, "by_file": {"src/a.js": {"Killed": 1, "Survived": 1}, "src/b.js": {"NoCoverage": 1}}}


# --------------------------------------------------------------------------- parser abuse (§17)

def hostile():
    many = {"src/a.js": {"language": "javascript", "source": "", "mutants": [mutant("1"), mutant("1")]}}
    return {
        "unknown_status": report({"a.js": {"mutants": [mutant(status="Exploded")]}}),
        "status_in_the_wrong_case": report({"a.js": {"mutants": [mutant(status="killed")]}}),
        "status_not_a_string": report({"a.js": {"mutants": [mutant(status=1)]}}),
        "line_zero": report({"a.js": {"mutants": [mutant(line=0)]}}),
        "negative_line": report({"a.js": {"mutants": [mutant(line=-1)]}}),
        "fractional_line": report({"a.js": {"mutants": [mutant(line=1.5)]}}),
        "boolean_line": report({"a.js": {"mutants": [mutant(line=True)]}}),
        "string_line": report({"a.js": {"mutants": [mutant(line="3")]}}),
        "line_missing": report({"a.js": {"mutants": [dict(mutant(), location={})]}}),
        "score_threshold_above_100": report(thresholds={"high": 101, "low": 60}),
        "negative_threshold": report(thresholds={"high": 80, "low": -1}),
        "break_threshold_above_100": report(thresholds={"high": 80, "low": 60, "break": 150}),
        "threshold_not_a_number": report(thresholds={"high": "high", "low": 60}),
        "thresholds_not_an_object": report(thresholds=[80, 60]),
        "duplicate_mutant": report(many),
        "files_is_a_list": report(files=[]),
        "file_is_a_list": report({"a.js": []}),
        "mutants_is_not_a_list": report({"a.js": {"mutants": {"1": mutant()}}}),
        "mutant_is_not_an_object": report({"a.js": {"mutants": ["Killed"]}}),
        "mutant_without_id": report({"a.js": {"mutants": [{k: v for k, v in mutant().items() if k != "id"}]}}),
        "mutant_id_is_a_number": report({"a.js": {"mutants": [mutant(mid=1)]}}),
        "mutant_without_mutator": report({"a.js": {"mutants": [{k: v for k, v in mutant().items() if k != "mutatorName"}]}}),
        "killed_by_is_a_string": report({"a.js": {"mutants": [mutant(killed_by="0")]}}),
        "killed_by_holds_a_number": report({"a.js": {"mutants": [mutant(killed_by=[0])]}}),
        "top_level_list": [],
        "top_level_string": "mutants",
    }


@pytest.mark.parametrize("name", sorted(hostile()))
def test_mutation_parser_refuses_hostile_input(name):
    with pytest.raises(ingest.ReportInvalid) as e:
        parse(hostile()[name])
    assert e.value.details["detail"] != "unknown_format"


@pytest.mark.parametrize("text,detail", [
    ('{"schemaVersion": "1", "files": {"a.js": {"mutants": [{"id": "1", "mutatorName": "m", "status": "Killed", "location": {"start": {"line": NaN}}}]}}}', "invalid_json"),
    ('{"schemaVersion": "1", "files": {"a.js": {"mutants": [{"id": "1", "mutatorName": "m", "status": "Killed", "location": {"start": {"line": Infinity}}}]}}}', "invalid_json"),
    ('{"schemaVersion": "1", "thresholds": {"high": NaN, "low": 60}, "files": {}}', "invalid_json"),
    ('{"schemaVersion": "1", "files": {}, "files": {}}', "invalid_json"),
    ("not json at all", "invalid_json"),
    ("", "invalid_json"),
    ('{"schemaVersion": "1", "files": {"a\\ud800.js": {"mutants": [{"id": "1", "mutatorName": "m", "status": "Killed", "location": {"start": {"line": 1}}}]}}}', "not_storable"),
])
def test_mutation_parser_refuses_json_that_cannot_be_trusted(text, detail):
    with pytest.raises(ingest.ReportInvalid) as e:
        parse(text)
    assert e.value.details["detail"] == detail


def test_a_deeply_nested_report_does_not_crash():
    text = '{"schemaVersion": "1", "files": {}, "x": ' + "[" * 5000 + "]" * 5000 + "}"
    try:
        parse(text)
    except ingest.ReportInvalid:
        return


def test_an_oversize_mutation_report_is_refused(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps(report()))
    with pytest.raises(ingest.ReportInvalid) as e:
        ingest.parse_report("mutation", path, 10)
    assert e.value.details["detail"] == "too_large"


# --------------------------------------------------------------------------- paths stay as recorded (F12)

@pytest.mark.parametrize("path", ["../x.js", "src/../../x.js", "a/../../b.js", "..\\x.js"])
def test_a_path_that_climbs_is_recorded_as_given_and_flagged_not_refused(path):
    s = parse(report({path: {"language": "javascript", "source": "", "mutants": [mutant()]}}))
    assert s.mutants[0].file == path.replace("\\", "/") and s.anomalies == ("path_outside_project",)


def test_paths_that_stay_inside_are_not_flagged():
    for path in ("src/a..b.js", "src\\a.js", "./a.js", "a.js"):
        assert parse(report({path: {"language": "javascript", "source": "", "mutants": [mutant()]}})).anomalies == ()


# --------------------------------------------------------------------------- runs

WRITER = ("import sys, pathlib\n"
          "*pairs, code = sys.argv[1:]\n"
          "for i in range(0, len(pairs), 2): pathlib.Path(pairs[i]).write_text(pairs[i + 1])\n"
          "sys.exit(int(code))")
TEXT = json.dumps(of("Killed", "Survived"))


def run(ws, actor, py, files, exit_code, reports, **kw):
    args = [x for pair in files for x in pair] + [str(exit_code)]
    return execution.execute(ws, actor, execution.RunRequest(command=py.argv(WRITER, *args), reports=tuple(reports), **kw))


def record(ws, result):
    return state.load(ws)["executions"][result["execution"]]


def test_a_mutation_report_is_recorded_on_the_execution_and_never_decides_status(trusted, actor, py):
    reports = (("mutation", "{evidence}/mutation.json"),)
    ok = run(trusted, actor, py, (("{evidence}/mutation.json", TEXT),), 0, reports)
    assert (ok["status"], ok["reason"], ok["strength"]) == ("passed", None, "exit_code")
    entry = record(trusted, ok)["measurements"][0]
    assert (entry["format"], entry["kind"], entry["file"]) == ("mutation", "mutants", "reports/mutation.json")
    assert entry["totals"] == {"files": 1, "mutants": 2, "by_file": {"src/a.js": {"Killed": 1, "Survived": 1}}}
    assert entry["file"] in record(trusted, ok)["evidence_manifest"]["files"] and "anomalies" not in entry
    crashed = run(trusted, actor, py, (("{evidence}/mutation.json", TEXT),), 3, reports)  # a mutation tool's exit code is not a verdict on the code
    assert (crashed["status"], crashed["reason"]) == ("failed", "nonzero_exit")
    assert record(trusted, crashed)["measurements"][0]["totals"]["mutants"] == 2
    assert len(execution.readable_measurements(record(trusted, crashed))) == 1


def test_a_run_may_declare_tests_and_mutation_but_not_tests_and_alerts(trusted, actor, py):
    junit = '<testsuite><testcase classname="c" name="t"/></testsuite>'
    r = run(trusted, actor, py, (("{evidence}/j.xml", junit), ("{evidence}/m.json", TEXT)), 0,
            (("junit", "{evidence}/j.xml"), ("mutation", "{evidence}/m.json")))
    assert (r["status"], r["tests"]["total"], len(record(trusted, r)["measurements"])) == ("passed", 1, 1)
    bad = run(trusted, actor, py, (("{evidence}/j.xml", junit), ("{evidence}/m.json", "garbage")), 0,
              (("junit", "{evidence}/j.xml"), ("mutation", "{evidence}/m.json")))
    assert bad["status"] == "passed" and any(item.startswith("measurement_invalid") for item in bad["limitations"])
    assert "measurements" not in record(trusted, bad)


def test_a_missing_or_invalid_mutation_report_is_the_runs_only_report_so_it_is_inconclusive(trusted, actor, py):
    gone = run(trusted, actor, py, (), 0, (("mutation", "{evidence}/mutation.json"),))
    junk = run(trusted, actor, py, (("{evidence}/mutation.json", "{}"),), 0, (("mutation", "{evidence}/mutation.json"),))
    assert [(r["status"], r["reason"]) for r in (gone, junk)] == [("inconclusive", "report_missing"), ("inconclusive", "report_invalid")]


def test_a_path_outside_the_project_is_flagged_on_the_measurement(trusted, actor, py):
    doc = json.dumps(report({"../x.js": {"language": "javascript", "source": "", "mutants": [mutant()]}}))
    r = run(trusted, actor, py, (("{evidence}/m.json", doc),), 0, (("mutation", "{evidence}/m.json"),))
    assert r["status"] == "passed" and record(trusted, r)["measurements"][0]["anomalies"] == ["path_outside_project"]


# --------------------------------------------------------------------------- qa ingest mutation

def test_ingest_mutation_from_an_execution(trusted, actor, py, tmp_path):
    r = execution.execute(trusted, actor, execution.RunRequest(command=py.argv("pass")))
    path = tmp_path / "mutation.json"
    path.write_text(TEXT)
    out = execution.ingest_file(trusted, actor, "mutation", path, from_execution=r["execution"], category="functional")
    assert (out["kind"], out["total"], out["strength"]) == ("mutants", 2, "attested")
    item = record(trusted, r)["ingested"][0]
    assert (item["format"], item["kind"], item["category"]) == ("mutation", "mutants", "functional")
    assert item["totals"] == {"files": 1, "mutants": 2, "by_file": {"src/a.js": {"Killed": 1, "Survived": 1}}}
    assert (trusted.path("evidence", r["execution"]) / item["file"]).is_file() and record(trusted, r)["status"] == "passed"


def test_cli_ingests_mutation_and_refuses_a_bad_file(trusted, qa_cli, tmp_path):
    exe = qa_cli("run", "--", "true")[1]["execution"]
    good, bad = tmp_path / "m.json", tmp_path / "bad.json"
    good.write_text(TEXT)
    bad.write_text(json.dumps(of("Exploded")))
    code, out = qa_cli("ingest", "mutation", str(good), "--from", exe)
    assert (code, out["kind"], out["total"]) == (0, "mutants", 2)
    code, out = qa_cli("ingest", "mutation", str(bad), "--from", exe)
    assert (code, out["error"]["code"], out["error"]["details"]["detail"]) == (2, "report_invalid", "unknown_status")


def test_cli_run_takes_a_mutation_report(trusted, qa_cli):
    code, out = qa_cli("run", "--report", "mutation:{evidence}/m.json", "--", sys.executable, "-c",
                       "import sys; open(sys.argv[1], 'w').write(%r)" % TEXT, "{evidence}/m.json")
    assert (code, out["status"], out["strength"]) == (0, "passed", "exit_code")
    assert out["measurements"][0]["kind"] == "mutants"


def test_a_mutation_only_run_is_not_a_suite_run(trusted, actor, py):
    r = run(trusted, actor, py, (("{evidence}/m.json", TEXT),), 0, (("mutation", "{evidence}/m.json"),))
    assert gate._is_suite_run(record(trusted, r)) is False


# --------------------------------------------------------------------------- no secret-shaped path reaches the ledger

KEY = "AKIA" + "ABCDEFGHIJKLMNOP"


def ledger_text(ws):
    return b"".join(p.read_bytes() for p in ws.path("ledger").iterdir()).decode("utf-8", "replace")


def test_a_secret_shaped_path_never_reaches_the_ledger_through_ingest_or_run(trusted, actor, py, tmp_path):
    doc = json.dumps(report({"src/%s/a.js" % KEY: {"language": "javascript", "source": "", "mutants": [mutant()]}}))
    lcov = "SF:src/%s/a.js\nDA:1,1\nend_of_record\n" % KEY
    r = execution.execute(trusted, actor, execution.RunRequest(command=py.argv("pass")))
    for fmt, text in (("mutation", doc), ("lcov", lcov)):
        path = tmp_path / fmt
        path.write_text(text)
        execution.ingest_file(trusted, actor, fmt, path, from_execution=r["execution"], category="functional")
    run(trusted, actor, py, (("{evidence}/m.json", doc), ("{evidence}/c.info", lcov)), 0,
        (("mutation", "{evidence}/m.json"), ("lcov", "{evidence}/c.info")))
    assert KEY not in ledger_text(trusted)
    item = record(trusted, r)["ingested"][0]
    assert item["totals"]["mutants"] == 1 and len(item["totals"]["by_file"]) == 1


# --------------------------------------------------------------------------- K3: a stored JSON report still parses after redaction

SECRET = "Zq8LmW4pR9" + "xTv"   # built at runtime
SECRET_TEXT = json.dumps(report({"src/a.js": {"language": "javascript", "source": "x", "mutants": [
    mutant("1", replacement='token = os.environ["API_TOKEN_VALUE"]'),
    mutant("2", "Survived", replacement='api_key = "%s"' % SECRET)]}}))


COPY = "import shutil, sys; shutil.copyfile(sys.argv[1], sys.argv[2])"


def copy_run(ws, actor, py, source, out, reports, exit_code=0):
    """A run that copies ``source`` to ``out``: the secret-shaped text stays out of the command's arguments, which the guard reads."""
    code = COPY if exit_code == 0 else COPY + "; sys.exit(%d)" % exit_code
    return execution.execute(ws, actor, execution.RunRequest(command=py.argv(code, str(source), out), reports=tuple(reports)))


def test_a_secret_shaped_assignment_in_a_mutant_is_redacted_and_the_stored_report_still_parses(trusted, actor, py, tmp_path):
    cut, _ = redact.load_redactor(trusted).bytes_(SECRET_TEXT.encode("utf-8"))
    with pytest.raises(ValueError):
        json.loads(cut)   # the byte-level pass alone would have left a report_invalid copy
    source = tmp_path / "source.json"
    source.write_text(SECRET_TEXT)
    r = copy_run(trusted, actor, py, source, "{evidence}/m.json", (("mutation", "{evidence}/m.json"),))
    assert (r["status"], r["reason"]) == ("passed", None)
    entry = record(trusted, r)["measurements"][0]
    assert entry["totals"]["by_file"] == {"src/a.js": {"Killed": 1, "Survived": 1}}
    stored = trusted.path("evidence", r["execution"], "reports", "m.json").read_bytes()
    assert SECRET.encode() not in stored and b"[REDACTED:secret_assignment:" in stored
    assert ingest.parse_bytes("mutation", stored).kind == "mutants"
    manifest = json.loads(trusted.path("evidence", r["execution"], "manifest.json").read_text(encoding="utf-8"))
    assert manifest["reports/m.json"]["redactions"] == 2 and manifest["reports/m.json"]["sha256"] == sha256_bytes(stored)
    assert record(trusted, r)["redactions"] >= 2


def test_an_ingested_mutation_report_holding_a_secret_is_stored_parsed_and_redacted(trusted, actor, py, tmp_path):
    r = execution.execute(trusted, actor, execution.RunRequest(command=py.argv("pass")))
    path = tmp_path / "mutation.json"
    path.write_text(SECRET_TEXT)
    out = execution.ingest_file(trusted, actor, "mutation", path, from_execution=r["execution"], category="functional")
    assert (out["kind"], out["total"]) == ("mutants", 2)
    item = record(trusted, r)["ingested"][0]
    stored = (trusted.path("evidence", r["execution"]) / item["file"]).read_bytes()
    assert SECRET.encode() not in stored and ingest.parse_bytes("mutation", stored).kind == "mutants"


def test_a_sarif_snippet_holding_a_secret_is_stored_redacted_and_still_parses(trusted, actor, py, tmp_path):
    sarif = json.dumps({"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "scanner", "rules": [{"id": "R1"}]}}, "results": [
        {"ruleId": "R1", "level": "error", "message": {"text": "hard-coded secret"}, "locations": [{"physicalLocation": {
            "artifactLocation": {"uri": "a.py"}, "region": {"startLine": 1, "snippet": {"text": 'my_token: "%s\\"' % SECRET}}}}]}]}]})
    cut, _ = redact.load_redactor(trusted).bytes_(sarif.encode("utf-8"))
    with pytest.raises(ValueError):
        json.loads(cut)
    source = tmp_path / "source.sarif"
    source.write_text(sarif)
    r = copy_run(trusted, actor, py, source, "{evidence}/r.sarif", (("sarif", "{evidence}/r.sarif"),), exit_code=1)
    assert (r["status"], r["reason"]) == ("failed", "assertion")   # an alert, read from the stored copy: not report_invalid
    stored = trusted.path("evidence", r["execution"], "reports", "r.sarif").read_bytes()
    assert SECRET.encode() not in stored and json.loads(stored)["runs"][0]["results"][0]["ruleId"] == "R1"
