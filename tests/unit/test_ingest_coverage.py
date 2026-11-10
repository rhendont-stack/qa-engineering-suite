from __future__ import annotations

import sys

import pytest

from helpers import WRITE_JUNIT
from qa import execution, ingest, state
from qa.errors import QaError

CAP = 10**6

LCOV = "SF:src/a.py\nDA:1,1\nDA:2,0\nLF:2\nLH:1\nBRDA:1,0,0,1\nBRDA:1,0,1,-\nBRF:2\nBRH:1\nend_of_record\n"
COBERTURA_DOCTYPE = '<!DOCTYPE coverage SYSTEM "http://cobertura.sourceforge.net/xml/coverage-04.dtd">'
COBERTURA = (COBERTURA_DOCTYPE + '<coverage lines-valid="3" lines-covered="2" branches-valid="2" branches-covered="1">'
             '<packages><package name="p"><classes><class name="a" filename="src/a.py">'
             '<methods><method name="m" signature="()V"><lines><line number="1" hits="1"/></lines></method></methods>'
             '<lines><line number="1" hits="1"/><line number="2" hits="1" branch="true" condition-coverage="50% (1/2)"/>'
             '<line number="3" hits="0"/></lines></class></classes></package></packages></coverage>')
JACOCO_DOCTYPE = '<!DOCTYPE report PUBLIC "-//JACOCO//DTD Report 1.1//EN" "report.dtd">'
JACOCO = (JACOCO_DOCTYPE + '<report name="r"><package name="com/x"><sourcefile name="A.java">'
          '<line nr="1" mi="0" ci="3" mb="1" cb="1"/><line nr="2" mi="2" ci="0" mb="0" cb="0"/>'
          '<counter type="LINE" missed="1" covered="1"/><counter type="BRANCH" missed="1" covered="1"/>'
          '</sourcefile></package></report>')
VALID = {"lcov": LCOV, "cobertura": COBERTURA, "jacoco": JACOCO}


def parse(fmt, text):
    return ingest.parse_bytes(fmt, text.encode("utf-8"))


def totals(summary):
    return (len(summary.files), summary.lines_total, summary.lines_covered, summary.branches_total, summary.branches_covered)


@pytest.mark.parametrize("fmt", sorted(VALID))
def test_a_coverage_format_is_a_measurement_kind(fmt):
    assert ingest.FORMAT_KINDS[fmt] == "coverage"
    s = parse(fmt, VALID[fmt])
    assert (s.format, s.kind, s.cases, s.alerts) == (fmt, "coverage", (), ())
    assert s.files[0].path.endswith(("a.py", "A.java")) and s.anomalies == ()


def test_each_format_counts_what_it_declares():
    assert totals(parse("lcov", LCOV)) == (1, 2, 1, 2, 1)
    assert totals(parse("cobertura", COBERTURA)) == (1, 3, 2, 2, 1)  # the method's own <line> is not counted twice
    assert totals(parse("jacoco", JACOCO)) == (1, 2, 1, 2, 1)
    assert parse("jacoco", JACOCO).files[0].path == "com/x/A.java"


def test_lcov_without_summary_lines_is_derived_from_its_records():
    s = parse("lcov", "SF:a.js\nDA:1,3\nDA:2,0\nDA:3,1\nBRDA:1,0,0,0\nBRDA:1,0,1,2\nend_of_record\n")
    assert totals(s) == (1, 3, 2, 2, 1)


@pytest.mark.parametrize("fmt", ["lcov", "cobertura", "jacoco"])
def test_an_empty_report_is_a_report_with_no_files_and_not_an_error(fmt):
    empty = {"lcov": "", "cobertura": "<coverage/>", "jacoco": "<report name='r'/>"}[fmt]
    assert totals(parse(fmt, empty)) == (0, 0, 0, 0, 0)


# --------------------------------------------------------------------------- parser abuse (§17)

