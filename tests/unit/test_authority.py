"""What a workspace's own files cannot back: a clone that ships its approvals (C1) and a ledger that does not verify (I2)."""
from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
from datetime import timedelta

import pytest

from helpers import write_event_at
from qa import authority, authz, bundle, cli, config, container, doctor, execution, gate, gitinfo, ids, integrity, ledger, state, workspace
from qa.errors import QaError


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
    data["authority_mac"] = authority.sign(ws, "authorization.granted", data)  # a valid MAC does not excuse a broken chain
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


# --------------------------------------------------------------------------- machine-bound authority (the key on this machine)

LOCAL = {"id": "local", "class": "local"}
ENGINE = "ssh build.host-1 docker"
RECOVERY = "qa config accept"
BAD_MAC = "hmac-sha256:" + "0" * 64


def _granted(auth_id="AUTH-001", **over):
    now = ids.now_utc()
    data = {"id": auth_id, "environment": "local", "targets": [], "activities": ["db-readonly"], "limits": {}, "granted_by": "user",
            "approval_quote": "ok", "granted_at": ids.format_ts(now), "expires_at": ids.format_ts(now + timedelta(days=1))}
    data.update(over)
    return data


def _allows(ws):
    return authz.check(state.load(ws), config.load_config(ws), activity="db-readonly", environment=LOCAL, now=ids.now_utc())


def _grant(ws, actor):
    return authz.grant(ws, actor, environment_id="local", targets=[], activities=["db-readonly"], limits={}, approval="ok",
                       expires_at=ids.now_utc() + timedelta(days=1))


def _other_machine(ws, tmp_path, name="other"):
    """``ws``'s ``.qa`` copied without its key: the same ledger and config.json, and the other machine's own key."""
    root = tmp_path / name
    root.mkdir()
    shutil.copytree(str(ws.qa_dir), str(root / ".qa"))
    (root / ".qa" / "authority.key").write_bytes(b"k" * 32)
    return workspace.find_workspace(root, root)


def test_a_key_that_travels_with_the_directory_still_verifies(trusted, actor, tmp_path):
    # A known limit, pinned so that closing it is a decision: this is any whole-directory transfer outside git, and the
    # source may be a workspace someone prepared with a key of their own. tracked_authority sees only the git route.
    _grant(trusted, actor)
    root = tmp_path / "unpacked"
    root.mkdir()
    shutil.copytree(str(trusted.qa_dir), str(root / ".qa"))
    arrived = workspace.find_workspace(root, root)
    st = state.load(arrived)
    assert config.accepted_hash(st) == config.current_hash(arrived)
    assert config.check_drift(arrived, st, actor, "test-execution-local") is None
    assert _allows(arrived).allowed is True and config.load_config(arrived)["project"]["trust"] == "trusted"
    assert integrity.authority(arrived)["refused"] == []
    report = doctor.doctor(arrived)["authority"]
    assert report["key"] == "ok" and report["events_without_authority"] == 0
    assert "brings its key along" in report["limit"]


def test_grant_without_a_valid_mac_authorizes_nothing(ws, actor):
    write_event_at(ws, actor, ids.now_utc(), "authorization.granted", _granted(), signed=False)
    assert _allows(ws).allowed is False and state.load(ws)["authorizations"] == {}
    write_event_at(ws, actor, ids.now_utc(), "authorization.granted", _granted("AUTH-002", authority_mac=BAD_MAC), signed=False)
    assert _allows(ws).allowed is False and state.load(ws)["authorizations"] == {}


def test_grant_recorded_here_authorizes(ws, actor):
    event = _grant(ws, actor)
    assert event["data"]["authority_mac"].startswith("hmac-sha256:")
    assert _allows(ws).allowed is True


def test_copied_ledger_with_another_key_authorizes_nothing(ws, actor, tmp_path):
    _grant(ws, actor)
    assert _allows(ws).allowed is True
    copy = _other_machine(ws, tmp_path)
    assert state.load(copy)["authorizations"] == {} and _allows(copy).allowed is False
    assert _allows(ws).allowed is True


def test_acceptance_from_another_machine_counts_as_drift(trusted, actor, tmp_path, py):
    copy = _other_machine(trusted, tmp_path)
    st = state.load(copy)
    assert st["config"]["accepted_hash"] is None
    assert config.check_drift(copy, st, actor, "test-execution-local") == "config_drift"
    marker = tmp_path / "ran"
    r = execution.execute(copy, actor, execution.RunRequest(command=py.argv("open(%r, 'w')" % str(marker))))
    assert (r["status"], r["reason"]) == ("blocked", "config_drift") and not marker.exists()
    assert any(RECOVERY in text for text in r["limitations"])


def test_re_accepting_restores_the_workspace(trusted, actor, tmp_path):
    copy = _other_machine(trusted, tmp_path)
    assert config.check_drift(copy, state.load(copy), actor, "test-execution-local") == "config_drift"
    config.accept(copy, actor, "yes, this configuration, on this machine")
    assert config.check_drift(copy, state.load(copy), actor, "test-execution-local") is None
    _grant(copy, actor)
    assert _allows(copy).allowed is True


