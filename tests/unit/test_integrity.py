from __future__ import annotations

import hashlib
import json
import shutil

import pytest

from helpers import KERNEL, WRITE_JUNIT, req
from qa import canonical, execution, ids, integrity, ledger, state


@pytest.fixture
def run(trusted, actor, py):
    return execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))


def _dir(ws, r):
    return ws.path("evidence", r["execution"])


def _kinds(problems):
    return {(p.kind, p.where) for p in problems}


def _hand_appended_line(ws, session, type_, data, actor_session=None):
    """A ledger line with the right seq, prev and hash, as a hand edit would write it."""
    path = ws.path("ledger", session + ".jsonl")
    lines = path.read_bytes().splitlines() if path.exists() else []
    head = json.loads(lines[-1]) if lines else None
    envelope = {
        "v": 1,
        "seq": head["seq"] + 1 if head else 1,
        "id": ids.new_id("EVT"),
        "ts": ids.format_ts(ids.now_utc()),
        "type": type_,
        "actor": {"role": "verifier", "skill": None, "session": actor_session or session},
        "data": data,
        "prev": head["hash"] if head else None,
    }
    envelope["hash"] = canonical.hash_obj(envelope)
    with open(path, "ab") as handle:
        handle.write(canonical.dumps(envelope) + b"\n")


# --------------------------------------------------------------------------- evidence

def test_clean_evidence_has_no_problems(trusted, run):
    assert integrity.verify_evidence(trusted, state.load(trusted)) == []


def test_editing_one_byte_of_stdout_is_modified(trusted, run):
    log = _dir(trusted, run) / "stdout.log"
    log.write_bytes(log.read_bytes() + b"x")
    assert _kinds(integrity.verify_evidence(trusted, state.load(trusted))) == {("modified", run["execution"] + "/stdout.log")}


def test_deleting_stdout_is_missing(trusted, run):
    (_dir(trusted, run) / "stdout.log").unlink()
    assert _kinds(integrity.verify_evidence(trusted, state.load(trusted))) == {("missing", run["execution"] + "/stdout.log")}


def test_a_nested_report_copy_is_checked_by_relative_path(trusted, run):
    copy = _dir(trusted, run) / "reports" / "junit.xml"
    copy.write_text("tampered")
    assert _kinds(integrity.verify_evidence(trusted, state.load(trusted))) == {("modified", run["execution"] + "/reports/junit.xml")}


def test_missing_manifest_is_one_problem_plus_each_absent_file(trusted, run):
    directory = _dir(trusted, run)
    (directory / "manifest.json").unlink()
    (directory / "stdout.log").unlink()
    (directory / "stderr.log").write_text("edited")   # with no manifest, only presence can be checked
    exe = run["execution"]
    assert _kinds(integrity.verify_evidence(trusted, state.load(trusted))) == {
        ("missing", exe + "/manifest.json"), ("missing", exe + "/stdout.log")}


def test_modified_manifest_is_one_problem_and_its_entries_are_not_trusted(trusted, run):
    directory = _dir(trusted, run)
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["stdout.log"]["sha256"] = "sha256:" + "0" * 64    # would flag stdout.log if the entries were used
    (directory / "manifest.json").write_text(json.dumps(manifest))
    (directory / "stderr.log").unlink()
    exe = run["execution"]
    assert _kinds(integrity.verify_evidence(trusted, state.load(trusted))) == {
        ("modified", exe + "/manifest.json"), ("missing", exe + "/stderr.log")}


def test_intact_manifest_is_compared_entry_by_entry(trusted, run):
    directory = _dir(trusted, run)
    (directory / "stderr.log").write_text("edited")
    (directory / "stdout.log").unlink()
    exe = run["execution"]
    assert _kinds(integrity.verify_evidence(trusted, state.load(trusted))) == {
        ("modified", exe + "/stderr.log"), ("missing", exe + "/stdout.log")}


