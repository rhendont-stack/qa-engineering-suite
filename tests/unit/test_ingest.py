from __future__ import annotations

import json

import pytest

from qa import ingest
from qa.errors import QaError


def write(tmp_path, text, name="r.xml"):
    p = tmp_path / name
    p.write_text(text)
    return p


def parse_junit(tmp_path, text):
    return ingest.parse_report("junit", write(tmp_path, text), 10**6)


def parse_ctrf(tmp_path, tests):
    return ingest.parse_report("ctrf", write(tmp_path, json.dumps({"results": {"tests": tests}}), "r.json"), 10**6)


def test_junit_counts_elements_not_attributes(tmp_path):
    s = ingest.parse_report("junit", write(tmp_path, '<testsuite tests="99"><testcase classname="c" name="a"/><testcase classname="c" name="b"/></testsuite>'), 10**6)
    assert (s.total, s.passed) == (2, 2)


@pytest.mark.parametrize("prolog", ['<!DOCTYPE x [<!ENTITY a "b">]>', '<!ENTITY a "b">'])
def test_junit_refuses_doctype_and_entities(tmp_path, prolog):
    with pytest.raises(ingest.ReportInvalid):
        ingest.parse_report("junit", write(tmp_path, prolog + "<testsuite/>"), 10**6)


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_junit_refuses_doctype_in_utf16(tmp_path, encoding):
    text = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY a "b">]><testsuite/>'
    p = tmp_path / "r.xml"
    p.write_bytes(text.encode(encoding))
    with pytest.raises(ingest.ReportInvalid):
        ingest.parse_report("junit", p, 10**6)


def test_junit_refuses_doctype_with_declared_latin1(tmp_path):
    text = '<?xml version="1.0" encoding="iso-8859-1"?><!DOCTYPE x SYSTEM "x.dtd"><testsuite/>'
    p = tmp_path / "r.xml"
    p.write_bytes(text.encode("iso-8859-1"))
    with pytest.raises(ingest.ReportInvalid):
        ingest.parse_report("junit", p, 10**6)


def test_junit_malformed_xml_is_report_invalid(tmp_path):
    with pytest.raises(ingest.ReportInvalid):
        parse_junit(tmp_path, "<testsuite><testcase")


def test_junit_outcomes(tmp_path):
    s = ingest.parse_report("junit", write(tmp_path, '<testsuites><testsuite><testcase classname="c" name="f"><failure message="m"/></testcase>'
        '<testcase classname="c" name="e"><error/></testcase><testcase classname="c" name="s"><skipped message="why"/></testcase></testsuite></testsuites>'), 10**6)
    assert [(c.status, c.failure_kind, c.skip_reason) for c in s.cases] == [("failed", "assertion", None), ("failed", "error", None), ("not_run", None, "why")]


def test_junit_counts_split_failures_and_errors(tmp_path):
    s = parse_junit(tmp_path, '<testsuite><testcase classname="c" name="a"/><testcase classname="c" name="f"><failure/></testcase>'
                              '<testcase classname="c" name="e"><error/></testcase><testcase classname="c" name="s"><skipped/></testcase></testsuite>')
    assert (s.total, s.passed, s.failed, s.errors, s.skipped) == (4, 1, 1, 1, 1)


def test_junit_missing_name_is_partial(tmp_path):
    s = ingest.parse_report("junit", write(tmp_path, '<testsuite><testcase classname="c"/></testsuite>'), 10**6)
    assert s.cases[0].test_id == "c::#0" and "partial" in s.anomalies


def test_junit_index_counts_all_testcases_in_document_order(tmp_path):
    s = parse_junit(tmp_path, '<testsuites><testsuite name="s1"><testcase classname="c" name="a"/></testsuite>'
                              '<testsuite name="s2"><testcase classname="c"/><testcase classname="c" name="z"/><testcase classname="d"/></testsuite></testsuites>')
    assert [c.test_id for c in s.cases] == ["c::a", "c::#1", "c::z", "d::#3"]


def test_junit_missing_classname_falls_back_to_suite_name_in_test_id_only(tmp_path):
    s = parse_junit(tmp_path, '<testsuite name="suite1"><testcase name="a"/></testsuite>')
    assert (s.cases[0].test_id, s.cases[0].classname) == ("suite1::a", None)
    s = parse_junit(tmp_path, '<testsuite><testcase name="a"/></testsuite>')
    assert (s.cases[0].test_id, s.cases[0].classname) == ("::a", None)


