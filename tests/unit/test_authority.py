"""What a workspace's own files cannot back: a clone that ships its approvals (C1) and a ledger that does not verify (I2)."""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import timedelta

import pytest

from qa import authz, cli, config, doctor, execution, gate, gitinfo, ids, integrity, state, workspace


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], check=True, capture_output=True)


def _trusted_workspace(root, actor):
    ws = workspace.init_workspace(root)[0]
    cfg = workspace.default_config("x")
    cfg["project"]["trust"] = "trusted"
    config.write_config(ws, cfg)
    config.record_initialized(ws, actor)
    return ws


def _clone(tmp_path, actor, *tracked):
    """A real ``git clone`` of a repository that commits ``tracked`` from its ``.qa/``."""
    if shutil.which("git") is None:
        pytest.skip("tool missing: git")
    origin = tmp_path / "origin"
    _trusted_workspace(origin, actor)
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "add", "-f", *tracked)
    _git(origin, "commit", "-q", "-m", "ship the workspace")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    return workspace.find_workspace(clone, clone)


@pytest.fixture
def hostile(tmp_path, actor):
    return _clone(tmp_path, actor, ".qa")


def _refused(limitations, name):
    return any(text.startswith(name + ":") for text in limitations)


def test_a_clone_that_ships_its_ledger_runs_nothing(hostile, actor, py, tmp_path):
    # The shipped ledger accepts the shipped trusted configuration, so only the tracked state tells them apart.
    assert config.current_hash(hostile) == config.accepted_hash(state.load(hostile))
    marker = tmp_path / "ran"
    r = execution.execute(hostile, actor, execution.RunRequest(command=py.argv("open(%r, 'w')" % str(marker))))
    assert (r["status"], r["reason"]) == ("blocked", "not_authorized") and not marker.exists()
    assert _refused(r["limitations"], "tracked_authority")
    assert state.load(hostile)["executions"][r["execution"]]["authorization"] is None


def test_a_shipped_grant_covers_nothing(tmp_path, actor, py):
    origin = tmp_path / "origin"
    if shutil.which("git") is None:
        pytest.skip("tool missing: git")
    ws = _trusted_workspace(origin, actor)
    authz.grant(ws, actor, environment_id="local", targets=[], activities=["db-readonly"], limits={}, approval="ok",
                expires_at=ids.now_utc() + timedelta(days=1))
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "add", "-f", ".qa")
    _git(origin, "commit", "-q", "-m", "ship the grant")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    r = execution.execute(workspace.find_workspace(clone, clone), actor,
                          execution.RunRequest(command=py.argv("pass"), activity="db-readonly"))
    assert (r["status"], r["reason"]) == ("blocked", "not_authorized")


def test_static_analysis_still_runs_and_names_the_limitation(hostile, actor):
    r = execution.execute(hostile, actor, execution.RunRequest(command=["ls"], activity="static-analysis"))
    assert r["status"] == "passed" and _refused(r["limitations"], "tracked_authority")


def test_the_gate_does_not_count_a_shipped_ledger(hostile, actor):
    assert gate.evaluate(hostile, actor, profile="change")["data"]["verdict"] == "NOT_READY"


def test_doctor_names_the_refusal(hostile):
    report = doctor.doctor(hostile)["authority"]
    assert _refused(report["refused"], "tracked_authority")
    assert any(".qa/redaction.key" in text for text in report["refused"])


def test_init_names_the_refusal(hostile):
    code, body = cli._run(["--workspace", str(hostile.root), "init"], cli.Context())
    assert code == 0 and not body["created"] and _refused(body["limitations"], "tracked_authority")


def test_the_two_shareable_files_carry_no_refusal(tmp_path, actor):
    clone = _clone(tmp_path, actor, ".qa/.gitignore", ".qa/config.json")
    report = integrity.authority(clone)
    assert report == {"refused": [], "limitation": None}


def test_tracked_paths_do_not_depend_on_the_case_of_the_directory(tmp_path, actor):
    if shutil.which("git") is None:
        pytest.skip("tool missing: git")
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / ".QA").mkdir()
    (root / ".QA" / "NOTES").write_text("x")
    _git(root, "add", "-f", ".QA/NOTES")
    assert [p.lower() for p in gitinfo.tracked_under_qa(root)[0]] == [".qa/notes"]


def test_a_tree_git_does_not_track_is_the_stated_limitation(ws):
    expected = "not_a_git_repo" if shutil.which("git") else "git_unavailable"
    assert integrity.authority(ws) == {"refused": [], "limitation": expected}


def test_a_missing_git_is_the_stated_limitation(ws, monkeypatch):
    monkeypatch.setattr(gitinfo.shutil, "which", lambda name: None)
    assert integrity.authority(ws) == {"refused": [], "limitation": "git_unavailable"}


# --------------------------------------------------------------------------- I2: only a verifying ledger holds authority

def _forged_grant(ws, actor):
    """A well-formed ``authorization.granted`` line, hand-appended with a ``prev`` and a ``hash`` nothing computed."""
    now = ids.now_utc()
    data = authz.build_grant(config.load_config(ws), environment_id="local", targets=[], activities=["db-readonly"], limits={},
                             approval="ok", expires_at=now + timedelta(days=1), now=now)
    data["id"] = "AUTH-001"
    path = ws.path("ledger", actor.session + ".jsonl")
    last = json.loads(path.read_bytes().splitlines()[-1])
    forged = {"v": 1, "seq": last["seq"] + 1, "id": ids.new_id("EVT"), "ts": ids.format_ts(now), "type": "authorization.granted",
              "actor": last["actor"], "data": data, "prev": "sha256:" + "0" * 64, "hash": "sha256:" + "1" * 64}
    with open(str(path), "ab") as handle:
        handle.write(json.dumps(forged, sort_keys=True).encode("utf-8") + b"\n")


def test_a_grant_outside_the_chain_covers_nothing(trusted, actor, py):
    _forged_grant(trusted, actor)
    assert "AUTH-001" in state.load(trusted)["authorizations"]  # the fold still shows it, which is why the run must not trust it
    r = execution.execute(trusted, actor, execution.RunRequest(command=py.argv("pass"), activity="db-readonly"))
    assert (r["status"], r["reason"]) == ("blocked", "not_authorized") and _refused(r["limitations"], "ledger_unverified")


def test_an_edited_event_blocks_the_trusted_default_too(trusted, actor, py):
    path = trusted.path("ledger", actor.session + ".jsonl")
    path.write_bytes(path.read_bytes().replace(b'"role":"verifier"', b'"role":"auditor"'))
    r = execution.execute(trusted, actor, execution.RunRequest(command=py.argv("pass")))
    assert (r["status"], r["reason"]) == ("blocked", "not_authorized") and _refused(r["limitations"], "ledger_unverified")


def test_static_analysis_does_not_need_a_verifying_ledger(trusted, actor):
    _forged_grant(trusted, actor)
    r = execution.execute(trusted, actor, execution.RunRequest(command=["ls"], activity="static-analysis"))
    assert r["status"] == "passed" and _refused(r["limitations"], "ledger_unverified")


def test_a_stray_file_in_the_ledger_directory_is_no_refusal(trusted):
    trusted.path("ledger", ".DS_Store").write_bytes(b"x")
    assert integrity.authority(trusted)["refused"] == []
