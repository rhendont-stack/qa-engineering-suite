from __future__ import annotations

import json
import os
import re
import stat

import pytest

import test_compare
import test_records
import test_trace

from helpers import REPO, WRITE_JUNIT, base, req
from qa import execution, findings, gate, ledger, records, report, schema, state, trace
from qa.canonical import dumps, loads, sha256_bytes
from qa.errors import QaError
from qa.report import check_markdown, md_cell, render, render_json, render_markdown

RPT = "events/report.rendered.schema.json"
SHA = "sha256:" + "a" * 64
LIMITS = ("It cannot stop an agent that ignores the skills and runs commands directly. "
          "It can only refuse to count unrecorded results and flag the gap.",
          "It cannot verify that a human really approved a grant or a configuration change. "
          "It records the agent's quote of the approval. The host's own permission prompts remain the authoritative control.",
          "It cannot make pattern-matching a sandbox.",
          "It cannot make a single-agent session truly independent.",
          "It cannot tell that a .qa/ copied in outside git carries approvals nobody here gave.")


def run(ws, actor, py, outcome="fail", **kw):
    return execution.execute(ws, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", outcome, **kw))["execution"]


def kinds(text, st):
    return sorted(p.kind for p in check_markdown(text, st))


def section(md, name):
    """The lines of one ``## name`` section."""
    lines = md.splitlines()
    start = lines.index("## " + name) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return lines[start:end]


# --------------------------------------------------------------------------- the brief's tests

def test_same_state_renders_identical_bytes(trusted, actor, py):
    execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))
    a, b = report.render(trusted, actor), report.render(trusted, actor)
    for name in ("report.md", "report.json"):
        assert trusted.path("reports", a["data"]["id"], name).read_bytes() == trusted.path("reports", b["data"]["id"], name).read_bytes()


def test_markdown_injection_is_escaped(ws, actor):
    findings.add(ws, actor, base(title="x | y <script>"))
    md = report.render_markdown(state.load(ws)); row = next(l for l in md.splitlines() if l.startswith("| FND-001"))
    assert "\\|" in row and "&lt;script&gt;" in row and len(re.split(r"(?<!\\)\|", row)) == len(re.split(r"(?<!\\)\|", md.splitlines()[md.splitlines().index(row) - 2]))


def test_no_gate_no_verdict(ws):
    assert "No gate evaluation recorded; no verdict." in report.render_markdown(state.load(ws))


def test_rendered_report_passes_check(trusted, actor, py):
    execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "fail")); st = state.load(trusted)
    assert report.check_markdown(report.render_markdown(st), st) == []


def test_check_problem_kinds(trusted, actor, py):
    exe = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "fail"))["execution"]; st = state.load(trusted)
    text = f"All tests passed.\n{exe} passed.\nSee FND-999.\n```\nfailed in code is ignored\n```\n"
    assert sorted(p.kind for p in report.check_markdown(text, st)) == ["status_mismatch", "uncited_claim", "universal_claim", "unknown_id"]


def test_universal_claim_allowed_with_full_scope_pass(trusted, actor, py):
    exe = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass", scope="full"))["execution"]
    assert report.check_markdown(f"All tests passed ({exe}).\n", state.load(trusted)) == []


# --------------------------------------------------------------------------- the project name (T17-R1)

def test_project_name_heads_the_report(ws):
    st = state.load(ws)
    assert render_markdown(st, project="Acme").splitlines()[0] == "# QA report: Acme"
    assert render_json(st, project="Acme")["project"] == "Acme"


def test_absent_project_reads_unnamed(ws):
    st = state.load(ws)
    assert render_markdown(st).splitlines()[0] == "# QA report: unnamed project"
    assert render_json(st)["project"] == "unnamed project"


def test_project_name_is_escaped(ws):
    st = state.load(ws)
    assert render_markdown(st, project="a|b <i>").splitlines()[0] == "# QA report: a\\|b &lt;i&gt;"


def test_render_takes_the_project_from_the_config(trusted, actor):
    event = render(trusted, actor)
    first = trusted.path("reports", event["data"]["id"], "report.md").read_text(encoding="utf-8").splitlines()[0]
    name = json.loads(trusted.path("config.json").read_text(encoding="utf-8"))["project"]["name"]
    assert first == "# QA report: " + md_cell(name)
    data = loads(trusted.path("reports", event["data"]["id"], "report.json").read_bytes())
    assert data["project"] == json.loads(trusted.path("config.json").read_text(encoding="utf-8"))["project"]["name"]


# --------------------------------------------------------------------------- the verdict line (T17-R2, T17-R3)

def test_verdict_line_is_exact(trusted, actor, py):
    run(trusted, actor, py)
    g = gate.evaluate(trusted, actor, profile="change")["data"]
    st = state.load(trusted)
    expected = "**Verdict:** %s (%s, profile %s, independence %s, state %s)" % (
        g["verdict"], g["id"], g["profile"], g["independence"], g["state_hash"])
    assert expected in render_markdown(st).splitlines()
    assert "_" in g["verdict"]  # NOT_READY or INSUFFICIENT_EVIDENCE: the underscore is not escaped


def test_verdict_line_uses_the_highest_gate_number(trusted, actor):
    gate.evaluate(trusted, actor, profile="change")
    second = gate.evaluate(trusted, actor, profile="release")["data"]
    st = state.load(trusted)
    st["gates"]["GATE-1000"] = dict(st["gates"]["GATE-002"], id="GATE-1000", profile="audit")
    st["gates"]["GATE-999"] = dict(st["gates"]["GATE-002"], id="GATE-999", profile="fix")
    line = next(l for l in render_markdown(st).splitlines() if l.startswith("**Verdict:**"))
    assert "GATE-1000, profile audit" in line and second["id"] not in line


def test_json_names_the_latest_gate(trusted, actor):
    g = gate.evaluate(trusted, actor, profile="change")["data"]
    data = render_json(state.load(trusted))
    assert data["verdict"] == g["verdict"] and data["gate"]["id"] == g["id"] and data["conditions"] == g["conditions"]
    assert render_json(state.empty_state())["verdict"] is None