ENTITY = '<!DOCTYPE coverage [<!ENTITY x "boom">]><coverage lines-valid="&x;"/>'
BILLION = '<!DOCTYPE report [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><report name="&b;"/>'
HOSTILE = {
    "entity_cobertura": ("cobertura", ENTITY),
    "entity_jacoco": ("jacoco", BILLION),
    "internal_subset_on_a_known_doctype": ("cobertura", '<!DOCTYPE coverage SYSTEM "http://cobertura.sourceforge.net/xml/coverage-04.dtd" [<!ELEMENT a ANY>]><coverage/>'),
    "unknown_doctype": ("jacoco", '<!DOCTYPE report SYSTEM "http://example.invalid/evil.dtd"><report name="r"/>'),
    "doctype_with_the_wrong_root": ("cobertura", JACOCO_DOCTYPE + "<coverage/>"),
    "not_xml": ("cobertura", "this is not xml"),
    "wrong_root_cobertura": ("cobertura", "<report/>"),
    "wrong_root_jacoco": ("jacoco", "<coverage/>"),
    "negative_lcov": ("lcov", "SF:a\nLF:-1\nLH:0\nend_of_record\n"),
    "covered_over_total_lcov": ("lcov", "SF:a\nLF:2\nLH:3\nend_of_record\n"),
    "branches_covered_over_total_lcov": ("lcov", "SF:a\nBRF:1\nBRH:2\nend_of_record\n"),
    "nan_lcov": ("lcov", "SF:a\nDA:1,nan\nend_of_record\n"),
    "inf_hits_lcov": ("lcov", "SF:a\nDA:1,inf\nend_of_record\n"),
    "not_a_number_lcov": ("lcov", "SF:a\nLF:many\nend_of_record\n"),
    "record_without_a_file_lcov": ("lcov", "DA:1,1\nend_of_record\n"),
    "unterminated_record_lcov": ("lcov", "SF:a\nDA:1,1\n"),
    "negative_cobertura": ("cobertura", '<coverage lines-valid="-3" lines-covered="0"/>'),
    "covered_over_total_cobertura": ("cobertura", '<coverage lines-valid="2" lines-covered="3"/>'),
    "branch_fraction_over_total_cobertura": ("cobertura", COBERTURA.replace("(1/2)", "(3/2)")),
    "huge_condition_coverage_cobertura": ("cobertura", COBERTURA.replace("(1/2)", "(%s/%s)" % ("9" * 5000, "9" * 5000))),
    "unicode_digits_condition_coverage_cobertura": ("cobertura", COBERTURA.replace("(1/2)", "(\u0661/\u0662)")),
    "undefined_entity_cobertura": ("cobertura", COBERTURA.replace('filename="src/a.py"', 'filename="&x;"')),
    "nan_cobertura": ("cobertura", '<coverage lines-valid="nan" lines-covered="0"/>'),
    "negative_hits_cobertura": ("cobertura", COBERTURA.replace('number="3" hits="0"', 'number="3" hits="-1"')),
    "negative_jacoco": ("jacoco", JACOCO.replace('missed="1" covered="1"/><counter type="BRANCH"', 'missed="-1" covered="1"/><counter type="BRANCH"')),
    "nan_jacoco": ("jacoco", JACOCO.replace('covered="1"/></sourcefile>', 'covered="NaN"/></sourcefile>')),
    "float_count_jacoco": ("jacoco", JACOCO.replace('missed="1" covered="1"/><counter type="BRANCH"', 'missed="1.5" covered="1"/><counter type="BRANCH"')),
    "negative_branch_line_jacoco": ("jacoco", JACOCO.replace('<line nr="1" mi="0" ci="3" mb="1" cb="1"/>', '<line nr="1" mi="0" ci="3" mb="-1" cb="1"/>')),
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_coverage_parsers_refuse_hostile_input(name):
    fmt, text = HOSTILE[name]
    with pytest.raises(ingest.ReportInvalid) as e:
        parse(fmt, text)
    assert e.value.details["detail"] != "unknown_format"


@pytest.mark.parametrize("fmt", sorted(VALID))
def test_coverage_parsers_refuse_an_oversize_report(tmp_path, fmt):
    path = tmp_path / "r"
    path.write_text(VALID[fmt])
    with pytest.raises(ingest.ReportInvalid) as e:
        ingest.parse_report(fmt, path, 10)
    assert e.value.details["detail"] == "too_large"


def test_a_deeply_nested_cobertura_does_not_crash():
    text = "<coverage>" + "<packages>" * 5000 + "</packages>" * 5000 + "</coverage>"
    try:
        s = parse("cobertura", text)
    except ingest.ReportInvalid:
        return
    assert s.files == ()


# --------------------------------------------------------------------------- paths stay as recorded (F12)

@pytest.mark.parametrize("path,outside", [
    ("src/a.py", False), ("./src/a.py", False), ("a/../a.py", True), ("../secret.py", True), ("..\\secret.py", True),
    ("src/../../x", True), ("..", True), ("a..b/c.py", False), ("src/..hidden/c.py", False)])
def test_outside_project_is_a_path_that_climbs(path, outside):
    assert ingest.outside_project(path) is outside


def test_an_absolute_path_is_outside_only_when_it_is_not_under_the_root():
    assert ingest.outside_project("/work/src/a.py", "/work") is False
    assert ingest.outside_project("/etc/passwd", "/work") is True
    assert ingest.outside_project("/work/../etc/passwd", "/work") is True
    assert ingest.outside_project("/elsewhere/a.py") is False  # no root to judge by: only `..` counts
    assert ingest.outside_project("C:\\other\\a.py", "/work") is True


@pytest.mark.parametrize("fmt,text", [
    ("lcov", "SF:..\\up\\a.py\nDA:1,1\nend_of_record\n"),
    ("cobertura", COBERTURA.replace("src/a.py", "..\\up\\a.py")),
    ("jacoco", JACOCO.replace('package name="com/x"', 'package name="../up"').replace("A.java", "a.py"))])
def test_a_path_with_dots_is_recorded_not_refused_and_flagged(fmt, text):
    s = parse(fmt, text)
    assert len(s.files) == 1 and s.files[0].path == "../up/a.py"  # given as recorded, normalised to "/"
    assert s.anomalies == ("path_outside_project",)


def test_a_repeated_lcov_file_merges_by_path_not_by_adding():
    s = parse("lcov", "SF:a.py\nDA:1,1\nDA:2,0\nend_of_record\nSF:a.py\nDA:1,0\nDA:2,5\nDA:3,0\nend_of_record\nSF:b.py\nDA:1,1\nend_of_record\n")
    assert [f.path for f in s.files] == ["a.py", "b.py"] and totals(s) == (2, 4, 3, 0, 0)


def test_paths_that_stay_inside_are_not_flagged():
    assert parse("lcov", LCOV).anomalies == ()


# --------------------------------------------------------------------------- runs

LCOV_ZERO = "SF:src/a.py\nDA:1,0\nLF:1\nLH:0\nend_of_record\n"
WRITER = ("import sys, pathlib\n"
          "*pairs, code = sys.argv[1:]\n"
          "for i in range(0, len(pairs), 2): pathlib.Path(pairs[i]).write_text(pairs[i + 1])\n"
          "sys.exit(int(code))")


def run(ws, actor, py, files, exit_code, reports, **kw):
    args = [x for pair in files for x in pair] + [str(exit_code)]
    return execution.execute(ws, actor, execution.RunRequest(command=py.argv(WRITER, *args), reports=tuple(reports), **kw))


def record(ws, result):
    return state.load(ws)["executions"][result["execution"]]


def test_measurement_report_never_decides_status(trusted, actor, py):
    junit_failed = '<testsuite><testcase classname="c" name="t"><failure/></testcase></testsuite>'
    junit_passed = '<testsuite><testcase classname="c" name="t"/></testsuite>'
    reports = (("junit", "{evidence}/junit.xml"), ("lcov", "{evidence}/cov.info"))
    files = lambda xml, cov: (("{evidence}/junit.xml", xml), ("{evidence}/cov.info", cov))  # noqa: E731
    failed = run(trusted, actor, py, files(junit_failed, LCOV), 1, reports)
    assert (failed["status"], failed["reason"], failed["strength"]) == ("failed", "assertion", "structured")
    passed = run(trusted, actor, py, files(junit_passed, LCOV_ZERO), 0, reports)  # 0% coverage is not a failure
    assert (passed["status"], passed["strength"]) == ("passed", "structured") and passed["tests"]["total"] == 1
    assert [m["totals"]["lines_covered"] for m in record(trusted, passed)["measurements"]] == [0]
    assert [m["totals"]["lines_covered"] for m in record(trusted, failed)["measurements"]] == [1]


def test_measurement_only_run_uses_exit_code_rules(trusted, actor, py):
    reports = (("lcov", "{evidence}/cov.info"),)
    ok = run(trusted, actor, py, (("{evidence}/cov.info", LCOV),), 0, reports)
    assert (ok["status"], ok["reason"], ok["strength"]) == ("passed", None, "exit_code")
    crashed = run(trusted, actor, py, (("{evidence}/cov.info", LCOV),), 3, reports)
    assert (crashed["status"], crashed["reason"], crashed["strength"]) == ("failed", "nonzero_exit", "exit_code")
    assert record(trusted, crashed)["measurements"][0]["totals"]["files"] == 1  # a crash stays visible, and so does the number


@pytest.mark.parametrize("files,reason", [((), "report_missing"), ((("{evidence}/cov.info", "not a coverage report\n"),), "report_invalid")])
def test_missing_or_unparseable_declared_coverage_is_inconclusive(trusted, actor, py, files, reason):
    r = run(trusted, actor, py, files, 0, (("lcov", "{evidence}/cov.info"),))
    assert (r["status"], r["reason"]) == ("inconclusive", reason)
    assert "measurements" not in record(trusted, r)


JUNIT_OK = '<testsuite><testcase classname="c" name="t"/></testsuite>'
JUNIT_BAD = '<testsuite><testcase classname="c" name="t"><failure/></testcase></testsuite>'


@pytest.mark.parametrize("junit,exit_code,coverage,status,reason,note", [
    (JUNIT_BAD, 1, None, "failed", "assertion", "measurement_missing"),
    (JUNIT_OK, 0, None, "passed", None, "measurement_missing"),
    (JUNIT_OK, 0, "garbage\n", "passed", None, "measurement_invalid"),
    (JUNIT_BAD, 1, "garbage\n", "failed", "assertion", "measurement_invalid")])
def test_a_bad_measurement_beside_test_reports_never_changes_the_verdict(trusted, actor, py, junit, exit_code, coverage, status, reason, note):
    files = (("{evidence}/junit.xml", junit),) + ((("{evidence}/cov.info", coverage),) if coverage else ())
    r = run(trusted, actor, py, files, exit_code, (("junit", "{evidence}/junit.xml"), ("lcov", "{evidence}/cov.info")))
    assert (r["status"], r["reason"]) == (status, reason)
    assert any(item.startswith(note) for item in r["limitations"]) and "measurements" not in record(trusted, r)
    assert [e["error"] for e in record(trusted, r)["reports"]] == [None, "report_missing" if coverage is None else "report_invalid"]


def test_missing_tests_beside_coverage_is_report_missing(trusted, actor, py):
    r = run(trusted, actor, py, (("{evidence}/cov.info", LCOV),), 0, (("junit", "{evidence}/junit.xml"), ("lcov", "{evidence}/cov.info")))
    assert (r["status"], r["reason"]) == ("inconclusive", "report_missing")


def test_a_run_may_declare_tests_and_coverage_but_not_tests_and_alerts():
    with pytest.raises(QaError) as e:
        execution.execute(None, None, execution.RunRequest(command=["x"], reports=(("junit", "a"), ("sarif", "b"))))
    assert e.value.code == "kind_mismatch"


def test_the_execution_event_lists_each_measurement(trusted, actor, py):
    # No DOCTYPE here: a URL in a command line is a network use to the guard, and the real DOCTYPEs are in the corpus.
    r = run(trusted, actor, py, (("{evidence}/cov.xml", COBERTURA.replace(COBERTURA_DOCTYPE, "")),
                                 ("{evidence}/jac.xml", JACOCO.replace(JACOCO_DOCTYPE, ""))), 0,
            (("cobertura", "{evidence}/cov.xml"), ("jacoco", "{evidence}/jac.xml")))
    rec = record(trusted, r)
    assert (r["status"], r["reason"]) == ("passed", None), r["limitations"]
    assert [(m["format"], m["kind"], m["file"]) for m in rec["measurements"]] == [
        ("cobertura", "coverage", "reports/cov.xml"), ("jacoco", "coverage", "reports/jac.xml")]
    assert rec["measurements"][0]["totals"] == {"files": 1, "lines_total": 3, "lines_covered": 2, "branches_total": 2, "branches_covered": 1}
    manifest = rec["evidence_manifest"]
    assert set(rec["measurements"][0]) == {"format", "kind", "file", "sha256", "totals"}
    assert rec["measurements"][0]["file"] in manifest["files"] and rec["report"] is None and r["tests"]["total"] == 0
    assert r["measurements"][0]["totals"] == rec["measurements"][0]["totals"]


def test_a_run_without_measurements_records_none_not_an_empty_list(trusted, actor, py):
    r = execution.execute(trusted, actor, execution.RunRequest(command=py.argv(WRITE_JUNIT, "{evidence}/junit.xml", "pass"),
                                                               reports=(("junit", "{evidence}/junit.xml"),)))
    assert "measurements" not in record(trusted, r) and "measurements" not in r


def test_a_path_outside_the_project_is_flagged_on_the_measurement(trusted, actor, py):
    r = run(trusted, actor, py, (("{evidence}/cov.info", "SF:../x.py\nDA:1,1\nend_of_record\n"),), 0, (("lcov", "{evidence}/cov.info"),))
    assert r["status"] == "passed" and record(trusted, r)["measurements"][0]["anomalies"] == ["path_outside_project"]
    root = str(trusted.root)
    inside = run(trusted, actor, py, (("{evidence}/cov.info", "SF:%s/a.py\nDA:1,1\nend_of_record\n" % root),), 0, (("lcov", "{evidence}/cov.info"),))
    elsewhere = run(trusted, actor, py, (("{evidence}/cov.info", "SF:/elsewhere/a.py\nDA:1,1\nend_of_record\n"),), 0, (("lcov", "{evidence}/cov.info"),))
    assert "anomalies" not in record(trusted, inside)["measurements"][0]
    assert record(trusted, elsewhere)["measurements"][0]["anomalies"] == ["path_outside_project"]


@pytest.mark.parametrize("exit_code,status", [(0, "passed"), (1, "failed")])
def test_a_measurement_is_readable_from_a_passed_or_failed_run(trusted, actor, py, exit_code, status):
    r = run(trusted, actor, py, (("{evidence}/cov.info", LCOV),), exit_code, (("lcov", "{evidence}/cov.info"),))
    assert r["status"] == status and len(execution.readable_measurements(record(trusted, r))) == 1


@pytest.mark.parametrize("status", ["blocked", "inconclusive", "not_run"])
def test_a_measurement_is_not_readable_from_any_other_status(status):
    rec = {"status": status, "measurements": [{"format": "lcov"}]}
    assert execution.readable_measurements(rec) == []
    assert execution.readable_measurements({"status": "passed"}) == []
    assert execution.readable_measurements({"status": "failed", "measurements": [{"format": "lcov"}]}) == [{"format": "lcov"}]


# --------------------------------------------------------------------------- qa ingest

@pytest.mark.parametrize("fmt", sorted(VALID))
def test_ingest_coverage_from_an_execution(trusted, actor, py, tmp_path, fmt):
    r = execution.execute(trusted, actor, execution.RunRequest(command=py.argv("pass")))
    path = tmp_path / "cov"
    path.write_text(VALID[fmt])
    out = execution.ingest_file(trusted, actor, fmt, path, from_execution=r["execution"], category="functional")
    assert (out["kind"], out["total"], out["strength"]) == ("coverage", 1, "attested")
    item = record(trusted, r)["ingested"][0]
    assert (item["format"], item["kind"], item["category"]) == (fmt, "coverage", "functional")
    assert item["totals"]["lines_total"] > 0 and (trusted.path("evidence", r["execution"]) / item["file"]).is_file()
    assert record(trusted, r)["status"] == "passed"  # ingesting a measurement never changes a run's status


def test_cli_ingests_lcov_and_refuses_a_hostile_file(trusted, qa_cli, tmp_path):
    exe = qa_cli("run", "--", "true")[1]["execution"]
    good, bad = tmp_path / "a.info", tmp_path / "b.xml"
    good.write_text(LCOV)
    bad.write_text(ENTITY)
    code, out = qa_cli("ingest", "lcov", str(good), "--from", exe)
    assert (code, out["kind"], out["total"]) == (0, "coverage", 1)
    code, out = qa_cli("ingest", "cobertura", str(bad), "--from", exe)
    assert (code, out["error"]["code"], out["error"]["details"]["detail"]) == (2, "report_invalid", "doctype_or_entity")


def test_cli_run_takes_coverage_reports_beside_test_reports(trusted, qa_cli):
    code, out = qa_cli("run", "--report", "lcov:{evidence}/cov.info", "--", sys.executable, "-c",
                       "import sys; open(sys.argv[1], 'w').write(%r)" % LCOV, "{evidence}/cov.info")
    assert (code, out["status"], out["strength"]) == (0, "passed", "exit_code")
