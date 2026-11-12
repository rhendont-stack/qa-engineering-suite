from __future__ import annotations

import json
import os

import pytest

import test_compare
from helpers import base
from qa import findings, fixes, ledger, records, schema, state, trace
from qa.errors import QaError
from qa.workspace import config_bytes
from test_records import ac, req, rsk, tc

TEST_ID = "tests.test_discount::test_cap"
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


def exe_id(letter):
    return "EXE-" + "0" * 25 + letter


def refused(code, call, *args, **kw):
    with pytest.raises(QaError) as caught:
        call(*args, **kw)
    assert caught.value.code == code
    return caught.value


def record_run(ws, actor, letter, commit, results, minute):
    """An execution on ``commit`` with ``results`` as ``(test_id, status, changes)``; returns its id."""
    data = test_compare._execution(exe_id(letter))
    data["started_at"] = data["ended_at"] = "2026-11-10T10:%02d:00.000Z" % minute
    data["git"] = {"commit": commit, "branch": None, "dirty": False, "diff_hash": "sha256:" + "0" * 64}
    ledger.append(ws, actor, "execution.recorded", data)
    for number, (test_id, status, changes) in enumerate(results):
        result = test_compare._result(minute * 100 + number, exe_id(letter), test_id, status)
        result.update(changes)
        ledger.append(ws, actor, "result.recorded", result)
    return exe_id(letter)


def chain(ws, actor, *, automated=True, **tc_changes):
    """REQ-001, AC-001.1, TC-001 (verifying it), and TC-001 automated_by TEST_ID."""
    records.add(ws, actor, "requirement", req())
    records.add(ws, actor, "criterion", ac())
    records.add(ws, actor, "testcase", tc(**tc_changes))
    if automated:
        trace.link(ws, actor, "TC-001", "automated_by", TEST_ID)


def gaps(ws, target=COMMIT_A):
    return trace.matrix(state.load(ws), target)["gaps"]


# --------------------------------------------------------------------------- links

@pytest.mark.parametrize("relation,source,target", [
    ("verified_by", "REQ-001", "TC-001"), ("verified_by", "TC-001", "AC-001.1"), ("mitigated_by", "AC-001.1", "TC-001"),
    ("executed_in", "AC-001.1", "EXE-" + "0" * 25 + "A"), ("executed_in", "TC-001", "TC-001"),
    ("automated_by", "AC-001.1", TEST_ID), ("automated_by", "TC-001", "no-separator"), ("automated_by", "TC-001", "../x.py::test_y"),
    ("red_check", "AC-001.1", "EXE-" + "0" * 25 + "A"), ("violates", "TC-001", "AC-001.1"), ("violates", "FND-001", "TC-001"),
    ("addressed_by", "FND-001", "FIX-001"), ("validated_by", "FIX-001", "RVL-001"), ("has", "REQ-001", "AC-001.1"),
])
def test_only_the_declared_pairs_link(ws, actor, relation, source, target):
    chain(ws, actor)
    findings.add(ws, actor, base())
    before = len(state.load(ws)["links"])
    refused("invalid_link", trace.link, ws, actor, source, relation, target)
    assert len(state.load(ws)["links"]) == before


def test_the_declared_pairs_link_and_a_repeat_records_nothing(ws, actor):
    chain(ws, actor)
    records.add(ws, actor, "risk", rsk())
    findings.add(ws, actor, base())
    run = record_run(ws, actor, "A", COMMIT_A, [(TEST_ID, "passed", {})], 1)
    for relation, source, target in [("executed_in", "TC-001", run), ("violates", "FND-001", "AC-001.1"),
                                     ("violates", "FND-001", "REQ-001")]:
        first = trace.link(ws, actor, source, relation, target)
        assert first["created"] is True
        again = trace.link(ws, actor, source, relation, target)
        assert (again["created"], again["event"]) == (False, first["event"])
    assert trace.link(ws, actor, "AC-001.1", "verified_by", "TC-001")["created"] is False  # tc add already recorded it
    assert trace.link(ws, actor, "RSK-001", "mitigated_by", "TC-001")["created"] is False
    linked = {(l["source"], l["relation"]): l.get("via") for l in state.load(ws)["links"]}
    assert linked[("TC-001", "executed_in")] == "manual" and linked[("AC-001.1", "verified_by")] is None


def test_a_link_to_a_record_that_does_not_exist_is_refused(ws, actor):
    chain(ws, actor)
    refused("unknown_record", trace.link, ws, actor, "AC-009.1", "verified_by", "TC-001")
    refused("unknown_record", trace.link, ws, actor, "AC-001.1", "verified_by", "TC-009")
    refused("unknown_record", trace.link, ws, actor, "TC-001", "executed_in", exe_id("Z"))
    refused("unknown_record", trace.link, ws, actor, "FND-001", "violates", "AC-001.1")
    refused("invalid_link", trace.link, ws, actor, "AC-001.1", "verified_by", "TC-001", via="guess")