# --------------------------------------------------------------------------- the headline (T17-R5)

def _with_gate(trusted, actor):
    gate.evaluate(trusted, actor, profile="change")
    return state.load(trusted)


def test_no_headline_for_a_clean_state(trusted, actor):
    st = _with_gate(trusted, actor)
    for criterion in st["gates"]["GATE-001"]["criteria"]:
        if criterion["type"] == "test_integrity":
            criterion["outcome"] = "MET"
    assert "Integrity failure" not in render_markdown(st)


def test_violations_make_the_headline(ws):
    st = state.load(ws)
    st["violations"].append({"event": "EVT-" + "0" * 26, "kind": "duplicate_id"})
    lines = render_markdown(st).splitlines()
    assert any(l.startswith("**Integrity failure:**") and l.endswith("; see Integrity.") for l in lines)


def test_a_test_integrity_failure_makes_the_headline_directly_above_the_verdict(trusted, actor):
    st = _with_gate(trusted, actor)
    for criterion in st["gates"]["GATE-001"]["criteria"]:
        if criterion["type"] == "test_integrity":
            criterion["outcome"] = "NOT_MET"
    lines = render_markdown(st).splitlines()
    verdict = next(i for i, l in enumerate(lines) if l.startswith("**Verdict:**"))
    assert lines[verdict - 1].startswith("**Integrity failure:**") and lines[verdict - 1].endswith("; see Integrity.")


def test_the_headline_stands_without_a_gate(ws):
    st = state.load(ws)
    st["violations"].append({"event": "EVT-" + "0" * 26, "kind": "unknown_type"})
    lines = render_markdown(st).splitlines()
    i = next(i for i, l in enumerate(lines) if l.startswith("**Integrity failure:**"))
    assert lines[i + 1] == "No gate evaluation recorded; no verdict."


# --------------------------------------------------------------------------- sections (T17-R4)

def test_sections_come_in_order(ws):
    md = render_markdown(state.load(ws))
    heads = [l for l in md.splitlines() if l.startswith("#")]
    assert heads == ["# QA report: unnamed project", "## Gate", "## Executions", "## Findings", "## Traceability", "## Integrity", "## Limitations"]


def test_gate_rows_keep_policy_order_and_conditions_follow(trusted, actor):
    st = _with_gate(trusted, actor)
    g = st["gates"]["GATE-001"]
    g["conditions"] = ["no_unresolved_high: FND-001 (HIGH, open)", "FND-002 accepted_risk until 2030-01-01T00:00:00.000Z"]
    lines = section(render_markdown(st), "Gate")
    rows = [l for l in lines if l.startswith("| ") and not l.startswith("| Criterion") and not l.startswith("| ---")]
    assert [r.split(" | ")[0][2:] for r in rows] == [md_cell(c["id"]) for c in g["criteria"]]
    first = g["criteria"][0]
    assert rows[0].startswith("| %s | %s | %s | " % (md_cell(first["id"]), first["outcome"], "blocking" if first["blocking"] else "advisory"))
    bullets = [l for l in lines if l.startswith("- ")]
    assert bullets == ["- no\\_unresolved\\_high: FND-001 (HIGH, open)", "- FND-002 accepted\\_risk until 2030-01-01T00:00:00.000Z"]


def test_gate_section_without_a_gate_says_so(ws):
    assert "No gate evaluation recorded." in section(render_markdown(state.load(ws)), "Gate")


def test_executions_are_rows_sorted_by_id(trusted, actor, py):
    a, b = run(trusted, actor, py, "pass"), run(trusted, actor, py, "fail")
    st = state.load(trusted)
    rows = [l for l in section(render_markdown(st), "Executions") if l.startswith("| EXE-")]
    assert [r.split(" | ")[0][2:] for r in rows] == sorted([a, b])
    header = next(l for l in section(render_markdown(st), "Executions") if l.startswith("| ID"))
    for word in ("Total", "Failures", "Errors", "Skipped", "Strength", "Scope"):
        assert word in header
    row = next(r for r in rows if r.startswith("| %s " % b))
    rec = st["executions"][b]
    assert " | failed | " in row and (rec["git"] is None or rec["git"]["commit"][:7] in row)
    assert "| 1 | 1 | 0 | 0 |" in row  # one case, one failure


def test_findings_are_rows_with_the_listed_columns(ws, actor):
    findings.add(ws, actor, base(title="first"))
    st = state.load(ws)
    f = st["findings"]["FND-001"]
    rows = [l for l in section(render_markdown(st), "Findings") if l.startswith("| FND-")]
    assert rows == ["| FND-001 | first | %s | %s | %s | %s |" % (f["severity"], f["priority"], f["state"], f["verification_status"])]


def test_integrity_lists_violations_then_the_gate_outcome(trusted, actor):
    st = _with_gate(trusted, actor)
    st["violations"] = [{"event": "EVT-" + "1" * 26, "kind": "duplicate_id"}, {"event": "EVT-" + "2" * 26, "kind": "unknown_type"}]
    lines = section(render_markdown(st), "Integrity")
    rows = [l for l in lines if l.startswith("| EVT-")]
    assert rows == ["| EVT-" + "1" * 26 + " | duplicate\\_id |", "| EVT-" + "2" * 26 + " | unknown\\_type |"]
    outcome = next(c["outcome"] for c in st["gates"]["GATE-001"]["criteria"] if c["type"] == "test_integrity")
    assert any(l.startswith("| test\\_integrity | %s |" % outcome) for l in lines)


def test_integrity_with_no_violations_says_so(ws):
    assert "The state fold found no violations." in section(render_markdown(state.load(ws)), "Integrity")


