from __future__ import annotations

import os
import shutil
import subprocess
import unicodedata

import pytest

from helpers import write_event_at
from qa import authority, authz, changes, config, ids, ledger, state
from qa.canonical import sha256_bytes
from qa.errors import QaError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True)


@pytest.fixture
def project(ws, actor):
    """A git repository holding one tracked test and one tracked production file, in a workspace whose config is accepted."""
    config.record_initialized(ws, actor)
    git(ws.root, "init", "-q", "-b", "main")
    (ws.root / "tests").mkdir()
    (ws.root / "src").mkdir()
    (ws.root / "tests" / "test_old.py").write_text("def test_old():\n    pass\n")
    (ws.root / "src" / "app.py").write_text("x = 1\n")
    git(ws.root, "add", "tests", "src")
    git(ws.root, "commit", "-q", "-m", "c1")
    return ws


def grant(ws, actor, paths=("tests/**",), **changes_):
    request = dict(environment_id="local", targets=[], activities=["write-tests"],
                   limits={"write-tests": {"paths": list(paths)}}, approval="ok", expires_at=None)
    request.update(changes_)
    return authz.grant(ws, actor, **request)["data"]["id"]


def refused(code, call, *args, **kw):
    with pytest.raises(QaError) as caught:
        call(*args, **kw)
    assert caught.value.code == code
    return caught.value


def write(ws, rel, text):
    path = ws.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def file_events(ws):
    return [e for e in ledger.iter_all_events(ws) if e["type"].startswith("file.")]


def test_record_needs_a_matching_write_tests_grant(project, actor):
    write(project, "tests/test_new.py", "def test_new():\n    pass\n")
    refused("write_not_authorized", changes.record, project, actor, ["tests/test_new.py"], kind="created", testcase=None)
    grant(project, actor)
    write(project, "src/other.py", "y = 2\n")
    refused("write_not_authorized", changes.record, project, actor, ["tests/test_new.py", "src/other.py"], kind="created", testcase=None)
    assert file_events(project) == []  # all or none: the matching path was not recorded either
    done = changes.record(project, actor, ["tests/test_new.py"], kind="created", testcase="TC-001")
    (event,) = file_events(project)
    assert event["type"] == "file.created" and event["data"]["path"] == "tests/test_new.py"
    assert event["data"]["sha256"] == sha256_bytes(b"def test_new():\n    pass\n") and event["data"]["testcase"] == "TC-001"
    assert done["files"][0]["path"] == "tests/test_new.py" and done["files"][0]["event"] == event["id"]


def test_a_revoked_or_expired_grant_authorizes_nothing(project, actor):
    write(project, "tests/test_new.py", "x")
    auth = grant(project, actor)
    authz.revoke(project, actor, auth, "done")
    refused("write_not_authorized", changes.record, project, actor, ["tests/test_new.py"], kind="created", testcase=None)
    assert file_events(project) == []


def test_a_grant_for_another_activity_authorizes_nothing(project, actor):
    write(project, "tests/test_new.py", "x")
    grant(project, actor, activities=["db-readonly"], limits={})
    refused("write_not_authorized", changes.record, project, actor, ["tests/test_new.py"], kind="created", testcase=None)


@pytest.mark.parametrize("path", ["CLAUDE.md", ".claude/x", ".vscode/settings.json", "sub/AGENTS.md"])
def test_protected_paths_are_refused(project, actor, path):
    write(project, path, "x")
    # A grant that reached the ledger by hand, covering everything: the protected list still wins.
    ledger.append(project, actor, "authorization.granted", {
        "id": "AUTH-900", "environment": "local", "targets": [], "activities": ["write-tests"],
        "limits": {"write-tests": {"paths": ["**", "*"]}}, "granted_by": "user", "approval_quote": "q",
        "granted_at": "2026-11-10T10:00:00.000Z", "expires_at": "2999-01-01T00:00:00.000Z"})
    refused("protected_path", changes.record, project, actor, [path], kind="created", testcase=None)
    assert file_events(project) == []