def test_an_automated_by_path_form_is_recorded_in_the_canonical_form(ws, actor):
    chain(ws, actor, automated=False)
    done = trace.link(ws, actor, "TC-001", "automated_by", "tests/test_discount.py::TestCap::test_cap[a-b]")
    assert done["target"] == "tests.test_discount.TestCap::test_cap[a-b]"
    assert [l["target"] for l in state.load(ws)["links"] if l["relation"] == "automated_by"] == [done["target"]]


@pytest.mark.parametrize("given,expected", [
    ("tests.test_x::test_y", "tests.test_x::test_y"), ("tests/test_x.py::test_y", "tests.test_x::test_y"),
    ("./tests/unit/test_x.py::TestA::test_y", "tests.unit.test_x.TestA::test_y"),
    ("tests/test_x.py::TestA::TestB::test_y[1-2]", "tests.test_x.TestA.TestB::test_y[1-2]"),
    ("test_x.py::test_y", "test_x::test_y"), ("/abs/test_x.py::test_y", None), ("../test_x.py::test_y", None),
    ("tests/test_x.py", None), ("tests/test_x.py::", None), ("::test_y", None), (None, None), (7, None),
    ("tests\\test_x.py::test_y", None), ("tests/test_x.py::A::::test_y", None),
])
def test_test_ids_are_normalised_to_what_results_record(given, expected):
    assert trace.normalise_test_id(given) == expected


def test_red_check_needs_an_assertion_failure_of_the_mapped_test(ws, actor):
    chain(ws, actor)
    records.add(ws, actor, "testcase", tc(title="Unmapped"))
    by_error = record_run(ws, actor, "A", COMMIT_A, [(TEST_ID, "failed", {"failure_kind": "error"})], 1)
    passed = record_run(ws, actor, "B", COMMIT_A, [(TEST_ID, "passed", {})], 2)
    other = record_run(ws, actor, "C", COMMIT_A, [("tests.test_other::test_z", "failed", {})], 3)
    good = record_run(ws, actor, "D", COMMIT_A, [(TEST_ID, "failed", {})], 4)
    for execution in (by_error, passed, other):  # an error such as a missing import proves nothing
        refused("red_check_refused", trace.link, ws, actor, "TC-001", "red_check", execution)
    refused("red_check_refused", trace.link, ws, actor, "TC-002", "red_check", good)  # TC-002 is not automated_by anything
    refused("unknown_record", trace.link, ws, actor, "TC-001", "red_check", exe_id("Z"))
    assert not [l for l in state.load(ws)["links"] if l["relation"] == "red_check"]
    assert trace.link(ws, actor, "TC-001", "red_check", good)["created"] is True


# --------------------------------------------------------------------------- scan

SOURCE = '''\
import pathlib
pathlib.Path(__file__).with_name("canary").write_text("ran")  # a scan must never run this

import pytest


def test_TC001_cap():
    assert True


def test_untagged():
    assert True


@pytest.mark.qa("TC-002")
def test_two_codes():
    assert True


@pytest.mark.qa("TC-004")
def test_other_name():
    assert True


class TestStacking:
    def test_TC_003_stack(self):
        assert True

    def helper_TC001(self):
        pass


def helper_TC001_not_a_test():
    pass
'''


def tag_project(root):
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_discount.py").write_text(SOURCE)
    (root / "tests" / "test_broken.py").write_text("def test_TC005_x(:\n")
    (root / "web").mkdir()
    (root / "web" / "cart.spec.ts").write_text("it('TC-006 shows the total', () => {})\n")
    (root / "docs").mkdir()
    (root / "docs" / "notes.py").write_text("def test_TC001_in_a_file_that_is_not_a_test_file():\n    pass\n")
    return root


def several_cases(ws, actor, count):
    records.add(ws, actor, "requirement", req())
    records.add(ws, actor, "criterion", ac())
    for number in range(count):
        records.add(ws, actor, "testcase", tc(title="Case %d" % (number + 1)))