def test_limitations_hold_the_stated_limits_then_execution_limitations(trusted, actor, py):
    exe = run(trusted, actor, py, "pass")
    st = state.load(trusted)
    st["executions"][exe]["limitations"] = ["ran | on a laptop"]
    lines = section(render_markdown(st), "Limitations")
    bullets = [l for l in lines if l.startswith("- ")]
    assert bullets == ["- " + text for text in LIMITS]
    assert "| %s | ran \\| on a laptop |" % exe in lines
    assert lines.index(bullets[-1]) < lines.index("| %s | ran \\| on a laptop |" % exe)
    assert all("§" not in l for l in lines)


# --------------------------------------------------------------------------- md_cell (T17-R6)

@pytest.mark.parametrize("raw,want", [
    ("\\", "\\\\"), ("|", "\\|"), ("`", "\\`"), ("*", "\\*"), ("_", "\\_"), ("[", "\\["), ("]", "\\]"),
    ("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"),
    ("a\nb", "a b"), ("a\r\nb", "a  b"), ("a\vb", "a b"), ("a\fb", "a b"), ("a\x1cb", "a b"), ("a\x1db", "a b"),
    ("a\x1eb", "a b"), ("a\x85b", "a b"), ("a\u2028b", "a b"), ("a\u2029b", "a b"),
    (None, ""), (12, "12"), (True, "True"), ("plain", "plain"),
])
def test_md_cell(raw, want):
    assert md_cell(raw) == want


def test_md_cell_escapes_the_backslash_first():
    assert md_cell("\\|") == "\\\\\\|" and md_cell("&lt;") == "&amp;lt;"


def test_a_line_separator_in_a_title_cannot_split_the_row(ws, actor):
    findings.add(ws, actor, base(title="one\u2028two\x85three\rfour"))
    md = render_markdown(state.load(ws))
    rows = [l for l in md.splitlines() if l.startswith("| FND-001")]
    assert len(rows) == 1 and rows[0].startswith("| FND-001 | one two three four | ")
    assert len(md.splitlines()) == md.count("\n")  # no line boundary besides the newlines the renderer wrote
    assert "\u2028" not in md and "\x85" not in md and "\r" not in md


# --------------------------------------------------------------------------- render (T17-R7, T17-R8)

def test_render_event_files_and_modes(trusted, actor, py):
    run(trusted, actor, py, "pass")
    before = state.state_hash(state.load(trusted))
    event = render(trusted, actor)
    data = event["data"]
    assert event["type"] == "report.rendered" and data["id"] == "RPT-001" and data["gate"] is None
    assert set(data) == {"id", "state_hash", "gate", "files"} and data["state_hash"] == before
    folder = trusted.path("reports", "RPT-001")
    assert sorted(data["files"]) == ["report.json", "report.md"]
    for name, digest in data["files"].items():
        assert digest == sha256_bytes((folder / name).read_bytes())
    assert state.state_hash(state.load(trusted)) == before  # rendering leaves the hash alone
    if os.name == "posix":
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_render_records_the_latest_gate_and_the_bytes_end_in_one_newline(trusted, actor):
    g = gate.evaluate(trusted, actor, profile="change")["data"]
    event = render(trusted, actor)
    assert event["data"]["gate"] == g["id"]
    folder = trusted.path("reports", event["data"]["id"])
    md, js = (folder / "report.md").read_bytes(), (folder / "report.json").read_bytes()
    assert md.endswith(b"\n") and not md.endswith(b"\n\n") and js.endswith(b"\n") and not js.endswith(b"\n\n")
    assert js[:-1] == dumps(loads(js))
    assert not re.search(rb"RPT-\d|T\d\d:\d\d:\d\d", js) and not re.search(rb"RPT-\d", md)


def test_render_one_format_writes_only_that_one(trusted, actor):
    event = render(trusted, actor, formats=("json",))
    folder = trusted.path("reports", event["data"]["id"])
    assert list(event["data"]["files"]) == ["report.json"] and [p.name for p in folder.iterdir()] == ["report.json"]


@pytest.mark.parametrize("formats", [(), ("pdf",), ("md", "pdf"), "md", ("MD",), None])
def test_render_refuses_a_bad_format_and_writes_nothing(trusted, actor, formats):
    with pytest.raises(QaError) as exc:
        render(trusted, actor, formats=formats)
    assert exc.value.code == "invalid_format"
    assert list(trusted.path("reports").iterdir()) == [] and [e for e in ledger.iter_all_events(trusted) if e["type"] == "report.rendered"] == []


def test_json_holds_the_listed_keys_and_no_clock_or_report_id(trusted, actor, py):
    run(trusted, actor, py, "pass"); gate.evaluate(trusted, actor, profile="change")
    st = state.load(trusted)
    st["reports"]["RPT-001"] = {"id": "RPT-001"}
    data = render_json(st, project="p")
    assert sorted(data) == ["conditions", "executions", "findings", "gate", "integrity", "limitations", "project", "state_hash", "verdict"]
    assert data["state_hash"] == state.state_hash(st)
    assert "RPT-001" not in json.dumps(data)
    assert render_json(st, project="p") == data


# --------------------------------------------------------------------------- the event and its reducer (T17-R11)

def _rendered(rid="RPT-001", **over):
    data = {"id": rid, "state_hash": SHA, "gate": None, "files": {"report.md": SHA}}
    data.update(over)
    return data


def test_event_schema_accepts_a_report_and_a_gate_id():
    assert schema.errors(_rendered(), schema.load_schema(RPT)) == [] and schema.errors(_rendered(gate="GATE-002", files={"report.md": SHA, "report.json": SHA}), schema.load_schema(RPT)) == []


@pytest.mark.parametrize("over", [{"id": "RPT-1"}, {"state_hash": "abc"}, {"gate": "GATE-x"}, {"files": {}}, {"files": {"x.pdf": SHA}},
                                  {"files": {"report.md": "abc"}}, {"extra": 1}])
def test_event_schema_refuses_the_rest(over):
    assert schema.errors(_rendered(**over), schema.load_schema(RPT)) != []