def test_modifying_a_tracked_test_needs_justification_and_approval(project, actor):
    grant(project, actor)
    write(project, "tests/test_old.py", "def test_old():\n    assert True\n")
    refused("justification_required", changes.record, project, actor, ["tests/test_old.py"], kind="modified", testcase=None)
    refused("approval_required", changes.record, project, actor, ["tests/test_old.py"], kind="modified", testcase=None,
            justification="the assertion was vacuous")
    refused("justification_required", changes.record, project, actor, ["tests/test_old.py"], kind="modified", testcase=None,
            approval="yes, change it", justification="  ")
    assert file_events(project) == []
    changes.record(project, actor, ["tests/test_old.py"], kind="modified", testcase=None,
                   justification="the assertion was vacuous", approval="yes, change it")
    assert [e["type"] for e in file_events(project)] == ["file.modified", "file.modification_approved"]
    approved = file_events(project)[1]
    assert approved["data"]["sha256"] == sha256_bytes(b"def test_old():\n    assert True\n")
    assert approved["data"]["justification"] == "the assertion was vacuous" and "authority_mac" in approved["data"]
    (row,) = changes.list_changes(project, state.load(project))
    assert row["approved"] is True and row["approval_required"] is True


def test_modifying_an_untracked_file_needs_no_approval(project, actor):
    grant(project, actor)
    write(project, "tests/test_new.py", "a")
    changes.record(project, actor, ["tests/test_new.py"], kind="created", testcase=None)
    write(project, "tests/test_new.py", "b")
    changes.record(project, actor, ["tests/test_new.py"], kind="modified", testcase=None)
    rows = changes.list_changes(project, state.load(project))
    assert [r["kind"] for r in rows] == ["created", "modified"] and all(r["approved"] for r in rows)


def test_a_tracked_modification_without_a_valid_approval_is_listed_as_unapproved(project, actor):
    grant(project, actor)
    write(project, "tests/test_old.py", "changed\n")
    changes.record(project, actor, ["tests/test_old.py"], kind="modified", testcase=None, justification="j", approval="a")
    unbacked = state.fold(ledger.iter_all_events(project), backed=lambda event: False)  # as on a machine without the key
    (row,) = changes.list_changes(project, unbacked)
    assert row["path"] == "tests/test_old.py" and row["approved"] is False and row["approval_required"] is True


def test_an_approval_for_other_bytes_does_not_approve(project, actor):
    grant(project, actor)
    write(project, "tests/test_old.py", "changed\n")
    changes.record(project, actor, ["tests/test_old.py"], kind="modified", testcase=None, justification="j", approval="a")
    ledger.append(project, actor, "file.modification_approved", {
        "path": "tests/test_old.py", "sha256": sha256_bytes(b"other"), "justification": "j", "approval": "a"})
    st = state.load(project)
    assert {"event": file_events(project)[-1]["id"], "kind": "stale_approval"} in st["violations"]
    (row,) = changes.list_changes(project, st)
    assert row["approved"] is True  # the genuine approval of these bytes still stands


def test_the_modification_event_always_folds_and_the_approval_is_signed():
    assert "file.modification_approved" in authority.CONFERRING
    assert not {"file.created", "file.modified"} & authority.EVENTS


def test_list_shows_edits_after_recording(project, actor):
    grant(project, actor)
    path = write(project, "tests/test_new.py", "one")
    changes.record(project, actor, ["tests/test_new.py"], kind="created", testcase=None)
    (row,) = changes.list_changes(project, state.load(project))
    assert (row["recorded_sha256"], row["current_sha256"], row["changed_since"]) == (sha256_bytes(b"one"),) * 2 + (False,)
    path.write_text("two")
    (row,) = changes.list_changes(project, state.load(project))
    assert row["current_sha256"] == sha256_bytes(b"two") and row["changed_since"] is True
    path.unlink()
    (row,) = changes.list_changes(project, state.load(project))
    assert row["current_sha256"] is None and row["changed_since"] is True


def test_recording_files_changes_the_state_hash_and_an_empty_key_does_not(project, actor):
    assert "files" in state.fold([]) and state.fold([])["files"] == {}
    grant(project, actor)
    write(project, "tests/test_new.py", "x")
    before = state.state_hash(state.load(project))
    changes.record(project, actor, ["tests/test_new.py"], kind="created", testcase=None)
    assert state.state_hash(state.load(project)) != before
    assert state.load(project)["files"]["tests/test_new.py"][0]["kind"] == "created"