def write_map(ws, data):
    path = ws.path("trace", "test-map.json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data))


def snapshot(root):
    found = {}
    for current, dirs, names in os.walk(str(root)):
        for name in names + dirs:
            full = os.path.join(current, name)
            info = os.lstat(full)
            found[os.path.relpath(full, str(root))] = (info.st_mtime_ns, info.st_size)
    return found


def test_scan_reads_tags_and_the_map_and_reports_disagreement(ws, actor, tmp_path):
    several_cases(ws, actor, 6)
    root = tag_project(tmp_path / "project")
    record_run(ws, actor, "A", COMMIT_A, [("pkg.mod::test_TC006_total", "passed", {})], 1)  # a tag in a recorded result's name
    write_map(ws, {"TC-004": ["tests/test_discount.py::test_other_name_but_mapped_elsewhere"],  # disagrees with the tag
                   "TC-003": ["tests/test_discount.py::TestStacking::test_TC_003_stack"],       # agrees with the tag, in the path form
                   "TC-001": ["tests.test_discount::test_TC001_cap", "../escape.py::t", "bare"],
                   "TC-009": ["tests.test_discount::test_x"]})
    before = snapshot(root)
    done = trace.scan(ws, actor, root)
    assert snapshot(root) == before and not (root / "tests" / "canary").exists()
    links = {(l["source"], l["target"]): l.get("via") for l in state.load(ws)["links"] if l["relation"] == "automated_by"}
    assert links == {
        ("TC-001", "tests.test_discount::test_TC001_cap"): "tag",
        ("TC-002", "tests.test_discount::test_two_codes"): "tag",
        ("TC-003", "tests.test_discount.TestStacking::test_TC_003_stack"): "tag",
        ("TC-006", "pkg.mod::test_TC006_total"): "tag",
    }
    assert done["conflicts"] == [{"testcase": "TC-004", "tags": ["tests.test_discount::test_other_name"],
                                  "map": ["tests.test_discount::test_other_name_but_mapped_elsewhere"]}]
    assert done["unknown_testcases"] == ["TC-009"]
    reasons = {(u.get("file"), u.get("entry") or u.get("tag")) for u in done["unmapped"]}
    assert (".qa/trace/test-map.json", "../escape.py::t") in reasons and (".qa/trace/test-map.json", "bare") in reasons
    assert ("web/cart.spec.ts", "TC-005") not in reasons and ("web/cart.spec.ts", "TC-006") in reasons  # another language: named, not mapped
    assert ("tests/test_broken.py", None) in reasons  # a file that does not parse is reported, never guessed
    assert not [l for l in links if l[0] == "TC-004"]
    assert done["files_read"] == 3 and done["symlinks_skipped"] == 0
    again = trace.scan(ws, actor, root)
    assert again["linked"] == [] and again["already_linked"] == 4


def test_a_tag_needs_a_boundary_before_it(ws, actor, tmp_path):
    several_cases(ws, actor, 1)
    root = tmp_path / "p"
    root.mkdir()
    (root / "test_a.py").write_text("def test_ETC_001_not_a_tag():\n    pass\n\n\ndef test_x_TC_001_is():\n    pass\n")
    assert [l["target"] for l in trace.scan(ws, actor, root)["linked"]] == ["test_a::test_x_TC_001_is"]


def test_scan_says_when_the_file_cap_cut_it_short(ws, actor, tmp_path, monkeypatch):
    several_cases(ws, actor, 1)
    for name in ("test_a.py", "test_b.py"):
        (tmp_path / name).write_text("def test_TC001_x():\n    pass\n")
    assert trace.scan(ws, actor, tmp_path)["truncated"] is False
    monkeypatch.setattr(trace, "_MAX_FILES", 1)
    assert trace.scan(ws, actor, tmp_path)["truncated"] is True


def test_a_map_alone_links_with_via_map(ws, actor, tmp_path):
    several_cases(ws, actor, 1)
    write_map(ws, {"TC-001": ["tests/test_a.py::TestB::test_c"]})
    done = trace.scan(ws, actor, tmp_path)
    assert [(l["target"], l["via"]) for l in done["linked"]] == [("tests.test_a.TestB::test_c", "map")]


def test_a_map_that_does_not_fit_its_schema_is_refused(ws, actor, tmp_path):
    several_cases(ws, actor, 1)
    write_map(ws, {"tc-1": ["x::y"]})
    refused("trace_map_invalid", trace.scan, ws, actor, tmp_path)
    write_map(ws, {"TC-001": "tests.test_a::test_b"})
    refused("trace_map_invalid", trace.scan, ws, actor, tmp_path)
    ws.path("trace", "test-map.json").write_text("{")
    refused("trace_map_invalid", trace.scan, ws, actor, tmp_path)


def test_a_map_that_repeats_a_test_case_key_is_refused(ws, actor, tmp_path):
    """K7: the map is read with the kernel's canonical JSON reader, so a repeated key is refused, not reduced to its last value."""
    several_cases(ws, actor, 1)
    path = ws.path("trace", "test-map.json")
    path.parent.mkdir(exist_ok=True)
    path.write_text('{"TC-001": ["tests.test_a::test_b"], "TC-001": ["tests.test_a::test_c"]}')
    error = refused("trace_map_invalid", trace.scan, ws, actor, tmp_path)
    assert "duplicate key" in error.details["error"]
    assert [l for l in state.load(ws)["links"] if l["relation"] == "automated_by"] == []


def test_scan_never_follows_symlinks_or_executes(ws, actor, tmp_path):
    several_cases(ws, actor, 4)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "test_out.py").write_text("def test_TC002_outside():\n    pass\n")
    (outside / "test_secret.py").write_text("def test_TC003_secret():\n    pass\n")
    root = tag_project(tmp_path / "project")
    (root / "linked_dir").symlink_to(outside, target_is_directory=True)
    (root / "tests" / "test_link.py").symlink_to(outside / "test_secret.py")
    (root / "tests" / "conftest.py").write_text("import pathlib\npathlib.Path(__file__).with_name('conftest-canary').write_text('ran')\n")
    done = trace.scan(ws, actor, root)
    targets = {l["target"] for l in done["linked"]}
    assert not any("outside" in t or "secret" in t or "linked_dir" in t or "test_link" in t for t in targets)
    assert [l["source"] for l in done["linked"] if l["source"] in ("TC-002", "TC-003")] == ["TC-002", "TC-003"]  # from the real file only
    assert {l["target"] for l in done["linked"] if l["source"] == "TC-002"} == {"tests.test_discount::test_two_codes"}
    assert done["symlinks_skipped"] == 2
    assert not list(root.rglob("*canary*"))
    link_root = tmp_path / "link-to-project"
    link_root.symlink_to(root, target_is_directory=True)
    refused("scan_root_invalid", trace.scan, ws, actor, link_root)
    refused("scan_root_invalid", trace.scan, ws, actor, tmp_path / "missing")