def test_the_reducer_stores_the_report(ws, actor):
    ledger.append(ws, actor, "report.rendered", _rendered())
    assert state.load(ws)["reports"]["RPT-001"] == _rendered()


def test_a_repeated_report_id_is_a_violation_and_the_first_stays(ws, actor):
    ledger.append(ws, actor, "report.rendered", _rendered())
    second = ledger.append(ws, actor, "report.rendered", _rendered(files={"report.json": SHA}))
    st = state.load(ws)
    assert st["reports"]["RPT-001"]["files"] == {"report.md": SHA}
    assert {"event": second["id"], "kind": "duplicate_id"} in st["violations"]


# --------------------------------------------------------------------------- check_markdown (T17-R9)

def test_a_clean_state_with_prose_that_cites_nothing_passes(ws):
    assert check_markdown("Plain words, no claims.\n", state.load(ws)) == []


def test_location_is_the_one_based_line(trusted, actor, py):
    st = state.load(trusted)
    problems = check_markdown("fine\nall good\nsee FND-999\n", st)
    assert [(p.kind, p.where) for p in problems] == [("unknown_id", "line 3")]


def test_a_tilde_fence_closes_with_a_tilde_and_hides_its_content(ws):
    st = state.load(ws)
    assert check_markdown("~~~\nfailed\n~~~\nfailed\n", st)[0].where == "line 4"
    assert [p.where for p in check_markdown("~~~\nfailed\n```\nfailed\n~~~\nfailed\n", st)] == ["line 6"]  # ``` does not close ~~~


def test_a_fence_may_be_indented_up_to_three_spaces(ws):
    st = state.load(ws)
    assert check_markdown("   ```\nfailed\n   ```\n", st) == []
    assert [p.where for p in check_markdown("    ```\nfailed\n", st)] == ["line 2"]  # four spaces: not a fence


def test_an_unclosed_fence_runs_to_the_end(ws):
    assert check_markdown("```\nfailed\nFND-999\nmore failed\n", state.load(ws)) == []


def test_a_heading_is_checked_like_any_other_line(ws):  # I13: the spec over plan line 1740
    st = state.load(ws)
    assert [(p.kind, p.where) for p in check_markdown("## passed\n#tag failed\n", st)] == [
        ("uncited_claim", "line 1"), ("uncited_claim", "line 2")]


def test_status_words_match_case_insensitively_and_whole(ws):
    st = state.load(ws)
    assert kinds("FAILED\n", st) == ["uncited_claim"] and kinds("Not Run\n", st) == ["uncited_claim"]
    assert kinds("Blocked and Inconclusive\n", st) == ["uncited_claim"]  # one claim per line
    assert kinds("unblocked tests_failed passedx\n", st) == []


def test_a_percentage_is_a_claim(ws):
    st = state.load(ws)
    assert kinds("coverage is 85%\n", st) == ["uncited_claim"] and kinds("85 % done\n", st) == ["uncited_claim"]
    assert kinds("fifty percent\n", st) == []


def test_verdict_tokens_are_case_sensitive_and_whole(ws):
    st = state.load(ws)
    assert kinds("The result is READY.\n", st) == ["uncited_claim"]
    assert kinds("The result is NOT_READY.\n", st) == ["uncited_claim"]  # one claim, and READY inside it is no second token
    assert kinds("INSUFFICIENT_EVIDENCE and READY_WITH_CONDITIONS\n", st) == ["uncited_claim"]
    assert kinds("ready for the demo\n", st) == [] and kinds("READYING\n", st) == []


def test_verdict_token_against_the_cited_gate(trusted, actor):
    g = gate.evaluate(trusted, actor, profile="change")["data"]
    st = state.load(trusted)
    other = "READY" if g["verdict"] != "READY" else "NOT_READY"
    assert check_markdown("%s says %s\n" % (g["id"], g["verdict"]), st) == []
    assert kinds("%s says %s\n" % (g["id"], other), st) == ["status_mismatch"]
    assert kinds("%s says %s and %s\n" % (g["id"], g["verdict"], other), st) == ["status_mismatch"]


def test_two_gates_on_a_line_are_each_compared(trusted, actor):  # I3: every cited gate must agree with the verdict word
    gate.evaluate(trusted, actor, profile="change"); gate.evaluate(trusted, actor, profile="change")
    st = state.load(trusted)
    recorded = st["gates"]["GATE-001"]["verdict"]
    other = "READY" if recorded != "READY" else "NOT_READY"
    assert check_markdown("GATE-001 and GATE-002 say %s\n" % recorded, st) == []
    assert kinds("GATE-001 and GATE-002 say %s\n" % other, st) == ["status_mismatch", "status_mismatch"]


def test_a_cited_execution_decides_the_status_word(trusted, actor, py):
    exe = run(trusted, actor, py, "fail")
    st = state.load(trusted)
    assert check_markdown("%s failed\n" % exe, st) == []
    assert kinds("%s passed\n" % exe, st) == ["status_mismatch"]
    assert kinds("%s failed, then passed\n" % exe, st) == ["status_mismatch"]  # a mixed line is flagged on purpose


def test_not_run_matches_the_not_run_status(trusted, actor, py):
    exe = run(trusted, actor, py, "fail")
    st = state.load(trusted)
    st["executions"][exe]["status"] = "not_run"
    assert check_markdown("%s was not run\n" % exe, st) == [] and kinds("%s failed\n" % exe, st) == ["status_mismatch"]


def test_a_repeated_id_is_judged_at_each_of_its_occurrences(trusted, actor, py):
    a, b = run(trusted, actor, py, "fail"), run(trusted, actor, py, "pass")
    st = state.load(trusted)  # a failed, b passed
    assert check_markdown("%s failed and %s passed, also %s failed\n" % (a, b, a), st) == []
    assert kinds("%s failed and %s passed, also %s passed\n" % (a, b, a), st) == ["status_mismatch"]