def test_junit_failure_outranks_error_outranks_skipped(tmp_path):
    s = parse_junit(tmp_path, '<testsuite><testcase classname="c" name="a"><skipped/><error/><failure/></testcase>'
                              '<testcase classname="c" name="b"><skipped message="x"/><error/></testcase>'
                              '<testcase classname="c" name="c"><skipped message="y"/></testcase></testsuite>')
    assert [(c.status, c.failure_kind, c.skip_reason) for c in s.cases] == [
        ("failed", "assertion", None), ("failed", "error", None), ("not_run", None, "y")]


def test_surefire_flaky_and_rerun(tmp_path):
    s = ingest.parse_report("junit", write(tmp_path, '<testsuite><testcase classname="c" name="a"><flakyFailure/></testcase>'
        '<testcase classname="c" name="b"><failure/><rerunFailure/></testcase></testsuite>'), 10**6)
    assert [(c.status, c.flaky) for c in s.cases] == [("passed", True), ("failed", False)]
    assert "retried_in_framework" in s.anomalies


def test_report_too_large(tmp_path):
    with pytest.raises(ingest.ReportInvalid) as e:
        ingest.parse_report("junit", write(tmp_path, "<testsuite/>"), 5)
    assert e.value.code == "report_invalid" and e.value.details["detail"] == "too_large"


def test_unknown_format_is_refused(tmp_path):
    with pytest.raises(ingest.ReportInvalid):
        ingest.parse_report("tap", write(tmp_path, ""), 10**6)


def test_ctrf_mapping(tmp_path):
    doc = {"results": {"tests": [{"name": "a", "status": "passed", "retries": 1}, {"name": "b", "status": "pending"},
                                 {"name": "c", "status": "other"}, {"name": "d", "status": "failed"}]}}
    s = ingest.parse_report("ctrf", write(tmp_path, json.dumps(doc), "r.json"), 10**6)
    assert [(c.status, c.flaky) for c in s.cases] == [("passed", True), ("not_run", False), ("not_run", False), ("failed", False)]
    assert "retried_in_framework" in s.anomalies


def test_ctrf_test_ids_and_failure_kind(tmp_path):
    s = parse_ctrf(tmp_path, [
        {"name": "a", "status": "failed", "filePath": "t/a.spec.js", "suite": "S"},
        {"name": "b", "status": "passed", "filePath": "", "suite": "S"},
        {"name": "c", "status": "passed", "filePath": 7},
        {"name": "d", "status": "skipped"},
    ])
    assert [c.test_id for c in s.cases] == ["t/a.spec.js::a", "S::b", "::c", "::d"]
    assert [(c.status, c.failure_kind) for c in s.cases] == [("failed", "assertion"), ("passed", None), ("passed", None), ("not_run", None)]
    assert (s.failed, s.errors, s.skipped) == (1, 0, 1)


def test_ctrf_missing_name_is_partial(tmp_path):
    s = parse_ctrf(tmp_path, [{"name": "a", "status": "passed"}, {"status": "passed", "filePath": "f"}])
    assert s.cases[1].test_id == "f::#1" and "partial" in s.anomalies


def test_ctrf_unknown_status_is_not_run_and_partial(tmp_path):
    s = parse_ctrf(tmp_path, [{"name": "a", "status": "weird"}, {"name": "b"}])
    assert [c.status for c in s.cases] == ["not_run", "not_run"]
    assert s.passed == 0 and "partial" in s.anomalies


def test_ctrf_flaky_flag(tmp_path):
    s = parse_ctrf(tmp_path, [{"name": "a", "status": "passed", "flaky": True}, {"name": "b", "status": "failed", "retries": 2}])
    assert [(c.status, c.flaky) for c in s.cases] == [("passed", True), ("failed", False)]


def test_ctrf_not_json_object_is_report_invalid(tmp_path):
    with pytest.raises(ingest.ReportInvalid):
        ingest.parse_report("ctrf", write(tmp_path, "[1]", "r.json"), 10**6)
    with pytest.raises(ingest.ReportInvalid):
        ingest.parse_report("ctrf", write(tmp_path, "{not json", "r.json"), 10**6)


def test_sarif_level_and_severity_fallbacks(tmp_path):
    doc = {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "t", "rules": [
        {"id": "R1", "defaultConfiguration": {"level": "error"}, "properties": {"security-severity": "7.5"}}, {"id": "R2"}]}},
        "results": [{"ruleId": "R1", "message": {"text": "m"}, "locations": [{"physicalLocation": {"artifactLocation": {"uri": "a.py"},
                     "region": {"startLine": 3, "snippet": {"text": "SECRET"}}}}]}, {"ruleId": "R2", "message": {"text": "n"}}]}]}
    s = ingest.parse_report("sarif", write(tmp_path, json.dumps(doc), "r.sarif"), 10**6)
    assert [(a.level, a.security_severity) for a in s.alerts] == [("error", 7.5), ("warning", None)]
    assert s.runs == 1 and "SECRET" not in repr(s)
    assert (s.alerts[0].path, s.alerts[0].start_line, s.alerts[0].tool) == ("a.py", 3, "t")