def test_a_symlinked_map_is_refused(ws, actor, tmp_path):
    several_cases(ws, actor, 1)
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"TC-001": ["a.b::c"]}))
    path = ws.path("trace", "test-map.json")
    path.parent.mkdir(exist_ok=True)
    path.symlink_to(real)
    refused("trace_map_invalid", trace.scan, ws, actor, tmp_path)
    assert state.load(ws)["links"][-1]["relation"] == "verified_by"


# --------------------------------------------------------------------------- matrix

def build_req_without_ac(ws, actor):
    records.add(ws, actor, "requirement", req())
    return "REQ-001"


def build_ac_without_tc(ws, actor):
    records.add(ws, actor, "requirement", req())
    records.add(ws, actor, "criterion", ac())
    return "AC-001.1"


def build_tc_not_automated(ws, actor):
    chain(ws, actor, automated=False)
    return "TC-001"


def build_tc_never_executed(ws, actor):
    chain(ws, actor)
    return "TC-001"


def build_blocked(ws, actor):
    chain(ws, actor)
    record_run(ws, actor, "A", COMMIT_A, [(TEST_ID, "blocked", {})], 1)
    return "TC-001"


def build_characterization(ws, actor):
    chain(ws, actor, oracle="characterization")
    return "REQ-001"


def build_unproven(ws, actor):
    chain(ws, actor)
    record_run(ws, actor, "A", COMMIT_A, [(TEST_ID, "passed", {})], 1)  # passes, but was never seen failing
    return "TC-001"


def build_fnd_without_fix(ws, actor):
    findings.add(ws, actor, base())
    return "FND-001"


def build_fix_without_rvl(ws, actor):
    findings.add(ws, actor, base())
    fixes.add(ws, actor, {"findings": ["FND-001"], "base_commit": COMMIT_B, "fix_commit": COMMIT_A, "description": "d"})
    return "FIX-001"


def build_rvl_off_target(ws, actor):
    findings.add(ws, actor, base())
    fixes.add(ws, actor, {"findings": ["FND-001"], "base_commit": COMMIT_A, "fix_commit": COMMIT_B, "description": "d"})
    base_f = record_run(ws, actor, "A", COMMIT_A, [(TEST_ID, "failed", {})], 1)
    head_f = record_run(ws, actor, "B", COMMIT_B, [(TEST_ID, "passed", {})], 2)
    base_p = record_run(ws, actor, "C", COMMIT_A, [("tests.t::keep", "passed", {})], 3)
    head_p = record_run(ws, actor, "D", COMMIT_B, [("tests.t::keep", "passed", {})], 4)
    fixes.revalidate(ws, actor, "FIX-001", f2p=(base_f, head_f), p2p=(base_p, head_p), defect_tests=[TEST_ID])
    return "RVL-001"  # validated at COMMIT_B, and the matrix below is about COMMIT_A