def test_a_result_is_compared_too(trusted, actor, py):
    run(trusted, actor, py, "fail")
    st = state.load(trusted)
    res = next(iter(st["results"]))
    assert check_markdown("%s failed\n" % res, st) == [] and kinds("%s passed\n" % res, st) == ["status_mismatch"]


def test_an_execution_and_a_result_on_one_line_are_each_judged_by_the_nearest_id(trusted, actor, py):
    exe = run(trusted, actor, py, "fail")
    res = next(iter(state.load(trusted)["results"]))
    st = state.load(trusted)
    assert check_markdown("%s failed and %s failed\n" % (exe, res), st) == []
    assert kinds("%s and %s passed\n" % (exe, res), st) == ["status_mismatch"]  # "passed" belongs to the result, which failed


def test_each_unknown_id_is_reported_once_per_line(ws):
    st = state.load(ws)
    problems = check_markdown("FND-999 FND-999 AUTH-007 GATE-004 RPT-009 RES-%s EXE-%s EV-%s\n" % ("0" * 26, "0" * 26, "0" * 12), st)
    assert [p.kind for p in problems] == ["unknown_id"] * 7 and len({p.detail for p in problems}) == 7


def test_one_unknown_and_one_known_id_count_as_cited(trusted, actor, py):
    exe = run(trusted, actor, py, "fail")
    st = state.load(trusted)
    assert kinds("%s and FND-999 failed\n" % exe, st) == ["unknown_id"]  # a resolvable ID is cited: no uncited_claim
    assert kinds("%s and FND-999 passed\n" % exe, st) == ["status_mismatch", "unknown_id"]  # FND-999 is not an EXE or RES


def test_an_unknown_execution_or_gate_beside_a_known_one_stops_the_comparison(trusted, actor, py):
    exe = run(trusted, actor, py, "fail")
    g = gate.evaluate(trusted, actor, profile="change")["data"]
    st = state.load(trusted)
    unknown_exe, unknown_gate = "EXE-" + "0" * 26, "GATE-099"
    other = "READY" if g["verdict"] != "READY" else "NOT_READY"
    assert kinds("%s and %s passed\n" % (exe, unknown_exe), st) == ["unknown_id"]
    # a verdict is judged against every gate that resolves, even beside an unknown one (I3)
    assert kinds("%s and %s say %s\n" % (g["id"], unknown_gate, other), st) == ["status_mismatch", "unknown_id"]
    assert kinds("%s passed\n" % exe, st) == ["status_mismatch"]  # alone it is still compared


def test_a_line_with_only_an_unknown_id_is_an_uncited_claim_too(trusted, actor, py):
    st = state.load(trusted)
    assert kinds("FND-999 failed\n", st) == ["uncited_claim", "unknown_id"]


def test_each_id_kind_resolves_in_its_own_table(trusted, actor, py):
    exe = run(trusted, actor, py, "fail")
    findings.add(trusted, actor, base())
    gate.evaluate(trusted, actor, profile="change")
    ledger.append(trusted, actor, "report.rendered", _rendered())
    st = state.load(trusted)
    ev = next(i for i in sorted(state.known_references(st)) if i.startswith("EV-"))
    res = next(iter(st["results"]))
    st["authorizations"]["AUTH-001"] = {"id": "AUTH-001"}
    known = [exe, res, "FND-001", "AUTH-001", "GATE-001", "RPT-001", ev]
    assert check_markdown(" ".join(known) + "\n", st) == []
    for fid in known:  # an ID of the right shape in the wrong table does not resolve
        assert kinds("see %s\n" % fid, {**st, "executions": {}, "results": {}, "findings": {}, "authorizations": {},
                                         "gates": {}, "reports": {}}) == ["unknown_id"] or fid == ev


def test_an_ingested_ev_id_resolves(trusted, actor, py, tmp_path):
    exe = run(trusted, actor, py, "pass")
    stored = trusted.path("evidence", exe, "reports", "junit.xml")
    other = tmp_path / "other.xml"
    other.write_text(stored.read_text().replace('name="t"', 'name="u"'))  # other bytes: the EV ID derives from the content
    execution.ingest_file(trusted, actor, "junit", other, from_execution=exe)
    st = state.load(trusted)
    ev = st["executions"][exe]["ingested"][0]["ev_id"]
    assert ev not in st["executions"][exe]["evidence_manifest"]["ids"].values()  # it is the ingested one, not the manifest's
    assert check_markdown("evidence %s\n" % ev, st) == [] and kinds("evidence EV-%s\n" % ("f" * 12), st) == ["unknown_id"]
    assert exe in state.known_references(st)


def test_ids_are_whole_tokens(ws):
    st = state.load(ws)
    assert kinds("FND-1234-x\n", st) == [] and kinds("xFND-001\n", st) == [] and kinds("(FND-001)\n", st) == ["unknown_id"]


def test_universal_phrases(ws):
    st = state.load(ws)
    for phrase in ("all tests pass", "ALL TESTS PASSED", "every test", "fully tested", "fully scanned", "every endpoint",
                   "100% coverage", "complete coverage", "no bugs", "no vulnerabilities"):
        assert "universal_claim" in kinds("We have %s here\n" % phrase, st), phrase
    assert kinds("every tester agrees\n", st) == [] and kinds("no bugsy\n", st) == []
    assert kinds("all tests pass and every endpoint\n", st) == ["universal_claim"]  # at most one per line


def test_universal_claim_needs_a_full_scope_passed_execution(trusted, actor, py):
    targeted = run(trusted, actor, py, "pass")
    failed_full = run(trusted, actor, py, "fail", scope="full")
    full = run(trusted, actor, py, "pass", scope="full")
    st = state.load(trusted)
    assert kinds("every endpoint (%s)\n" % targeted, st) == ["universal_claim"]
    assert kinds("every endpoint (%s)\n" % failed_full, st) == ["universal_claim"]
    assert kinds("every endpoint (%s)\n" % full, st) == []
    assert kinds("every endpoint (%s, %s)\n" % (targeted, full), st) == []
    assert kinds("every endpoint (FND-999)\n", st) == ["universal_claim", "unknown_id"]