def test_ingested_file_is_checked_by_its_sha256(trusted, actor, tmp_path, run):
    other = tmp_path / "x.xml"
    other.write_text('<testsuite><testcase classname="c" name="u"/></testsuite>')
    execution.ingest_file(trusted, actor, "junit", other, from_execution=run["execution"])
    copy = _dir(trusted, run) / "ingested" / "x.xml"
    exe = run["execution"]
    assert integrity.verify_evidence(trusted, state.load(trusted)) == []
    copy.write_text("tampered")
    assert _kinds(integrity.verify_evidence(trusted, state.load(trusted))) == {("modified", exe + "/ingested/x.xml")}
    copy.unlink()
    assert _kinds(integrity.verify_evidence(trusted, state.load(trusted))) == {("missing", exe + "/ingested/x.xml")}


def test_blocked_execution_has_no_evidence_to_check(trusted, actor):
    execution.execute(trusted, actor, execution.RunRequest(command=["git", "push"]))
    assert integrity.verify_evidence(trusted, state.load(trusted)) == []


@pytest.mark.parametrize("bad", ["/etc/passwd", "../outside", "reports/../../outside", "..", ""])
def test_unsafe_path_in_the_events_files_is_invalid_and_never_opened(trusted, run, bad):
    (_dir(trusted, run) / "manifest.json").unlink()
    st = state.load(trusted)
    st["executions"][run["execution"]]["evidence_manifest"]["files"].append(bad)
    problems = integrity.verify_evidence(trusted, st)
    assert ("invalid", run["execution"] + "/" + bad) in _kinds(problems)


@pytest.mark.parametrize("bad", ["/etc/passwd", "../outside"])
def test_unsafe_path_in_an_intact_manifest_is_invalid_and_never_opened(trusted, run, bad, monkeypatch):
    directory = _dir(trusted, run)
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest[bad] = {"sha256": "sha256:" + "0" * 64, "size": 1, "media_type": "text/plain", "redactions": 0}
    data = canonical.dumps(manifest)
    (directory / "manifest.json").write_bytes(data)
    st = state.load(trusted)
    st["executions"][run["execution"]]["evidence_manifest"]["sha256"] = canonical.sha256_bytes(data)
    opened = []
    real = integrity.sha256_file
    monkeypatch.setattr(integrity, "sha256_file", lambda p: opened.append(str(p)) or real(p))
    problems = integrity.verify_evidence(trusted, st)
    assert _kinds(problems) == {("invalid", run["execution"] + "/" + bad)}
    assert not any(bad.lstrip("/") in path for path in opened)


def test_unsafe_ingested_path_is_invalid_and_never_opened(trusted, run):
    st = state.load(trusted)
    st["executions"][run["execution"]]["ingested"].append({"file": "../../x", "sha256": "sha256:" + "0" * 64})
    assert _kinds(integrity.verify_evidence(trusted, st)) == {("invalid", run["execution"] + "/../../x")}


def test_a_manifest_that_is_not_an_object_is_invalid(trusted, run):
    directory = _dir(trusted, run)
    data = b"[1, 2]"
    (directory / "manifest.json").write_bytes(data)
    st = state.load(trusted)
    st["executions"][run["execution"]]["evidence_manifest"]["sha256"] = canonical.sha256_bytes(data)
    assert _kinds(integrity.verify_evidence(trusted, st)) == {("invalid", run["execution"] + "/manifest.json")}


# --------------------------------------------------------------------------- kernel manifest

@pytest.fixture
def kernel(tmp_path):
    """A vendored-layout copy of the kernel: _kernel/qa next to _kernel/schemas."""
    root = tmp_path / "_kernel"
    root.mkdir()
    shutil.copytree(str(KERNEL / "qa"), str(root / "qa"))
    (root / "schemas").mkdir()
    (root / "schemas" / "a.schema.json").write_text("{}")
    (root / "policies").mkdir()
    (root / "policies" / "b.json").write_text("{}")
    return root