BUILDERS = {
    "req_without_ac": build_req_without_ac, "ac_without_tc": build_ac_without_tc, "tc_not_automated": build_tc_not_automated,
    "tc_never_executed": build_tc_never_executed, "tc_last_blocked_or_inconclusive": build_blocked,
    "req_only_characterization_or_assumed": build_characterization, "tc_unproven": build_unproven,
    "fnd_without_fix": build_fnd_without_fix, "fix_without_rvl": build_fix_without_rvl, "rvl_off_target": build_rvl_off_target,
}


def test_every_gap_kind_has_a_case_and_the_schema_lists_every_kind():
    assert set(BUILDERS) == set(trace.GAP_KINDS) and len(trace.GAP_KINDS) == 10
    assert {"fix_without_rvl", "rvl_off_target"} <= set(trace.GAP_KINDS)
    listed = schema.load_schema("trace-matrix.schema.json")["$defs"]["gap"]["properties"]["kind"]["enum"]
    assert list(listed) == list(trace.GAP_KINDS)


@pytest.mark.parametrize("kind", trace.GAP_KINDS)
def test_each_gap_is_reported(ws, actor, kind):
    identifier = BUILDERS[kind](ws, actor)
    result = trace.matrix(state.load(ws), COMMIT_A)
    assert {"kind": kind, "id": identifier} in result["gaps"]
    assert result["counts"][kind] >= 1 and set(result["counts"]) == set(trace.GAP_KINDS)
    assert schema.errors(result, schema.load_schema("trace-matrix.schema.json"), base="trace-matrix.schema.json") == []


def test_an_assumed_requirement_is_a_gap_until_the_user_confirms_it(ws, actor):
    records.add(ws, actor, "requirement", dict(req(), provenance="assumed"))
    records.add(ws, actor, "criterion", ac())
    records.add(ws, actor, "testcase", tc())
    trace.link(ws, actor, "TC-001", "automated_by", TEST_ID)
    assert {"kind": "req_only_characterization_or_assumed", "id": "REQ-001"} in gaps(ws)


def test_a_complete_chain_has_no_gap_and_is_proven(ws, actor):
    chain(ws, actor)
    red = record_run(ws, actor, "A", COMMIT_A, [(TEST_ID, "failed", {})], 1)
    record_run(ws, actor, "B", COMMIT_A, [(TEST_ID, "passed", {})], 2)
    trace.link(ws, actor, "TC-001", "red_check", red)
    result = trace.matrix(state.load(ws), COMMIT_A)
    assert result["gaps"] == [] and result["requirements"][0]["proven"] is True
    assert result["testcases"][0]["status"] == "passed" and result["testcases"][0]["red_check"] is True
    assert result["requirements"][0]["criteria"] == [{"id": "AC-001.1", "testcases": ["TC-001"]}]


def test_a_characterization_test_never_proves_a_requirement(ws, actor):
    chain(ws, actor, oracle="characterization")
    record_run(ws, actor, "A", COMMIT_A, [(TEST_ID, "passed", {})], 1)
    assert trace.matrix(state.load(ws), COMMIT_A)["requirements"][0]["proven"] is False


def test_an_open_finding_that_violates_a_criterion_is_listed_under_its_requirement(ws, actor):
    chain(ws, actor)
    findings.add(ws, actor, base())
    trace.link(ws, actor, "FND-001", "violates", "AC-001.1")
    row = trace.matrix(state.load(ws), COMMIT_A)["requirements"][0]
    assert row["findings"] == ["FND-001"]


def test_flaky_latest_result_is_failed_in_the_matrix(ws, actor):
    chain(ws, actor)
    record_run(ws, actor, "A", COMMIT_A, [(TEST_ID, "passed", {"flaky": True})], 1)
    case = trace.matrix(state.load(ws), COMMIT_A)["testcases"][0]
    assert case["status"] == "failed" and case["tests"][0]["flaky"] is True


def test_result_on_another_commit_is_not_the_latest(ws, actor):
    chain(ws, actor)
    record_run(ws, actor, "A", COMMIT_A, [(TEST_ID, "passed", {})], 1)
    record_run(ws, actor, "B", COMMIT_B, [(TEST_ID, "failed", {})], 2)
    st = state.load(ws)
    assert trace.matrix(st, COMMIT_A)["testcases"][0]["status"] == "passed"
    assert trace.matrix(st, COMMIT_B)["testcases"][0]["status"] == "failed"
    assert trace.matrix(st, "c" * 40)["testcases"][0]["status"] is None
    assert {"kind": "tc_never_executed", "id": "TC-001"} in trace.matrix(st, "c" * 40)["gaps"]
    assert [t["commit"] for t in trace.matrix(st, COMMIT_A)["testcases"][0]["tests"]] == [COMMIT_A]
    refused("target_required", trace.matrix, st, None)  # commits are never combined
    refused("target_required", trace.matrix, st, COMMIT_A[:8])