def test_doctor_counts_events_that_carry_no_authority(ws, actor):
    config.record_initialized(ws, actor)
    write_event_at(ws, actor, ids.now_utc(), "authorization.granted", _granted(), signed=False)
    write_event_at(ws, actor, ids.now_utc(), "config.accepted", {"hash": "sha256:" + "a" * 64, "approval": "old"}, signed=False)
    report = doctor.doctor(ws)["authority"]
    assert report["events_without_authority"] == 2
    assert report["key"] == "ok" and RECOVERY in report["recovery"] and "qa authorize" in report["recovery"]


def test_doctor_is_quiet_about_a_workspace_that_owes_nothing(trusted, actor):
    _grant(trusted, actor)
    report = doctor.doctor(trusted)["authority"]
    assert report["events_without_authority"] == 0 and report["recovery"] is None and report["refused"] == []


def test_chain_still_verifies_for_old_events_without_a_mac(ws, actor):
    write_event_at(ws, actor, ids.now_utc(), "authorization.granted", _granted(), signed=False)
    assert ledger.verify_chains(ws, foreign=False) == []
    assert doctor.ledger_problems(ws) == []
    assert state.load(ws)["violations"] == []


@pytest.mark.parametrize("field", ["approval", "approval_quote"], ids=["config_accepted", "grant"])
def test_the_mac_verifies_what_the_ledger_stored_when_the_redactor_changes_the_quote(ws, actor, field):
    secret = "password=SuperSecret12345"
    if field == "approval":
        event = config.accept(ws, actor, secret)
    else:
        event = authz.grant(ws, actor, environment_id="local", targets=[], activities=["db-readonly"], limits={}, approval=secret,
                            expires_at=ids.now_utc() + timedelta(days=1))
    stored = next(e for e in ledger.iter_all_events(ws) if e["id"] == event["id"])
    assert secret not in stored["data"][field] and stored["redactions"] >= 1
    assert authority.verify(ws, stored["type"], stored["data"]) is True


def test_redacted_approvals_still_count(ws, actor):
    config.accept(ws, actor, "password=SuperSecret12345")
    assert config.check_drift(ws, state.load(ws), actor, "test-execution-local") is None


def test_the_mac_is_not_computed_over_itself(ws):
    data = {"hash": "sha256:" + "a" * 64, "approval": "yes"}
    mac = authority.sign(ws, "config.accepted", data)
    assert authority.sign(ws, "config.accepted", dict(data, authority_mac=BAD_MAC)) == mac
    assert authority.verify(ws, "config.accepted", dict(data, authority_mac=mac)) is True


def test_the_mac_binds_the_event_type_and_every_field(ws):
    data = {"hash": "sha256:" + "a" * 64, "approval": "yes"}
    signed = dict(data, authority_mac=authority.sign(ws, "config.accepted", data))
    assert authority.verify(ws, "config.changed", signed) is False
    assert authority.verify(ws, "config.accepted", dict(signed, approval="no")) is False
    assert authority.verify(ws, "config.accepted", data) is False


@pytest.mark.parametrize("mac", [BAD_MAC, "x", "", "ñandú", "hmac-sha256:\udc80", None, 7, ["a"]],
                         ids=["wrong", "short", "empty", "non_ascii", "surrogate", "null", "number", "list"])
def test_verify_refuses_a_malformed_mac_without_raising(ws, mac):
    assert authority.verify(ws, "config.accepted", {"hash": "sha256:" + "a" * 64, "approval": "yes", "authority_mac": mac}) is False


def _break_key(path, how, scratch):
    if how == "missing":
        path.unlink()
    elif how == "short":
        path.write_bytes(b"k" * 31)
    elif how == "long":
        path.write_bytes(b"k" * 33)
    elif how == "directory":
        path.unlink()
        path.mkdir()
    elif how == "symlink":
        real = scratch / "real.key"
        real.write_bytes(b"k" * 32)
        path.unlink()
        path.symlink_to(real)


@pytest.mark.parametrize("how", ["missing", "short", "long", "directory", "symlink"])
def test_a_broken_key_means_no_authority(trusted, actor, tmp_path, how):
    good = next(e for e in ledger.iter_all_events(trusted) if e["type"] == "config.changed")
    assert authority.verify(trusted, good["type"], good["data"]) is True
    _break_key(trusted.path("authority.key"), how, tmp_path)
    assert authority.verify(trusted, good["type"], good["data"]) is False
    assert state.load(trusted)["config"]["accepted_hash"] is None
    assert config.check_drift(trusted, state.load(trusted), actor, "test-execution-local") == "config_drift"
    assert not list(tmp_path.glob("**/.authority.key.*.tmp"))