def test_a_full_report_passes_its_own_check(trusted, actor, py):
    run(trusted, actor, py, "fail"); run(trusted, actor, py, "pass", scope="full")
    findings.add(trusted, actor, base())
    gate.evaluate(trusted, actor, profile="change")
    ledger.append(trusted, actor, "report.rendered", _rendered())
    ledger.append(trusted, actor, "report.rendered", _rendered())  # a violation too
    st = state.load(trusted)
    md = render_markdown(st, project="p")
    assert "**Integrity failure:**" in md and check_markdown(md, st) == []


def test_integrity_rows_are_sorted_by_event_id_even_when_the_fold_settles_late(ws, actor):
    ledger.append(ws, actor, "result.reclassified", {"result": "RES-" + "0" * 26, "new_status": "passed", "reason": "r",
                                                     "evidence": ["EXE-" + "0" * 26]})  # settled after the fold loop
    ledger.append(ws, actor, "report.rendered", _rendered())
    ledger.append(ws, actor, "report.rendered", _rendered())  # duplicate_id, found during the loop
    st = state.load(ws)
    assert [v["kind"] for v in st["violations"]] == ["duplicate_id", "unknown_result"]  # fold order
    rows = [l for l in section(render_markdown(st), "Integrity") if l.startswith("| EVT-")]
    assert rows == sorted(rows) and [r.split(" | ")[1].rstrip(" |") for r in rows] == ["unknown\\_result", "duplicate\\_id"]


def test_rows_sort_by_number_not_by_text(ws, actor):
    st = state.load(ws)
    for n in (1000, 999):
        st["findings"]["FND-%d" % n] = dict(base(), title="t", severity="LOW", priority="P3", state="open",
                                              verification_status="suspected")
    rows = [l.split(" | ")[0] for l in section(render_markdown(st), "Findings") if l.startswith("| FND-")]
    assert rows == ["| FND-999", "| FND-1000"]


# --------------------------------------------------------------------------- kernel text passes its own check (T17-R12)

def test_a_report_with_kernel_refusals_passes_its_own_check(trusted, actor, py):
    execution.execute(trusted, actor, execution.RunRequest(command="echo hi"))  # shell-without-flag
    execution.execute(trusted, actor, execution.RunRequest(command=["env", "PGPASSWORD=hunter22", "psql"]))  # secret-in-argv
    refused = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass", attempt_of="EXE-" + "9" * 26))
    st = state.load(trusted)
    rules = {b["rule_id"] for b in st["blocked_commands"]}
    assert {"shell-without-flag", "secret-in-argv"} <= rules and st["executions"][refused["execution"]]["limitations"]
    md = render_markdown(st, project="p")
    assert check_markdown(md, st) == []


# --------------------------------------------------------------------------- final review: I13

CLAIM = "The suite passed."


def _lines(*lines):
    return "\n".join(lines) + "\n"


def _flagged(text):
    return [p.kind for p in check_markdown(text, state.empty_state())]


@pytest.mark.parametrize("fake", ["```not a fence`", "``` ` ```", "```x```"])
def test_an_opener_with_a_backtick_in_its_info_string_is_no_fence(fake):
    assert _flagged(_lines(fake, CLAIM)) == ["uncited_claim"]


def test_a_tilde_fence_may_hold_a_backtick_in_its_info_string():
    assert _flagged(_lines("~~~ a`b", CLAIM, "~~~")) == []


@pytest.mark.parametrize("opener,closer", [("```", "````"), ("~~~~", "~~~~~"), ("   ```", "```   ")])
def test_a_longer_or_indented_closer_ends_the_fence(opener, closer):
    assert _flagged(_lines(opener, CLAIM, closer, CLAIM)) == ["uncited_claim"]


@pytest.mark.parametrize("not_closing", ["``", "~~~", "``` python", "```x"])
def test_only_a_real_closer_ends_the_fence(not_closing):
    assert _flagged(_lines("```", not_closing, CLAIM, "```")) == []


def test_a_shorter_run_does_not_close_a_longer_fence():
    assert _flagged(_lines("`````", "```", CLAIM, "`````")) == []


def test_an_unclosed_fence_hides_the_rest():
    assert _flagged(_lines("```", CLAIM)) == []


@pytest.mark.parametrize("heading", ["# All tests passed", "## READY", "### 100% coverage", "  #### Build failed"])
def test_a_claim_in_a_heading_is_checked(heading):
    assert _flagged(_lines("# QA report: x", heading)) != []


def test_a_plain_heading_is_not_a_claim():
    assert _flagged(_lines("# QA report: x", "## Gate", "## Findings")) == []


def test_only_the_generated_title_is_exempt():
    assert _flagged(_lines("# QA report: a project that passed")) == []
    assert _flagged(_lines("Intro", "# QA report: everything passed")) == ["uncited_claim"]
    assert _flagged(_lines("# Something else that passed")) == ["uncited_claim"]


def test_the_line_breaks_are_written_as_escapes():  # M9: an editor shows nothing at a raw U+2028 or U+2029
    source = (REPO / "shared" / "kernel" / "qa" / "report" / "__init__.py").read_text(encoding="utf-8")
    assert "\u2028" not in source and "\u2029" not in source
    assert {"\u2028", "\u2029"} <= set(report._LINE_BREAKS)


# --------------------------------------------------------------------------- executive summary (task 14)

def _exec_state(trusted, actor):
    findings.add(trusted, actor, base(component={"path": "a/low.py", "criticality": "low"},
                                     severity_rationale={"anchor": "SEC-M1", "modifiers": []}, title="Low one",
                                     refs={"rule": "manual", "location": "a/low.py"}, recommendation="Fix the low one"))
    findings.add(trusted, actor, base(title="Critical one", recommendation="Fix the critical one"))
    findings.add(trusted, actor, base(component={"path": "a/mid.py", "criticality": "normal"}, title="Middle one",
                                     refs={"rule": "manual", "location": "a/mid.py"}, recommendation=None))
    gate.evaluate(trusted, actor, profile="change")
    return state.load(trusted)