def test_the_latest_result_on_a_commit_decides(ws, actor):
    chain(ws, actor)
    record_run(ws, actor, "A", COMMIT_A, [(TEST_ID, "passed", {})], 1)
    record_run(ws, actor, "B", COMMIT_A, [(TEST_ID, "failed", {})], 2)
    assert trace.matrix(state.load(ws), COMMIT_A)["testcases"][0]["status"] == "failed"


def test_a_test_case_with_one_unexecuted_test_is_not_passed(ws, actor):
    chain(ws, actor)
    trace.link(ws, actor, "TC-001", "automated_by", "tests.test_discount::test_second")
    record_run(ws, actor, "A", COMMIT_A, [(TEST_ID, "passed", {})], 1)
    assert trace.matrix(state.load(ws), COMMIT_A)["testcases"][0]["status"] == "not_run"


def test_a_skipped_latest_result_is_a_gap(ws, actor):
    chain(ws, actor)
    record_run(ws, actor, "A", COMMIT_A, [(TEST_ID, "not_run", {})], 1)
    assert {"kind": "tc_never_executed", "id": "TC-001"} in gaps(ws)


def test_an_abbreviated_target_is_resolved_once_against_the_recorded_commits(ws, actor):
    chain(ws, actor)
    record_run(ws, actor, "A", COMMIT_A, [(TEST_ID, "passed", {})], 1)
    record_run(ws, actor, "B", "ab" + "c" * 38, [(TEST_ID, "failed", {})], 2)
    record_run(ws, actor, "C", COMMIT_B, [(TEST_ID, "passed", {})], 3)
    st = state.load(ws)
    assert trace.resolve_target(ws, st, "b" * 7) == COMMIT_B and trace.resolve_target(ws, st, COMMIT_A.upper()) == COMMIT_A
    assert trace.resolve_target(ws, st, "d" * 40) == "d" * 40  # a full id is taken as given
    assert refused("target_unknown", trace.resolve_target, ws, st, "1234567").details["candidates"] == []
    refused("target_invalid", trace.resolve_target, ws, st, "aaaaaa")  # under 7 digits
    refused("target_invalid", trace.resolve_target, ws, st, "not-hex")


def test_an_ambiguous_abbreviation_names_its_candidates(ws, actor):
    chain(ws, actor)
    record_run(ws, actor, "A", "abcdef0" + "1" * 33, [(TEST_ID, "passed", {})], 1)
    record_run(ws, actor, "B", "abcdef0" + "2" * 33, [(TEST_ID, "passed", {})], 2)
    error = refused("target_ambiguous", trace.resolve_target, ws, state.load(ws), "abcdef0")
    assert error.details["candidates"] == ["abcdef0" + "1" * 33, "abcdef0" + "2" * 33]


def test_a_recorded_short_commit_never_matches_by_prefix(ws, actor):
    chain(ws, actor)
    record_run(ws, actor, "A", "abcdef0", [(TEST_ID, "passed", {})], 1)
    st = state.load(ws)
    refused("target_unknown", trace.resolve_target, ws, st, "abcdef0")
    assert trace.matrix(st, "abcdef0" + "9" * 33)["testcases"][0]["status"] is None


def test_no_target_means_the_current_commit_and_refuses_when_it_is_unknown(ws, actor, monkeypatch):
    st = state.load(ws)
    monkeypatch.setattr(trace.gitinfo, "git_info", lambda root: ({"commit": COMMIT_B}, None))
    assert trace.resolve_target(ws, st, None) == COMMIT_B
    monkeypatch.setattr(trace.gitinfo, "git_info", lambda root: (None, "not a git repository"))
    refused("target_unavailable", trace.resolve_target, ws, st, None)


def test_a_dirty_tree_run_shows_dirty_on_its_row(ws, actor):
    chain(ws, actor)
    data = test_compare._execution(exe_id("A"))
    data["started_at"] = data["ended_at"] = "2026-11-10T10:01:00.000Z"
    data["git"] = {"commit": COMMIT_A, "branch": None, "dirty": True, "diff_hash": "sha256:" + "1" * 64}
    ledger.append(ws, actor, "execution.recorded", data)
    ledger.append(ws, actor, "result.recorded", test_compare._result(1, exe_id("A"), TEST_ID, "passed"))
    row = trace.matrix(state.load(ws), COMMIT_A)["testcases"][0]["tests"][0]
    assert (row["dirty"], row["diff_hash"]) == (True, "sha256:" + "1" * 64) and row["status"] == "passed"
    record_run(ws, actor, "B", COMMIT_B, [(TEST_ID, "passed", {})], 2)
    clean = trace.matrix(state.load(ws), COMMIT_B)["testcases"][0]["tests"][0]
    assert clean["dirty"] is False and "diff_hash" not in clean
    assert trace.matrix(state.load(ws), "c" * 40)["testcases"][0]["tests"][0]["dirty"] is None