def test_kernel_files_excludes_caches_and_the_manifest(kernel):
    (kernel / "qa" / "__pycache__").mkdir(exist_ok=True)
    (kernel / "qa" / "__pycache__" / "x.cpython-39.pyc").write_bytes(b"x")
    (kernel / "qa" / "__pycache__" / "x.lock").write_bytes(b"x")   # no .pyc suffix: only the directory rule drops it
    (kernel / "qa" / "stray.pyc").write_bytes(b"x")
    (kernel / "MANIFEST.sha256").write_text("")
    relative = [p.relative_to(kernel).as_posix() for p in integrity.kernel_files(kernel)]
    assert "qa/errors.py" in relative and "schemas/a.schema.json" in relative
    assert not [r for r in relative if "__pycache__" in r or r.endswith(".pyc") or r == "MANIFEST.sha256"]
    assert relative == sorted(relative)


def test_kernel_files_skips_a_symlink(kernel, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("x")
    (kernel / "qa" / "link.py").symlink_to(outside)
    (kernel / "qa" / "link_dir").symlink_to(tmp_path)
    relative = [p.relative_to(kernel).as_posix() for p in integrity.kernel_files(kernel)]
    assert "qa/link.py" not in relative and "qa/link_dir" not in relative


def test_write_manifest_format(kernel):
    path = integrity.write_manifest(kernel)
    assert path == kernel / "MANIFEST.sha256"
    raw = path.read_bytes()
    assert raw.endswith(b"\n") and b"\r" not in raw
    lines = raw.decode("utf-8").splitlines()
    names = [line.split("  ", 1)[1] for line in lines]
    assert names == sorted(names) and "qa/errors.py" in names
    for line in lines:
        digest, name = line.split("  ", 1)
        assert digest == hashlib.sha256((kernel / name).read_bytes()).hexdigest()


def test_write_manifest_then_kernel_integrity_is_ok(kernel):
    path = integrity.write_manifest(kernel)
    assert integrity.kernel_integrity(kernel) == {
        "status": "ok", "source_checkout": False, "manifest_sha256": canonical.sha256_file(path),
        "modified": [], "missing": [], "extra": []}


def test_editing_a_kernel_file_is_modified_with_its_path(kernel):
    integrity.write_manifest(kernel)
    (kernel / "qa" / "errors.py").write_text("# tampered\n")
    report = integrity.kernel_integrity(kernel)
    assert report["status"] == "modified" and report["modified"] == ["qa/errors.py"]


def test_deleted_and_added_kernel_files(kernel):
    integrity.write_manifest(kernel)
    (kernel / "qa" / "errors.py").unlink()
    (kernel / "policies" / "new.json").write_text("{}")
    report = integrity.kernel_integrity(kernel)
    assert (report["status"], report["missing"], report["extra"]) == ("modified", ["qa/errors.py"], ["policies/new.json"])


def test_source_tree_is_absent_with_source_checkout():
    report = integrity.kernel_integrity()
    assert report == {"status": "absent", "source_checkout": True, "manifest_sha256": None,
                      "modified": [], "missing": [], "extra": []}


def test_vendored_copy_without_a_manifest_is_absent_and_not_a_source_checkout(kernel):
    report = integrity.kernel_integrity(kernel)
    assert (report["status"], report["source_checkout"]) == ("absent", False)


@pytest.mark.parametrize("bad", ["not a manifest line\n", "%s  /etc/passwd\n" % ("a" * 64),
                                 "%s  ../outside\n" % ("a" * 64), "%s qa/errors.py\n" % ("a" * 64),
                                 "%s  qa/errors.py" % ("a" * 64)])
def test_malformed_manifest_line_is_modified(kernel, bad):
    (kernel / "MANIFEST.sha256").write_text(bad)
    report = integrity.kernel_integrity(kernel)
    assert report["status"] == "modified" and report["modified"] == ["MANIFEST.sha256"]
    assert report["manifest_sha256"] == canonical.sha256_file(kernel / "MANIFEST.sha256")


def test_manifest_with_an_undecodable_line_is_modified(kernel):
    (kernel / "MANIFEST.sha256").write_bytes(b"\xff\xfe\n")
    assert integrity.kernel_integrity(kernel)["modified"] == ["MANIFEST.sha256"]


# --------------------------------------------------------------------------- problems

def test_a_clean_workspace_has_no_problems(trusted, run):
    assert integrity.problems(trusted, state.load(trusted)) == []


def test_ledger_tampering_is_reported(trusted, actor, run):
    path = trusted.path("ledger", actor.session + ".jsonl")
    path.write_bytes(path.read_bytes().replace(b'"qa-test-execution"', b'"qa-test-tampered!"', 1))
    assert any(p.kind == "modified" for p in integrity.problems(trusted, state.load(trusted)))


def test_hand_appended_event_that_breaks_its_schema_is_invalid(trusted, actor, run):
    _hand_appended_line(trusted, actor.session, "execution.recorded", {"id": "nope"})
    problems = integrity.problems(trusted, state.load(trusted))
    assert [p.kind for p in problems] == ["invalid"] and problems[0].where.startswith(actor.session + ".jsonl")


def test_event_type_with_no_schema_file_is_invalid_not_a_crash(trusted, actor, run):
    _hand_appended_line(trusted, actor.session, "made.up", {})
    assert [p.kind for p in integrity.problems(trusted, state.load(trusted))] == ["invalid"]


def test_event_whose_actor_session_differs_from_its_file_is_invalid(trusted, actor, run):
    other = "S-" + "1" * 26
    _hand_appended_line(trusted, actor.session, "workspace.initialized",
                        {"kernel_version": "x", "schema_version": "1", "config_hash": "sha256:" + "0" * 64},
                        actor_session=other)
    assert [p.kind for p in integrity.problems(trusted, state.load(trusted))] == ["invalid"]


def test_anchors_come_from_the_gate_with_the_highest_number(trusted, actor, run):
    st = state.load(trusted)
    good = ledger.heads(trusted)
    bad = {actor.session: {"seq": 1, "hash": "sha256:" + "f" * 64}}
    # GATE-999 sorts last as text; GATE-1000 is the highest number.
    st["gates"] = {"GATE-999": {"heads": bad}, "GATE-1000": {"heads": good}}
    assert integrity.problems(trusted, st) == []
    st["gates"] = {"GATE-999": {"heads": good}, "GATE-1000": {"heads": bad}}
    assert [p.kind for p in integrity.problems(trusted, st)] == ["modified"]


def test_no_gates_means_no_anchors(trusted, actor, run, monkeypatch):
    seen = []
    real = ledger.verify_chains
    monkeypatch.setattr(ledger, "verify_chains", lambda ws, anchors=None: seen.append(anchors) or real(ws, anchors))
    integrity.problems(trusted, state.load(trusted))
    assert seen == [None]


def test_evidence_problems_are_included(trusted, run):
    (_dir(trusted, run) / "stdout.log").unlink()
    assert ("missing", run["execution"] + "/stdout.log") in _kinds(integrity.problems(trusted, state.load(trusted)))


def _kernel_report(**changes):
    report = {"status": "modified", "source_checkout": False, "manifest_sha256": "sha256:" + "0" * 64,
              "modified": [], "missing": [], "extra": []}
    report.update(changes)
    return report


@pytest.mark.parametrize("field, kind", [("modified", "modified"), ("missing", "missing"), ("extra", "modified")])
def test_kernel_differences_become_problems(trusted, run, monkeypatch, field, kind):
    monkeypatch.setattr(integrity, "kernel_integrity", lambda kernel_dir=None: _kernel_report(**{field: ["qa/x.py"]}))
    problems = integrity.problems(trusted, state.load(trusted))
    assert [p.kind for p in problems] == [kind] and "qa/x.py" in problems[0].where


def test_an_absent_kernel_manifest_gives_no_problem(trusted, run, monkeypatch):
    monkeypatch.setattr(integrity, "kernel_integrity", lambda kernel_dir=None: _kernel_report(status="absent"))
    assert integrity.problems(trusted, state.load(trusted)) == []