def test_executive_section_is_byte_stable(trusted, actor):
    st = _exec_state(trusted, actor)
    a, b = report.render_executive(st, project="Shop"), report.render_executive(st, project="Shop")
    assert a == b and a.endswith("\n") and not a.endswith("\n\n")
    assert a.splitlines()[0] == "# QA report: Shop" and "## Executive summary" in a
    first, second = report.render(trusted, actor, formats=("executive",)), report.render(trusted, actor, formats=("executive",))
    assert first["data"]["files"].keys() == {"executive.md"}
    assert first["data"]["files"] == second["data"]["files"]
    assert not re.search(r"RPT-\d|T\d\d:\d\d:\d\d", a)


def test_executive_recommendations_ordered_by_priority(trusted, actor):
    text = report.render_executive(_exec_state(trusted, actor), project="Shop")
    lines = section(text, "Recommendations")
    recs = [l for l in lines if re.match(r"\d+\. ", l)]
    assert [re.search(r"FND-\d+", l).group(0) for l in recs] == ["FND-002", "FND-003", "FND-001"]
    assert [re.search(r"P\d", l).group(0) for l in recs] == ["P0", "P1", "P3"]
    assert "Fix the critical one" in recs[0] and "Middle one" in recs[1]  # no recommendation: the title stands in


def test_executive_passes_the_claim_check_and_carries_the_integrity_headline(trusted, actor):
    st = _exec_state(trusted, actor)
    assert check_markdown(report.render_executive(st, project="Shop"), st) == []
    assert "**Verdict:** " in report.render_executive(st, project="Shop")
    broken = state.load(trusted)
    broken["violations"].append({"event": "EV-x", "kind": "hash_mismatch"})
    assert report.render_executive(broken, project="Shop").splitlines()[2].startswith("**Integrity failure:**")


def test_executive_without_findings_or_gate_says_so(ws):
    text = report.render_executive(state.load(ws), project="Shop")
    assert "No gate evaluation recorded; no verdict." in text and "No unresolved findings." in text


# --------------------------------------------------------------------------- fix round 1: tamper after the gate, row cap

def _gated_run(trusted, actor, py):
    exe = run(trusted, actor, py, "pass")
    gate.evaluate(trusted, actor, profile="change")
    return exe


def _tamper(ws, exe):
    log = ws.path("evidence", exe, "stdout.log")
    log.write_bytes(log.read_bytes() + b"added after the gate\n")


@pytest.mark.parametrize("fmt,name", [("md", "report.md"), ("executive", "executive.md"), ("json", "report.json")])
def test_a_log_edited_after_the_gate_is_the_headline_in_every_format(trusted, actor, py, fmt, name):
    exe = _gated_run(trusted, actor, py)
    _tamper(trusted, exe)
    event = render(trusted, actor, formats=(fmt,))
    text = trusted.path("reports", event["data"]["id"], name).read_text(encoding="utf-8")
    if fmt in ("md", "executive"):
        lines = text.splitlines()
        assert lines[2].startswith("**Integrity failure:**") and "ledger verification" in lines[2]
        verdict = state.load(trusted)["gates"]["GATE-001"]["verdict"]
        assert "**Verdict:**" not in text and ("%s (GATE-001" % verdict) in lines[3]
        assert lines[3].startswith("**Recorded verdict, not current:**") and "cannot be relied on" in lines[3]
        assert "before the change" in lines[3]
    elif fmt == "json":
        data = json.loads(text)
        recorded = state.load(trusted)["gates"]["GATE-001"]["verdict"]
        assert data["verdict"] is None and data["recorded_verdict"] == recorded and data["verdict_current"] is False
        assert data["integrity"]["ledger_problems"][0]["kind"] == "modified"


def test_report_check_flags_a_file_when_the_ledger_fails_now(trusted, actor, py):
    exe = _gated_run(trusted, actor, py)
    event = render(trusted, actor, formats=("executive",))
    path = trusted.path("reports", event["data"]["id"], "executive.md")
    assert report.check(trusted, path.read_text(encoding="utf-8")) == []
    _tamper(trusted, exe)
    assert [p.kind for p in report.check(trusted, path.read_text(encoding="utf-8"))] == ["ledger_integrity"]


def test_a_log_edited_before_the_gate_keeps_the_gates_own_verdict_line_and_passes_the_check(trusted, actor, py):
    exe = run(trusted, actor, py, "pass")
    _tamper(trusted, exe)
    gate.evaluate(trusted, actor, profile="change")
    event = render(trusted, actor, formats=("md",))
    text = trusted.path("reports", event["data"]["id"], "report.md").read_text(encoding="utf-8")
    assert text.splitlines()[2].startswith("**Integrity failure:**") and "**Verdict:** NOT_READY (GATE-001" in text
    assert report.check(trusted, text) == []


def test_a_clean_ledger_keeps_the_report_unchanged(trusted, actor, py):
    from qa import config
    _gated_run(trusted, actor, py)
    st, project = state.load(trusted), config.load_config(trusted)["project"]["name"]
    event = render(trusted, actor, formats=("md", "json", "executive"))
    folder = trusted.path("reports", event["data"]["id"])
    assert (folder / "report.md").read_bytes() == render_markdown(st, project=project).encode("utf-8")
    assert (folder / "executive.md").read_bytes() == report.render_executive(st, project=project).encode("utf-8")
    assert "Recorded verdict" not in (folder / "executive.md").read_text(encoding="utf-8")
    assert "verdict_current" not in json.loads((folder / "report.json").read_text(encoding="utf-8"))


def test_the_error_for_a_bad_format_names_every_format(trusted, actor):
    with pytest.raises(QaError) as e:
        render(trusted, actor, formats=("pdf",))
    assert "executive" in e.value.message


