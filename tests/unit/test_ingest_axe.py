"""axe-core results: one WCAG-tagged alert per (violation, node); incomplete checks are notes; passes are only counted."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from qa import execution, ingest, schema, state

CORPUS = Path(__file__).resolve().parents[1] / "corpus" / "axe" / "axe-core-4.14.0-fixture-page.json"
MANIFEST = json.loads((Path(__file__).resolve().parents[1] / "corpus" / "MANIFEST.json").read_text(encoding="utf-8"))


def node(target=("#a",), impact="serious", **over):
    return dict({"html": "<img src=\"x\">", "target": target if isinstance(target, str) else list(target), "impact": impact, "any": [], "all": [], "none": []}, **over)


def rule(rule_id="image-alt", impact="critical", tags=("cat.text-alternatives", "wcag2a", "wcag111"), nodes=None, **over):
    return dict({"id": rule_id, "impact": impact, "tags": list(tags), "description": "d", "help": "Images must have alternative text",
                 "helpUrl": "https://dequeuniversity.com/rules/axe/4.14/image-alt", "nodes": nodes if nodes is not None else [node()]}, **over)


def results(violations=(), incomplete=(), passes=(), inapplicable=(), url="http://127.0.0.1:8000/", **over):
    return dict({"testEngine": {"name": "axe-core", "version": "4.14.0"}, "testRunner": {"name": "axe"},
                 "url": url, "timestamp": "2026-11-13T00:00:00.000Z", "violations": list(violations),
                 "incomplete": list(incomplete), "passes": list(passes), "inapplicable": list(inapplicable)}, **over)


def parse(doc):
    return ingest.parse_bytes("axe", json.dumps(doc).encode("utf-8") if not isinstance(doc, bytes) else doc)


def tags_of(alert):
    return set(alert.tags)


def alert_with(tag_name):
    return parse(results([rule(tags=("wcag2aa", tag_name))])).alerts[0]


def test_axe_corpus_counts_match_the_manifest():
    entry = next(e for e in MANIFEST if e["file"] == "axe/axe-core-4.14.0-fixture-page.json")
    s = ingest.parse_report("axe", CORPUS, 10**7)
    assert (s.format, s.kind, s.anomalies) == ("axe", "alerts", ())
    assert s.tool_version == entry["version"] == "4.14.0"
    expected = entry["expected"]
    assert {"alerts": len(s.alerts), "runs": s.runs} == {"alerts": expected["alerts"], "runs": expected["runs"]}
    notes = [a for a in s.alerts if a.level == "note"]
    assert (len(s.alerts) - len(notes), len(notes)) == (expected["violations"], expected["incomplete"])
    assert (s.rules_passed, s.rules_inapplicable) == (expected["rules_passed"], expected["rules_inapplicable"])
    seeded = {t for a in s.alerts if a.level != "note" for t in a.tags if t.startswith("wcag-sc:")}
    assert {"wcag-sc:1.1.1", "wcag-sc:1.4.3"} <= seeded
    assert all(a.tool == "axe-core" and a.path == "/" for a in s.alerts)
    assert ingest.FORMAT_KINDS["axe"] == "alerts" and not ingest.is_measurement("axe")


def test_axe_tags_map_to_wcag_success_criteria():
    assert "wcag-sc:1.4.10" in tags_of(alert_with("wcag1410"))
    assert "wcag-sc:1.1.1" in tags_of(alert_with("wcag111"))
    assert "wcag-sc:4.1.2" in tags_of(alert_with("wcag412"))


@pytest.mark.parametrize("tag, level", [("wcag2a", "A"), ("wcag21a", "A"), ("wcag2aa", "AA"), ("wcag21aa", "AA"), ("wcag22aa", "AA")])
def test_axe_conformance_tags_map_to_wcag_levels(tag, level):
    alert = parse(results([rule(tags=(tag,))])).alerts[0]
    assert "wcag-level:" + level in alert.tags and not any(t.startswith("wcag-sc:") for t in alert.tags)


def test_axe_best_practice_is_kept_and_other_tags_are_not_invented():
    alert = parse(results([rule(tags=("cat.aria", "best-practice"))])).alerts[0]
    assert alert.tags == ("best-practice",)


def test_axe_one_alert_per_violation_and_node_with_the_impact_as_level():
    doc = results([rule("image-alt", "critical", nodes=[node(("#a",), "critical"), node(("#b",), "serious")]),
                   rule("color-contrast", "serious", tags=("wcag2aa", "wcag143"), nodes=[node(("#c", "span"), "moderate")]),
                   rule("region", "minor", tags=("best-practice",), nodes=[node(("html",), "minor")])])
    s = parse(doc)
    assert [(a.rule_id, a.level) for a in s.alerts] == [("image-alt", "error"), ("image-alt", "error"), ("color-contrast", "warning"),
                                                       ("region", "warning")]
    assert [a.partial_fingerprints["target"] for a in s.alerts] == ["#a", "#b", "#c > span", "html"]
    assert s.runs == 1 and s.alerts[0].message == "Images must have alternative text"
    assert all(a.security_severity is None and a.start_line is None for a in s.alerts)


def test_axe_target_chain_is_capped_at_500_characters():
    s = parse(results([rule(nodes=[node(("a" * 400, "b" * 400))])]))
    assert len(s.alerts[0].partial_fingerprints["target"]) == 500


def test_axe_incomplete_is_a_note_needing_review():
    s = parse(results(incomplete=[rule("color-contrast", None, tags=("wcag2aa", "wcag143"), nodes=[node(("#c",), None)])]))
    (alert,) = s.alerts
    assert (alert.level, alert.rule_id) == ("note", "color-contrast")
    assert "needs-review" in alert.tags and "wcag-sc:1.4.3" in alert.tags


def test_axe_passes_and_inapplicable_are_only_counted():
    s = parse(results(passes=[rule("a"), rule("b")], inapplicable=[rule("c")]))
    assert (len(s.alerts), s.rules_passed, s.rules_inapplicable) == (0, 2, 1)


def test_axe_url_loses_query_fragment_and_credentials():
    s = parse(results([rule(nodes=[node((".a",))])], url="http://user:pw@127.0.0.1:8000/page/one?token=abc#frag"))
    path = s.alerts[0].path
    assert path == "/page/one" and s.alerts[0].partial_fingerprints["url"] == "/page/one"
    for text in (path, json.dumps(s.alerts[0].partial_fingerprints)):
        assert "user:pw@" not in text and "?" not in text and "#" not in text and "token" not in text


def test_axe_url_that_is_not_a_url_gives_no_path():
    assert parse(results([rule()], url="user:pw@host/p?x#y")).alerts[0].path is None


def test_axe_array_of_results_counts_each_run():
    s = parse([results([rule()], url="http://127.0.0.1:8000/a"), results([rule()], url="http://127.0.0.1:8000/b")])
    assert (s.runs, [a.path for a in s.alerts]) == (2, ["/a", "/b"])


HOSTILE = {
    "not_axe": ({"violations": []}, "not_axe_results"),
    "other_engine": (results(testEngine={"name": "other", "version": "1"}), "not_axe_results"),
    "version_not_text": (results(testEngine={"name": "axe-core", "version": 4}), "not_axe_results"),
    "violations_not_a_list": (dict(results(), violations={}), "not_axe_results"),
    "empty_array": ([], "not_axe_results"),
    "not_an_object": ("axe", "not_axe_results"),
    "element_not_axe": ([results(), {"x": 1}], "not_axe_results"),
    "impact_not_text": (results([rule(nodes=[node(impact=3)])]), "invalid_node"),
    "impact_unknown": (results([rule(nodes=[node(impact="catastrophic")])]), "invalid_node"),
    "violation_without_impact": (results([rule(impact=None, nodes=[node(impact=None)])]), "invalid_node"),
    "node_not_an_object": (results([rule(nodes=["x"])]), "invalid_node"),
    "target_not_a_list": (results([rule(nodes=[node(target="#a")])]), "invalid_node"),
    "target_item_not_text": (results([rule(nodes=[dict(node(), target=[1])])]), "invalid_node"),
    "rule_id_not_text": (results([rule(rule_id=7)]), "invalid_node"),
    "tags_not_text": (results([rule(tags=(3,))]), "invalid_node"),
    "rule_id_600_characters": (results([rule(rule_id="r" * 600)]), "invalid_name"),
    "ten_thousand_and_one_nodes": (results([rule(nodes=[node()] * 10001)]), "too_many_alerts"),
    "ten_thousand_and_one_across_lists": (results([rule(nodes=[node()] * 5000)], incomplete=[rule(nodes=[node()] * 5001)]),
                                          "too_many_alerts"),
}


@pytest.mark.parametrize("hostile", HOSTILE.values(), ids=HOSTILE)
def test_axe_parser_refuses_hostile_input(hostile):
    doc, detail = hostile
    with pytest.raises(ingest.ReportInvalid) as refused:
        parse(doc)
    assert refused.value.details["detail"] == detail


def test_axe_limits_are_inclusive():
    assert len(parse(results([rule(rule_id="r" * 200, nodes=[node()] * 10000)])).alerts) == 10000


def test_axe_json_that_is_not_json_is_refused():
    with pytest.raises(ingest.ReportInvalid) as refused:
        parse(b"{not json")
    assert refused.value.details["detail"] == "invalid_json"


# --------------------------------------------------------------------------- run and ingest

def test_axe_run_records_the_report_block_with_counts_and_version(trusted, actor, tmp_path):
    source = tmp_path / "axe.json"
    source.write_text(json.dumps(results([rule()], passes=[rule("p")], inapplicable=[rule("i"), rule("j")])))
    program = tmp_path / "axetool"
    program.write_text("#!/bin/sh\ncp '%s' \"$1\"\n" % source)
    program.chmod(0o755)
    ran = execution.execute(trusted, actor, execution.RunRequest(command=[str(program), "{evidence}/axe.json"],
                                                                 reports=(("axe", "{evidence}/axe.json"),), category="accessibility"))
    record = state.load(trusted)["executions"][ran["execution"]]
    assert record["report"] == {"format": "axe", "kind": "alerts", "alerts": 1, "runs": 1, "tool_version": "4.14.0",
                                "rules_passed": 1, "rules_inapplicable": 2}
    schema.validate({key: value for key, value in record.items() if key != "ingested"}, "events/execution.recorded.schema.json")


def test_cli_ingests_axe_results_as_accessibility_findings(trusted, actor, tmp_path, qa_cli):
    program = tmp_path / "tool"
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(0o755)
    code, ran = qa_cli("run", "--", str(program))
    assert code == 0
    code, attached = qa_cli("ingest", "axe", str(CORPUS), "--from", ran["execution"])
    assert code == 0 and attached["kind"] == "alerts" and attached["findings"]["opened"] > 0
    findings = state.load(trusted)["findings"]
    assert findings and {f["category"] for f in findings.values()} == {"accessibility"}