def test_sarif_result_properties_override_rule(tmp_path):
    doc = {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "t", "rules": [
        {"id": "R1", "defaultConfiguration": {"level": "note"}, "properties": {"security-severity": "1.0"}}]}},
        "results": [{"ruleId": "R1", "level": "error", "message": {"text": "m"}, "properties": {"security-severity": 9.1, "tags": ["x"]},
                     "partialFingerprints": {"k": "v"}}]}]}
    a = ingest.parse_report("sarif", write(tmp_path, json.dumps(doc), "r.sarif"), 10**6).alerts[0]
    assert (a.level, a.security_severity, a.tags, a.partial_fingerprints) == ("error", 9.1, ("x",), {"k": "v"})


def test_sarif_wrong_version_or_shape_is_report_invalid(tmp_path):
    with pytest.raises(ingest.ReportInvalid):
        ingest.parse_report("sarif", write(tmp_path, json.dumps({"version": "2.1.0"}), "r.sarif"), 10**6)


def test_merge_sums_and_refuses_mixed_kinds(tmp_path):
    a = ingest.parse_report("junit", write(tmp_path, '<testsuite><testcase classname="c" name="a"/><testcase classname="c" name="b"><failure/></testcase></testsuite>', "a.xml"), 10**6)
    b = ingest.parse_report("junit", write(tmp_path, '<testsuite><testcase classname="d" name="e"><error/></testcase><testcase classname="d" name="s"><skipped/></testcase></testsuite>', "b.xml"), 10**6)
    m = ingest.merge([a, b])
    assert (m.kind, m.total, m.passed, m.failed, m.errors, m.skipped) == ("tests", 4, 1, 1, 1, 1)
    sarif = ingest.parse_report("sarif", write(tmp_path, json.dumps({"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "t"}}, "results": []}]}), "r.sarif"), 10**6)
    with pytest.raises(QaError) as e:
        ingest.merge([a, sarif])
    assert e.value.code == "kind_mismatch"


@pytest.mark.parametrize("encoding", ["shift_jis", "no-such-enc"])
def test_junit_undecodable_encoding_declaration_is_report_invalid(tmp_path, encoding):
    p = tmp_path / "r.xml"
    p.write_bytes(('<?xml version="1.0" encoding="%s"?><testsuite/>' % encoding).encode("ascii"))
    with pytest.raises(ingest.ReportInvalid):
        ingest.parse_report("junit", p, 10**6)


@pytest.mark.parametrize("value", ["inf", "1e400", "nan", "abc"])
def test_junit_bad_time_is_absent(tmp_path, value):
    s = parse_junit(tmp_path, '<testsuite><testcase classname="c" name="a" time="%s"/></testsuite>' % value)
    assert s.cases[0].duration_ms is None and s.total == 1


def test_junit_deep_nesting_does_not_crash(tmp_path):
    n = 2000
    text = "<testsuites>" + '<testsuite name="s">' * n + '<testcase classname="c" name="a"/>' + "</testsuite>" * n + "</testsuites>"
    s = parse_junit(tmp_path, text)
    assert s.total == 1


def sarif_with_severity(tmp_path, value):
    doc = {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "t"}}, "results": [
        {"ruleId": "R", "message": {"text": "m"}, "properties": {"security-severity": value}}]}]}
    return ingest.parse_report("sarif", write(tmp_path, json.dumps(doc), "r.sarif"), 10**6)


@pytest.mark.parametrize("value", ["NaN", "inf", "-inf", 10**400, "x"])
def test_sarif_bad_security_severity_is_absent(tmp_path, value):
    assert sarif_with_severity(tmp_path, value).alerts[0].security_severity is None


@pytest.mark.parametrize("doc", [
    {"version": "2.1.0", "runs": [5]},
    {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "t"}}, "results": 5}]},
    {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "t"}}}]},
    {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "t"}}, "results": None}]},
    {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "t"}}, "results": [1, None]}]},
])
def test_sarif_wrong_structure_is_not_a_clean_scan(tmp_path, doc):
    with pytest.raises(ingest.ReportInvalid) as e:
        ingest.parse_report("sarif", write(tmp_path, json.dumps(doc), "r.sarif"), 10**6)
    assert e.value.details["detail"] == "not_sarif_2_1_0"


def test_sarif_empty_results_list_is_a_clean_scan(tmp_path):
    doc = {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "t"}}, "results": []}]}
    s = ingest.parse_report("sarif", write(tmp_path, json.dumps(doc), "r.sarif"), 10**6)
    assert (len(s.alerts), s.runs) == (0, 1)