def test_path_outside_the_project_or_through_a_symlink_is_refused(project, actor, tmp_path):
    grant(project, actor, paths=["tests/**", "link/**", "linked.py"])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "t.py").write_text("x")
    os.symlink(str(outside), str(project.root / "link"))
    os.symlink(str(outside / "t.py"), str(project.root / "linked.py"))
    for bad in ("../outside/t.py", "tests/../../outside/t.py", str(outside / "t.py"), "link/t.py", "linked.py", "tests/missing.py", "tests"):
        refused("invalid_path", changes.record, project, actor, [bad], kind="created", testcase=None)
    assert file_events(project) == []


def test_a_nul_or_empty_path_is_refused(project, actor):
    grant(project, actor)
    for bad in ("", "tests/a\0b"):
        refused("invalid_path", changes.record, project, actor, [bad], kind="created", testcase=None)
    refused("invalid_path", changes.record, project, actor, [], kind="created", testcase=None)


def test_an_unknown_kind_is_refused(project, actor):
    grant(project, actor)
    write(project, "tests/test_new.py", "x")
    refused("invalid_kind", changes.record, project, actor, ["tests/test_new.py"], kind="deleted", testcase=None)


def test_the_commands(project, actor, qa_cli):
    grant(project, actor)
    write(project, "tests/test_new.py", "x")
    code, out = qa_cli("changes", "record", "tests/test_new.py", "--kind", "created", "--tc", "TC-001")
    assert code == 0 and out["files"][0]["path"] == "tests/test_new.py"
    code, out = qa_cli("changes", "list")
    assert code == 0 and out["changes"][0]["changed_since"] is False and out["changes"][0]["testcase"] == "TC-001"
    write(project, "tests/test_old.py", "changed")
    code, out = qa_cli("changes", "record", "tests/test_old.py", "--kind", "modified")
    assert (code, out["error"]["code"]) == (2, "justification_required")
    code, out = qa_cli("changes", "record", "tests/test_old.py", "--kind", "modified", "--justification", "j", "--approval", "a")
    assert code == 0 and len(out["files"]) == 1


def test_creating_a_file_git_tracks_is_refused(project, actor):
    grant(project, actor)
    write(project, "tests/test_old.py", "changed\n")
    refused("already_tracked", changes.record, project, actor, ["tests/test_old.py"], kind="created", testcase=None)
    assert file_events(project) == []


def _same_file_under(project, variant):
    try:
        return os.path.samefile(str(project.root / variant), str(project.root / "tests" / "test_old.py"))
    except OSError:
        return False


def test_a_case_variant_of_a_tracked_file_is_the_same_file(project, actor):
    if not _same_file_under(project, "tests/TEST_OLD.py"):
        pytest.skip("this filesystem is case-sensitive")
    grant(project, actor)
    write(project, "tests/test_old.py", "changed\n")
    refused("justification_required", changes.record, project, actor, ["tests/TEST_OLD.py"], kind="modified", testcase=None)
    changes.record(project, actor, ["tests/TEST_OLD.py", "tests/test_old.py"], kind="modified", testcase=None,
                   justification="j", approval="a")
    assert list(state.load(project)["files"]) == ["tests/test_old.py"]  # one canonical key, one record
    assert len(state.load(project)["files"]["tests/test_old.py"]) == 1
    refused("already_tracked", changes.record, project, actor, ["tests/Test_Old.py"], kind="created", testcase=None)


def test_a_decomposed_spelling_of_a_tracked_file_is_the_same_file(project, actor):
    nfc = unicodedata.normalize("NFC", "tests/test_\u00e9.py")
    nfd = unicodedata.normalize("NFD", nfc)
    write(project, nfc, "a\n")
    git(project.root, "add", "tests")
    git(project.root, "commit", "-q", "-m", "c2")
    if not os.path.lexists(str(project.root / nfd)):
        pytest.skip("this filesystem keeps the two spellings apart")
    grant(project, actor)
    write(project, nfc, "b\n")
    refused("justification_required", changes.record, project, actor, [nfd], kind="modified", testcase=None)
    changes.record(project, actor, [nfd], kind="modified", testcase=None, justification="j", approval="a")
    assert list(state.load(project)["files"]) == [nfc]


@pytest.mark.parametrize("breakage,code", [("tracked_authority", "authority_refused"), ("ledger_unverified", "authority_refused"),
                                           ("authority_key_invalid", "authority_refused"), ("config_drift", "config_drift")])