@pytest.mark.parametrize("how", ["short", "long", "directory", "symlink"])
def test_an_unusable_key_is_reported_and_refuses_to_sign(ws, actor, tmp_path, how):
    _break_key(ws.path("authority.key"), how, tmp_path)
    assert any(text.startswith("authority_key_invalid:") for text in integrity.authority(ws)["refused"])
    assert doctor.doctor(ws)["authority"]["key"] == "invalid"
    with pytest.raises(QaError) as refused:
        config.accept(ws, actor, "yes")
    assert refused.value.code == "authority_key_invalid"
    assert list(ledger.iter_all_events(ws)) == []


def test_a_missing_key_is_reported_and_is_not_a_refusal(ws):
    ws.path("authority.key").unlink()
    assert integrity.authority(ws)["refused"] == []
    assert doctor.doctor(ws)["authority"]["key"] == "missing"
    assert not ws.path("authority.key").exists()


def test_init_creates_a_private_key_and_never_replaces_it(ws):
    key = ws.path("authority.key")
    before = key.read_bytes()
    assert len(before) == 32 and key.stat().st_mode & 0o777 == 0o600
    workspace.init_workspace(ws.root)
    assert key.read_bytes() == before


def test_an_older_workspace_gets_its_key_on_first_need(ws, actor):
    key = ws.path("authority.key")
    key.unlink()
    config.accept(ws, actor, "yes")
    made = key.read_bytes()
    assert len(made) == 32 and key.stat().st_mode & 0o777 == 0o600
    assert config.check_drift(ws, state.load(ws), actor, "test-execution-local") is None
    config.accept(ws, actor, "again")
    assert key.read_bytes() == made


def test_a_revocation_counts_even_without_a_mac(ws, actor):
    _grant(ws, actor)
    write_event_at(ws, actor, ids.now_utc() + timedelta(seconds=1), "authorization.revoked", {"id": "AUTH-001", "reason": "no"}, signed=False)
    assert _allows(ws).allowed is False


def test_revoking_a_grant_that_carries_no_authority_is_no_violation(ws, actor):
    now = ids.now_utc()
    write_event_at(ws, actor, now, "authorization.granted", _granted(), signed=False)
    write_event_at(ws, actor, now + timedelta(seconds=1), "authorization.revoked", {"id": "AUTH-001", "reason": "no"}, signed=False)
    st = state.load(ws)
    assert st["authorizations"] == {} and st["violations"] == []


def test_the_refusals_name_the_way_back(ws, actor):
    config.record_initialized(ws, actor)
    assert "qa authorize" in _allows(ws).detail
    ws.path("config.json").write_bytes(ws.path("config.json").read_bytes() + b"\n")
    with pytest.raises(QaError) as refused:
        config.set_value(ws, actor, "project.trust", "trusted", "yes")
    assert refused.value.code == "config_drift" and RECOVERY in refused.value.message


def test_an_engine_recorded_on_another_machine_carries_no_authority(ws, actor, tmp_path):
    config.record_initialized(ws, actor)
    config.set_value(ws, actor, "container.engine", ENGINE, "yes")
    assert config.container_engine(ws) == ENGINE
    copy = _other_machine(ws, tmp_path)
    assert integrity.authority(copy)["refused"] == []
    with pytest.raises(QaError) as refused:
        config.container_engine(copy)
    assert refused.value.code == "container_engine_unapproved"


def test_the_key_is_git_ignored(ws):
    if shutil.which("git") is None:
        pytest.skip("tool missing: git")
    _git(ws.root, "init", "-q")
    done = subprocess.run(["git", "-C", str(ws.root), "check-ignore", "-q", ".qa/authority.key"], capture_output=True)
    assert done.returncode == 0


def test_the_key_never_enters_a_bundle(ws, actor, tmp_path):
    config.record_initialized(ws, actor)
    _grant(ws, actor)
    out = tmp_path / "out.tar.gz"
    bundle.create(ws, actor, out)
    key = ws.path("authority.key").read_bytes()
    with tarfile.open(str(out), "r:gz") as tar:
        names = tar.getnames()
        contents = b"".join(tar.extractfile(member).read() for member in tar.getmembers() if member.isfile())
    assert not any("authority.key" in name for name in names) and key not in contents


def test_the_key_never_enters_a_container_archive(ws, tmp_path):
    (ws.root / "app.py").write_text("x = 1\n")
    out = tmp_path / "project.tar"
    container.project_archive(ws.root, out, 1 << 20)
    with tarfile.open(str(out)) as tar:
        assert tar.getnames() == ["app.py"]


def test_a_bundle_with_old_events_still_imports_as_data(ws, actor, tmp_path):
    source = workspace.init_workspace(tmp_path / "ci")[0]
    config.record_initialized(source, actor)
    write_event_at(source, actor, ids.now_utc(), "authorization.granted", _granted(), signed=False)
    bundle.create(source, actor, tmp_path / "out.tar.gz")
    done = bundle.ingest(ws, actor, tmp_path / "out.tar.gz")
    assert done["ignored"]["authorizations"] == 1
    assert state.load(ws)["authorizations"] == {}