def test_executive_recommendations_are_capped_and_the_rest_counted(trusted, actor):
    total = report.EXECUTIVE_ROW_LIMIT + 4
    for n in range(total):
        findings.add(trusted, actor, base(title="Finding number %d" % n, refs={"rule": "r%d" % n, "location": "a/f%d.py" % n},
                                          component={"path": "a/f%d.py" % n, "criticality": "normal"}))
    st = state.load(trusted)
    text = report.render_executive(st, project="Shop")
    lines = section(text, "Recommendations")
    assert len([l for l in lines if re.match(r"\d+\. ", l)]) == report.EXECUTIVE_ROW_LIMIT
    assert "4 more unresolved findings are not listed here" in "\n".join(lines)
    assert check_markdown(text, st) == []


# --------------------------------------------------------------------------- fix round 2: the check reads the generated shape

def _honest_after_tamper(trusted, actor, py, fmt="executive"):
    exe = _gated_run(trusted, actor, py)
    clean = render(trusted, actor, formats=(fmt,))
    name = {"md": "report.md", "executive": "executive.md"}[fmt]
    clean_text = trusted.path("reports", clean["data"]["id"], name).read_text(encoding="utf-8")
    _tamper(trusted, exe)
    event = render(trusted, actor, formats=(fmt,))
    return exe, clean_text, trusted.path("reports", event["data"]["id"], name).read_text(encoding="utf-8")


@pytest.mark.parametrize("fmt", ["md", "executive"])
def test_the_honest_report_rendered_after_the_tamper_passes_the_check(trusted, actor, py, fmt):
    _, _, honest = _honest_after_tamper(trusted, actor, py, fmt)
    assert report.check(trusted, honest) == []


def test_a_hostile_headline_does_not_pass_the_check(trusted, actor, py):
    _, clean, honest = _honest_after_tamper(trusted, actor, py)
    lines = clean.splitlines()
    hostile = "\n".join(lines[:2] + ["**Integrity failure:** none, everything is fine; see Integrity."] + lines[2:]) + "\n"
    assert [p.kind for p in report.check(trusted, hostile)] == ["ledger_integrity"]
    padded = honest.replace("; see Integrity.", "; everything is fine; see Integrity.", 1)
    assert [p.kind for p in report.check(trusted, padded)] == ["ledger_integrity"]


def test_a_headline_for_fewer_problems_than_exist_now_does_not_pass(trusted, actor, py):
    exe, _, honest = _honest_after_tamper(trusted, actor, py)
    log = trusted.path("evidence", exe, "stderr.log")
    log.write_bytes(log.read_bytes() + b"second edit\n")
    assert [p.kind for p in report.check(trusted, honest)] == ["ledger_integrity"]


def test_a_plain_verdict_line_is_refused_while_the_gate_did_not_see_the_problem(trusted, actor, py):
    _, clean, honest = _honest_after_tamper(trusted, actor, py)
    headline = honest.splitlines()[2]
    lines = clean.splitlines()
    forged = "\n".join(lines[:2] + [headline] + lines[2:]) + "\n"
    assert "**Verdict:**" in forged and [p.kind for p in report.check(trusted, forged)] == ["ledger_integrity"]
    both = honest + "\n" + "\n".join(l for l in lines if l.startswith("**Verdict:**")) + "\n"
    assert [p.kind for p in report.check(trusted, both)] == ["ledger_integrity"]


# --------------------------------------------------------------------------- the traceability section

COMMIT = "a" * 40


def _traced_state(ws, actor):
    """REQ-001, AC-001.1, TC-001 automated and run on COMMIT without a red check, REQ-002 with no AC."""
    test_trace.chain(ws, actor)
    test_trace.record_run(ws, actor, "A", COMMIT, [(test_trace.TEST_ID, "passed", {})], 1)
    records.add(ws, actor, "requirement", test_records.req(statement="A second rule."))
    return state.load(ws)


def test_the_report_has_a_traceability_section_with_counts_and_one_row_per_gap(ws, actor):
    st = _traced_state(ws, actor)
    lines = section(render_markdown(st), "Traceability")
    matrix = trace.matrix(st, COMMIT)
    assert "Target commit: %s" % COMMIT[:12] in "\n".join(lines)
    for kind in trace.GAP_KINDS:
        assert "| %s | %d |" % (md_cell(kind), matrix["counts"][kind]) in lines
    rows = [l for l in lines if l.startswith("| ") and any(l.startswith("| %s | %s |" % (g["id"], md_cell(g["kind"]))) for g in matrix["gaps"])]
    assert len(rows) == len(matrix["gaps"]) and len(matrix["gaps"]) >= 2
    assert "| REQ-002 | %s |" % md_cell("req_without_ac") in lines and "| TC-001 | %s |" % md_cell("tc_unproven") in lines


def test_the_traceability_section_is_byte_stable(ws, actor):
    st = _traced_state(ws, actor)
    assert render_markdown(st) == render_markdown(state.load(ws)) == render_markdown(st)
    assert section(render_markdown(st), "Traceability") == section(render_markdown(state.load(ws)), "Traceability")


def test_the_traceability_target_is_the_gates_when_there_is_one(ws, actor):
    st = _traced_state(ws, actor)
    st["gates"] = {"GATE-001": {"id": "GATE-001", "target": {"commit": "b" * 40}, "criteria": [], "conditions": [],
                                  "verdict": "INSUFFICIENT_EVIDENCE", "profile": "change", "independence": "I1", "state_hash": SHA}}
    assert "Target commit: %s" % ("b" * 12) in "\n".join(section(render_markdown(st), "Traceability"))


def test_the_traceability_section_without_records_says_so(ws):
    assert [l for l in section(render_markdown(state.load(ws)), "Traceability") if l] == ["No requirements or test cases recorded."]


def test_the_traceability_section_without_a_commit_says_so(ws, actor):
    test_trace.chain(ws, actor)
    lines = [l for l in section(render_markdown(state.load(ws)), "Traceability") if l]
    assert lines == ["No execution on a recorded commit; the matrix needs one."]