def test_nothing_is_recorded_where_execution_would_refuse(project, actor, breakage, code):
    grant(project, actor)
    write(project, "tests/test_new.py", "x")
    if breakage == "tracked_authority":
        git(project.root, "add", "-f", ".qa")
        git(project.root, "commit", "-q", "-m", "qa")
    elif breakage == "ledger_unverified":
        session = next((project.qa_dir / "ledger").glob("*.jsonl"))
        session.write_bytes(session.read_bytes().replace(b'"write-tests"', b'"write-tests "', 1))
    elif breakage == "authority_key_invalid":
        (project.qa_dir / "authority.key").write_bytes(b"short")
    else:
        cfg = config.load_config(project)
        cfg["write_scopes"] = ["tests/**"]
        project.path("config.json").write_bytes(__import__("qa.workspace", fromlist=["x"]).config_bytes(cfg))
    with pytest.raises(QaError) as caught:
        changes.record(project, actor, ["tests/test_new.py"], kind="created", testcase=None)
    assert caught.value.code == code
    if code == "authority_refused":
        assert caught.value.details["limitations"] == [breakage]
    assert [e for e in file_events(project)] == []


def test_a_grant_this_machine_did_not_sign_authorizes_nothing(project, actor):
    write(project, "tests/test_new.py", "x")
    write_event_at(project, actor, ids.now_utc(), "authorization.granted", {
        "id": "AUTH-901", "environment": "local", "targets": [], "activities": ["write-tests"],
        "limits": {"write-tests": {"paths": ["tests/**"]}}, "granted_by": "user", "approval_quote": "q",
        "granted_at": "2026-11-10T10:00:00.000Z", "expires_at": "2999-01-01T00:00:00.000Z"}, signed=False)
    refused("write_not_authorized", changes.record, project, actor, ["tests/test_new.py"], kind="created", testcase=None)


def test_the_workspace_directory_is_never_recorded(project, actor):
    ledger.append(project, actor, "authorization.granted", {
        "id": "AUTH-902", "environment": "local", "targets": [], "activities": ["write-tests"],
        "limits": {"write-tests": {"paths": ["**"]}}, "granted_by": "user", "approval_quote": "q",
        "granted_at": "2026-11-10T10:00:00.000Z", "expires_at": "2999-01-01T00:00:00.000Z"})
    refused("protected_path", changes.record, project, actor, [".qa/config.json"], kind="modified", testcase=None)


def test_when_git_cannot_say_a_modification_is_recorded_as_tracked_with_its_approval(project, actor, monkeypatch):
    monkeypatch.setattr(changes.gitinfo, "tracked_files", lambda root: None)
    grant(project, actor)
    write(project, "tests/test_new.py", "x")  # not tracked, but nobody can tell
    refused("justification_required", changes.record, project, actor, ["tests/test_new.py"], kind="modified", testcase=None)
    changes.record(project, actor, ["tests/test_new.py"], kind="modified", testcase=None, justification="j", approval="a")
    assert [e["type"] for e in file_events(project)] == ["file.modified", "file.modification_approved"]
    assert file_events(project)[0]["data"]["tracked"] is True and file_events(project)[0]["data"]["git_unknown"] is True
    (row,) = changes.list_changes(project, state.load(project))
    assert row["approval_required"] is True and row["approved"] is True and row["git_unknown"] is True


def test_when_git_cannot_say_a_creation_is_refused_without_claiming_the_file_is_tracked(project, actor, monkeypatch):
    monkeypatch.setattr(changes.gitinfo, "tracked_files", lambda root: None)
    grant(project, actor)
    write(project, "tests/test_new.py", "x")
    caught = refused("tracked_unknown", changes.record, project, actor, ["tests/test_new.py"], kind="created", testcase=None)
    assert "cannot say whether" in caught.message and "tracks this file" not in caught.message
    assert "modified" in caught.message and file_events(project) == []


def test_a_case_variant_in_git_is_another_file_unless_the_filesystem_says_it_is_the_same(project, actor, monkeypatch):
    if _same_file_under(project, "tests/TEST_OLD.py"):
        pytest.skip("this filesystem is case-insensitive, so the variant is the same file")
    monkeypatch.setattr(changes.gitinfo, "tracked_files", lambda root: {"tests/TEST_OLD.py"})
    grant(project, actor)
    write(project, "tests/test_old.py", "changed\n")
    changes.record(project, actor, ["tests/test_old.py"], kind="created", testcase=None)  # a distinct file: not tracked
    assert file_events(project)[0]["type"] == "file.created"