@pytest.mark.parametrize("dirty", [True, False])
def test_the_head_default_reports_whether_the_tree_is_dirty(ws, actor, monkeypatch, dirty):
    chain(ws, actor)
    monkeypatch.setattr(trace.gitinfo, "git_info", lambda root: ({"commit": COMMIT_A, "dirty": dirty}, None))
    st = state.load(ws)
    facts = trace.target_facts(ws, st, None)
    assert facts == {"commit": COMMIT_A, "source": "head", "dirty": dirty}
    result, path = trace.write_matrix(ws, st, facts["commit"], target_source="head", tree_dirty=dirty)
    assert (result["target_source"], result["tree_dirty"]) == ("head", dirty)
    assert schema.errors(json.loads(path.read_text()), schema.load_schema("trace-matrix.schema.json"), base="trace-matrix.schema.json") == []
    given = trace.target_facts(ws, st, COMMIT_A)
    assert given == {"commit": COMMIT_A, "source": "given", "dirty": None}
    assert "tree_dirty" not in trace.matrix(st, COMMIT_A, target_source="given", tree_dirty=None)
    assert "target_source" not in trace.matrix(st, COMMIT_A)


def run_with_status(ws, actor, letter, run_status, row_status, minute, *, reason=None, diff_hash="sha256:" + "0" * 64, commit=COMMIT_A):
    """An execution of ``run_status`` on ``commit`` holding one result of TEST_ID that reads ``row_status``."""
    data = test_compare._execution(exe_id(letter))
    data.update(status=run_status, reason=reason)
    data["started_at"] = data["ended_at"] = "2026-11-10T10:%02d:00.000Z" % minute
    data["git"] = {"commit": commit, "branch": None, "dirty": diff_hash != "sha256:" + "0" * 64, "diff_hash": diff_hash}
    ledger.append(ws, actor, "execution.recorded", data)
    ledger.append(ws, actor, "result.recorded", test_compare._result(minute * 100, exe_id(letter), TEST_ID, row_status))
    return exe_id(letter)


@pytest.mark.parametrize("run_status,reason,gap", [
    ("inconclusive", "exit_code_mismatch", "tc_last_blocked_or_inconclusive"),
    ("inconclusive", "timeout", "tc_last_blocked_or_inconclusive"),
    ("inconclusive", "network_not_isolated", "tc_last_blocked_or_inconclusive"),
    ("blocked", "policy_denied", "tc_last_blocked_or_inconclusive"),
    ("not_run", "all_skipped", "tc_never_executed"),
])
def test_a_run_that_did_not_conclude_gives_its_status_to_every_row(ws, actor, run_status, reason, gap):
    """K1: rows that read passed in a run that neither passed nor failed say nothing about the tests."""
    chain(ws, actor)
    record_run(ws, actor, "A", COMMIT_A, [(TEST_ID, "passed", {})], 1)
    run_with_status(ws, actor, "B", run_status, "passed", 2, reason=reason)
    result = trace.matrix(state.load(ws), COMMIT_A)
    case = result["testcases"][0]
    assert case["tests"][0]["status"] == run_status and case["tests"][0]["execution"] == exe_id("B")
    assert case["status"] == run_status and {"kind": gap, "id": "TC-001"} in result["gaps"]
    assert schema.errors(result, schema.load_schema("trace-matrix.schema.json"), base="trace-matrix.schema.json") == []


@pytest.mark.parametrize("run_status,row_status", [("passed", "passed"), ("failed", "failed"), ("failed", "passed"), ("passed", "failed")])
def test_a_run_that_concluded_keeps_the_rows_own_status(ws, actor, run_status, row_status):
    chain(ws, actor)
    run_with_status(ws, actor, "A", run_status, row_status, 1, reason="assertion" if run_status == "failed" else None)
    assert trace.matrix(state.load(ws), COMMIT_A)["testcases"][0]["tests"][0]["status"] == row_status


def test_a_failed_row_in_an_inconclusive_run_reads_inconclusive(ws, actor):
    chain(ws, actor)
    run_with_status(ws, actor, "A", "inconclusive", "failed", 1, reason="timeout")
    assert trace.matrix(state.load(ws), COMMIT_A)["testcases"][0]["status"] == "inconclusive"


