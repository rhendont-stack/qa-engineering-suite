from __future__ import annotations

import json

import pytest

from helpers import base
from qa import findings, report, state
from qa.canonical import loads
from qa.report import sarif


@pytest.fixture
def st_with_findings(ws, actor):
    findings.add(ws, actor, base(title="IDOR on orders"))
    findings.add(ws, actor, base(title="Weak hash", severity_rationale={"anchor": "SEC-M1", "modifiers": []},
                                 component={"path": "app/auth.py", "criticality": "normal"},
                                 refs={"rule": "weak-hash", "location": "app/auth.py::digest"}))
    return state.load(ws)


def test_sarif_output_has_only_recorded_findings(st_with_findings):
    doc = loads(sarif.render_sarif(st_with_findings))
    assert doc["version"] == "2.1.0" and len(doc["runs"]) == 1
    results = doc["runs"][0]["results"]
    assert [r["properties"]["id"] for r in results] == sorted(st_with_findings["findings"])
    for r in results:
        record = st_with_findings["findings"][r["properties"]["id"]]
        assert r["message"]["text"].startswith(record["title"]) and r["partialFingerprints"]["qa/v1"] == record["fingerprint"]
    assert {r["id"] for r in doc["runs"][0]["tool"]["driver"]["rules"]} == {r["ruleId"] for r in results}


def test_sarif_of_an_empty_state_has_no_results():
    doc = loads(sarif.render_sarif(state.empty_state()))
    assert doc["runs"][0]["results"] == [] and doc["runs"][0]["tool"]["driver"]["rules"] == []


def test_sarif_is_byte_stable(st_with_findings):
    assert sarif.render_sarif(st_with_findings) == sarif.render_sarif(json.loads(json.dumps(st_with_findings)))
    assert sarif.render_sarif(st_with_findings).endswith(b"\n")


def test_a_dismissed_finding_is_suppressed_not_dropped(ws, actor):
    findings.add(ws, actor, base())
    st = state.load(ws)
    st["findings"]["FND-001"]["state"] = "accepted_risk"
    result = loads(sarif.render_sarif(st))["runs"][0]["results"][0]
    assert result["suppressions"][0]["kind"] == "external" and result["properties"]["state"] == "accepted_risk"


def test_render_writes_sarif_on_request_and_md_json_by_default(ws, actor, st_with_findings):
    event = report.render(ws, actor, formats=("sarif",))
    assert sorted(event["data"]["files"]) == ["report.sarif"]
    default = report.render(ws, actor)
    assert sorted(default["data"]["files"]) == ["report.json", "report.md"]


def test_cli_renders_sarif(ws, actor, qa_cli, st_with_findings):
    code, out = qa_cli("report", "render", "--format", "sarif")
    assert code == 0 and list(out["files"]) == ["report.sarif"]