def test_with_a_diff_hash_only_runs_on_that_tree_count(ws, actor):
    """K1: the gate evaluates the working tree itself; a run on another uncommitted tree at the same commit is not this code."""
    chain(ws, actor)
    here, there = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    run_with_status(ws, actor, "A", "failed", "failed", 1, reason="assertion", diff_hash=here)
    run_with_status(ws, actor, "B", "passed", "passed", 2, diff_hash=there)
    st = state.load(ws)
    assert trace.matrix(st, COMMIT_A)["testcases"][0]["status"] == "passed"   # no tree filter: the latest run on the commit
    assert trace.matrix(st, COMMIT_A, diff_hash=here)["testcases"][0]["status"] == "failed"
    assert trace.matrix(st, COMMIT_A, diff_hash=there)["testcases"][0]["status"] == "passed"
    assert trace.matrix(st, COMMIT_A, diff_hash="sha256:" + "3" * 64)["testcases"][0]["status"] is None


def test_matrix_does_not_change_the_state_it_reads(ws, actor):
    chain(ws, actor)
    record_run(ws, actor, "A", COMMIT_A, [(TEST_ID, "passed", {})], 1)
    st = state.load(ws)
    snapshot_before = json.dumps(st, sort_keys=True)
    trace.matrix(st, COMMIT_A)
    assert json.dumps(st, sort_keys=True) == snapshot_before


def test_matrix_file_is_byte_stable(ws, actor):
    chain(ws, actor)
    record_run(ws, actor, "A", COMMIT_A, [(TEST_ID, "passed", {})], 1)
    findings.add(ws, actor, base())
    first = trace.write_matrix(ws, state.load(ws), COMMIT_A)[1]
    data = first.read_bytes()
    assert first == ws.path("trace", "matrix.json")
    assert trace.write_matrix(ws, state.load(ws), COMMIT_A)[1].read_bytes() == data
    assert schema.errors(json.loads(data), schema.load_schema("trace-matrix.schema.json"), base="trace-matrix.schema.json") == []
    assert data.endswith(b"\n") and json.loads(data)["target"] == COMMIT_A
    assert data == config_bytes(json.loads(data))   # K7: the kernel's own writer, as for the files under state/


# --------------------------------------------------------------------------- the event, the state and the commands

def test_a_link_event_accepts_via_and_nothing_else_new():
    def problems(data):
        return schema.errors(data, schema.load_schema("events/trace.linked.schema.json"), base="events/trace.linked.schema.json")

    good = {"source": "TC-001", "relation": "automated_by", "target": TEST_ID, "via": "map"}
    assert problems(good) == [] and problems({k: v for k, v in good.items() if k != "via"}) == []  # older events carry no via
    assert problems(dict(good, relation="red_check", via="manual")) == []
    assert problems(dict(good, via="guess")) != [] and problems(dict(good, extra=1)) != []


def test_a_link_with_and_without_via_is_one_link(ws, actor):
    several_cases(ws, actor, 1)
    ledger.append(ws, actor, "trace.linked", {"source": "TC-001", "relation": "automated_by", "target": TEST_ID})
    ledger.append(ws, actor, "trace.linked", {"source": "TC-001", "relation": "automated_by", "target": TEST_ID, "via": "tag"})
    assert [l for l in state.load(ws)["links"] if l["relation"] == "automated_by"] == [
        {"source": "TC-001", "relation": "automated_by", "target": TEST_ID, "event": state.load(ws)["links"][-1]["event"]}]


def test_the_commands_link_scan_and_write_the_matrix(ws, actor, qa_cli, tmp_path):
    several_cases(ws, actor, 1)
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_discount.py").write_text("def test_TC001_cap():\n    pass\n")
    code, out = qa_cli("trace", "scan", "--root", str(root))
    assert code == 0 and [l["target"] for l in out["linked"]] == ["tests.test_discount::test_TC001_cap"]
    code, out = qa_cli("trace", "link", "TC-001", "automated_by", "tests/test_discount.py::test_second")
    assert (code, out["created"], out["target"]) == (0, True, "tests.test_discount::test_second")
    code, out = qa_cli("trace", "link", "TC-001", "addressed_by", "FIX-001")
    assert (code, out["error"]["code"]) == (2, "invalid_link")
    code, out = qa_cli("trace", "matrix", "--target", COMMIT_A)
    assert code == 0 and out["path"] == ".qa/trace/matrix.json" and out["counts"]["tc_never_executed"] == 1
    assert (out["target_source"], "tree_dirty" in out) == ("given", False)
    assert out["target"] == COMMIT_A and set(out["counts"]) == set(trace.GAP_KINDS)
    first = (ws.root / out["path"]).read_bytes()
    assert qa_cli("trace", "matrix", "--target", COMMIT_A)[0] == 0 and (ws.root / out["path"]).read_bytes() == first
    code, out = qa_cli("trace", "scan")  # the default root is the workspace root
    assert code == 0 and out["linked"] == []
