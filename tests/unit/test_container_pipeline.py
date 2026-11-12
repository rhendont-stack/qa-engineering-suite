"""The container pipeline, against a fake engine: what each step asks of the daemon, and what comes back."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest

from qa import container
from qa.errors import QaError

EXECUTION = "EXE-01ARZ3NDEKTSV4RRFFQ69G5FAV"
NAME = "qa-" + EXECUTION.lower()
WORK, EVIDENCE = NAME + "-work", NAME + "-evidence"
IMAGE = "python@sha256:" + "a" * 64
VERSION_JSON = json.dumps({"Server": {"Version": "27.5.1", "Os": "linux", "Arch": "amd64"}}).encode()
NEEDS_GIT = pytest.mark.skipif(shutil.which("git") is None, reason="tool missing: git")
CONFINED = ("--cpus=2", "--memory=2g", "--pids-limit=512", "--init", "--cap-drop=ALL", "--security-opt=no-new-privileges")


def inspected(network="none", image="sha256:" + "b" * 64, status="exited", code=0):
    return json.dumps([{"Image": image, "HostConfig": {"NetworkMode": network}, "State": {"Status": status, "ExitCode": code}}]).encode()


def tar_of(members):
    """A tar of ``(name, type, payload)`` as a daemon could send it; hostile members exist only here, at run time."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for name, kind, payload in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind == tarfile.REGTYPE:
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            else:
                if kind == tarfile.SYMTYPE:
                    info.linkname = payload
                tar.addfile(info)
    return raw.getvalue()


def is_version(args):
    return args[0] == "version"


def is_copy_out(args):
    return args[0] == "run" and "-c" in args


def is_copy_in(args):
    return args[0] == "run" and "-x" in args


def is_install(args):
    return args[0] == "run" and "--network=bridge" in args


def is_create(args):
    return args[0] == "create"


def is_start(args):
    return args[0] == "start"


def is_inspect(args):
    return args[0] == "inspect"


def is_cleanup(args):
    return args[0] == "rm" or args[:2] == ["volume", "rm"]


@pytest.fixture(autouse=True)
def daemon(fake_engine):
    fake_engine.when(is_version, stdout=VERSION_JSON)
    fake_engine.when(is_inspect, stdout=inspected())
    fake_engine.when(is_copy_out, stdout=tar_of([(".", tarfile.DIRTYPE, None), ("./junit.xml", tarfile.REGTYPE, b"<testsuite/>")]))
    return fake_engine


def make_spec(tmp_path, **over):
    archive = tmp_path / "project.tar"
    if not archive.exists():
        archive.write_bytes(tar_of([("a.txt", tarfile.REGTYPE, b"a")]))
    fields = dict(execution_id=EXECUTION, image=IMAGE, script="pytest -q", install_script=None, archive=archive,
                  workdir="/work", timeout_s=60.0, max_bytes=1 << 20)
    fields.update(over)
    return container.ContainerSpec(**fields)


@pytest.fixture
def spec(tmp_path):
    return make_spec(tmp_path)


@pytest.fixture
def spec_install(tmp_path):
    return make_spec(tmp_path, install_script="pip install -r requirements.txt")


@pytest.fixture
def spec_slow(fake_engine, tmp_path):
    fake_engine.when(is_start, exit_code=None, timed_out=True)
    return make_spec(tmp_path, timeout_s=1.0)


def started(fake_engine):
    return [call for call in fake_engine.calls if call[0] in ("run", "create")]


def spawn_of(fake_engine, picks):
    return next(spawn for call, spawn in zip(fake_engine.calls, fake_engine.spawns) if picks(call))


# --------------------------------------------------------------------------- the sequence

def test_sequence_and_network_modes(fake_engine, spec):
    container.run(fake_engine.engine, spec)
    assert fake_engine.networks() == ["none", "none", "none", "none"]  # copy in, prepare, test, copy out; no install
    assert [call[:2] for call in fake_engine.calls[:9]] == [
        ["version", "--format"], ["volume", "create"], ["volume", "create"], ["run", "--rm"], ["run", "--rm"], ["create", "-i"],
        ["start", "-a"], ["inspect", NAME], ["run", "--rm"]]
    assert fake_engine.calls[6] == ["start", "-a", "-i", NAME]


def test_outcome_carries_what_was_observed(fake_engine, spec):
    done = container.run(fake_engine.engine, spec)
    assert done.run.exit_code == 0 and done.evidence == {"junit.xml": b"<testsuite/>"} and done.evidence_error is None
    assert (done.install_ran, done.install_failed, done.network_mode) == (False, False, "none")
    assert done.image_id == "sha256:" + "b" * 64 and done.facts.version == "27.5.1"
    assert (done.anomalies, done.cleanup_failed) == ((), ())


def test_copy_in_sends_the_archive_on_stdin_and_owns_nothing_from_the_host(fake_engine, spec):
    container.run(fake_engine.engine, spec)
    spawn = spawn_of(fake_engine, is_copy_in)
    assert spawn["stdin_path"] == spec.archive
    call = next(call for call in fake_engine.calls if is_copy_in(call))
    assert call[-5:] == [IMAGE, "-x", "--no-same-owner", "-C", "/work"] and "--entrypoint=tar" in call and "-i" in call


def is_prepare(args):
    return args[0] == "run" and "--entrypoint=chmod" in args


def test_file_steps_run_as_root_and_the_tests_as_the_image_user(fake_engine, spec_install):
    """An image may name another user for its tool (k6, ZAP, Schemathesis, squawk do): a new volume belongs to root, so
    only a root step can unpack into it or read every file back. The tests and the install keep the image's own user."""
    container.run(fake_engine.engine, spec_install)
    for picks in (is_copy_in, is_prepare, is_copy_out):
        call = next(call for call in fake_engine.calls if picks(call))
        assert "--user=0" in call, call
    for picks in (is_install, is_create):
        call = next(call for call in fake_engine.calls if picks(call))
        assert not any(arg.startswith("--user=") for arg in call), call


def test_volumes_open_to_the_image_user_after_copy_in_and_before_anything_runs(fake_engine, spec_install):
    container.run(fake_engine.engine, spec_install)
    call = next(call for call in fake_engine.calls if is_prepare(call))
    assert call[-4:] == [IMAGE, "0777", "/work", "/qa-evidence"] and "--network=none" in call and "--rm" in call
    assert "--volume=%s:/work" % WORK in call and "--volume=%s:/qa-evidence" % EVIDENCE in call
    assert "--name=" + NAME + "-prep" in call and spawn_of(fake_engine, is_prepare)["stdin_path"] is None
    first = {name: next(i for i, call in enumerate(fake_engine.calls) if picks(call))
             for name, picks in (("copy_in", is_copy_in), ("prepare", is_prepare), ("install", is_install), ("create", is_create))}
    assert first["copy_in"] < first["prepare"] < first["install"] < first["create"]


def test_a_failed_prepare_step_stops_the_run_and_cleans_up(fake_engine, spec):
    fake_engine.when(is_prepare, exit_code=1)
    with pytest.raises(QaError) as failed:
        container.run(fake_engine.engine, spec)
    assert failed.value.code == "container_step_failed" and failed.value.details["step"] == "prepare"
    assert not any(is_create(call) for call in fake_engine.calls)
    assert [call[-1] for call in fake_engine.calls if is_cleanup(call)] == [NAME + "-in", NAME + "-prep", WORK, EVIDENCE]


def test_copy_out_reads_only_the_evidence_volume(fake_engine, spec):
    container.run(fake_engine.engine, spec)
    call = next(call for call in fake_engine.calls if is_copy_out(call))
    assert call[-5:] == [IMAGE, "-c", "-C", "/qa-evidence", "."] and "--entrypoint=tar" in call
    assert "--volume=%s:/qa-evidence:ro" % EVIDENCE in call and not any(arg.startswith("--volume=" + WORK) for arg in call)


def test_install_is_the_only_step_with_a_network(fake_engine, spec_install):
    done = container.run(fake_engine.engine, spec_install)
    assert fake_engine.networks() == ["none", "none", "bridge", "none", "none"]
    assert (done.install_ran, done.install_failed) == (True, False)
    assert not any("--network=bridge" in call and "--volume=%s:/qa-evidence" % EVIDENCE in call for call in fake_engine.calls)


def test_failed_install_runs_no_tests(fake_engine, spec_install):
    fake_engine.when(is_install, exit_code=1)
    done = container.run(fake_engine.engine, spec_install)
    assert (done.install_ran, done.install_failed, done.run.exit_code) == (True, True, 1)
    assert not any(is_create(call) or is_start(call) or is_copy_out(call) for call in fake_engine.calls)
    assert any(is_cleanup(call) for call in fake_engine.calls)


def test_timed_out_install_counts_as_failed(fake_engine, spec_install):
    fake_engine.when(is_install, exit_code=None, timed_out=True)
    assert container.run(fake_engine.engine, spec_install).install_failed is True


def test_every_container_is_named_labelled_pinned_and_has_an_explicit_entrypoint(fake_engine, spec_install):
    container.run(fake_engine.engine, spec_install)
    calls = started(fake_engine)
    assert len(calls) == 5
    for call in calls:
        assert "--pull=never" in call and "--label=qa.execution=" + EXECUTION in call
        assert [arg for arg in call if arg.startswith("--entrypoint=")] in (["--entrypoint=tar"], ["--entrypoint=sh"], ["--entrypoint=chmod"])
        assert any(arg.startswith("--name=" + NAME) for arg in call)
    volumes = [call for call in fake_engine.calls if call[:2] == ["volume", "create"]]
    assert volumes == [["volume", "create", "--label=qa.execution=" + EXECUTION, WORK],
                       ["volume", "create", "--label=qa.execution=" + EXECUTION, EVIDENCE]]


def test_test_container_is_confined(fake_engine, spec):
    container.run(fake_engine.engine, spec)
    call = next(call for call in fake_engine.calls if is_create(call))
    assert set(CONFINED) <= set(call) and "--network=none" in call and "--entrypoint=sh" in call
    assert {"--volume=%s:/work" % WORK, "--volume=%s:/qa-evidence" % EVIDENCE} <= set(call)
    assert "--workdir=/work" in call and call[-2:] == [IMAGE, "-s"]


def test_install_is_confined_like_the_tests_but_for_the_network(fake_engine, spec_install):
    container.run(fake_engine.engine, spec_install)
    call = next(call for call in fake_engine.calls if is_install(call))
    assert set(CONFINED) <= set(call) and call[-2:] == [IMAGE, "-s"]


@pytest.mark.parametrize("picks", [is_start, is_install], ids=["tests", "install"])
def test_scripts_cannot_read_the_rest_of_themselves(fake_engine, spec_install, picks):
    container.run(fake_engine.engine, spec_install)
    body = spawn_of(fake_engine, picks)["stdin_bytes"]
    command = b"pytest -q" if picks is is_start else b"pip install -r requirements.txt"
    assert body == b"(\n" + command + b"\n) </dev/null\n"


def test_project_paths_with_spaces_travel_in_the_archive(fake_engine, spec, tmp_path):
    root = tmp_path / "my project"
    (root / "a dir").mkdir(parents=True)
    (root / "a dir" / "b c.txt").write_text("x")
    archive = tmp_path / "out.tar"
    container.project_archive(root, archive, 1 << 20)
    container.run(fake_engine.engine, make_spec(tmp_path, archive=archive, script="cat 'a dir/b c.txt'"))
    assert not any(" " in arg or "my project" in arg for call in fake_engine.calls for arg in call)
    assert "a dir/b c.txt" in names_in(archive)


def test_no_secrets_or_env_are_passed(fake_engine, spec):
    container.run(fake_engine.engine, spec)
    assert not any(arg in ("-e", "--env", "--env-file") or arg.startswith(("--env=", "--env-file=")) for call in fake_engine.calls for arg in call)


def test_never_bind_mounts_a_host_path(fake_engine, spec_install):
    container.run(fake_engine.engine, spec_install)
    sources = [arg.split("=", 1)[1].split(":")[0] for call in fake_engine.calls for arg in call if arg.startswith("--volume=")]
    assert sources and set(sources) == {WORK, EVIDENCE}
    assert not any(arg in ("-v", "--mount") or arg.startswith("--mount=") for call in fake_engine.calls for arg in call)


def test_only_own_objects_are_touched(fake_engine, spec):
    container.run(fake_engine.engine, spec)
    assert not any(w in ("prune", "system", "ps", "ls") for call in fake_engine.calls for w in call)
    targets = [call[-1] for call in fake_engine.calls if is_cleanup(call)]
    assert NAME in targets and WORK in targets and EVIDENCE in targets
    assert all(target.startswith(NAME) for target in targets)


# --------------------------------------------------------------------------- timeouts and failures

def test_timeout_kills_and_removes_only_its_own_objects(fake_engine, spec_slow):
    done = container.run(fake_engine.engine, spec_slow)
    assert done.run.timed_out is True
    assert ["kill", NAME] in fake_engine.calls and ["rm", "-f", NAME] in fake_engine.calls
    assert fake_engine.calls.index(["kill", NAME]) < next(i for i, call in enumerate(fake_engine.calls) if is_copy_out(call))
    assert ["volume", "rm", "-f", WORK] in fake_engine.calls and ["volume", "rm", "-f", EVIDENCE] in fake_engine.calls
    assert all(call[-1].startswith(NAME) for call in fake_engine.calls if call[0] in ("kill", "rm"))


def test_the_test_step_gets_the_callers_limits(fake_engine, tmp_path):
    container.run(fake_engine.engine, make_spec(tmp_path, timeout_s=33.0, max_bytes=4242))
    spawn = spawn_of(fake_engine, is_start)
    assert (spawn["timeout_s"], spawn["max_bytes"]) == (33.0, 4242)


def test_cleanup_runs_when_a_step_fails(fake_engine, spec):
    fake_engine.when(is_copy_in, exit_code=1)
    with pytest.raises(QaError) as failed:
        container.run(fake_engine.engine, spec)
    assert failed.value.code == "container_step_failed" and failed.value.details["step"] == "copy_in"
    assert [call[-1] for call in fake_engine.calls if is_cleanup(call)] == [NAME + "-in", WORK, EVIDENCE]


def test_absent_image_is_its_own_failure(fake_engine, spec):
    fake_engine.when(is_copy_in, exit_code=125, stderr=b"docker: Error response from daemon: No such image: x\n")
    with pytest.raises(QaError) as failed:
        container.run(fake_engine.engine, spec)
    assert failed.value.code == "container_image_missing"


def test_a_container_that_never_started_is_no_test_result(fake_engine, spec):
    fake_engine.when(is_start, exit_code=1)
    fake_engine.when(is_inspect, stdout=inspected(status="created"))
    with pytest.raises(QaError) as failed:
        container.run(fake_engine.engine, spec)
    assert failed.value.code == "container_step_failed" and failed.value.details["step"] == "start"


def test_engine_down_creates_nothing_and_never_falls_back(fake_engine, spec):
    fake_engine.when(is_version, exit_code=255)
    with pytest.raises(container.EngineUnavailable):
        container.run(fake_engine.engine, spec)
    assert len(fake_engine.calls) == 1


def lose_the_connection(fake_engine):
    """The next probe answers ``exit 255``, as ``ssh`` does."""
    fake_engine.when(is_version, exit_code=255)
    fake_engine.when(is_version, times=1, stdout=VERSION_JSON)  # the first probe, before the run, still works


def test_lost_connection_during_tests_is_not_a_test_failure(fake_engine, spec):
    fake_engine.when(is_start, exit_code=255)
    lose_the_connection(fake_engine)
    with pytest.raises(container.EngineUnavailable) as lost:
        container.run(fake_engine.engine, spec)
    assert lost.value.details["reason"] == "connection_lost" and lost.value.details["step"] == "test"
    assert any(is_cleanup(call) for call in fake_engine.calls)


def test_lost_connection_during_copy_in_is_not_a_step_failure(fake_engine, spec):
    fake_engine.when(is_copy_in, exit_code=255)
    lose_the_connection(fake_engine)
    with pytest.raises(container.EngineUnavailable) as lost:
        container.run(fake_engine.engine, spec)
    assert lost.value.details["reason"] == "connection_lost" and lost.value.details["step"] == "copy_in"


def test_lost_connection_during_copy_out_keeps_the_test_result(fake_engine, spec):
    fake_engine.when(is_copy_out, exit_code=255)
    lose_the_connection(fake_engine)
    done = container.run(fake_engine.engine, spec)
    assert (done.run.exit_code, done.evidence, done.evidence_error) == (0, {}, "connection_lost")


def test_the_exit_code_is_the_one_the_daemon_recorded(fake_engine, spec):
    # A shell between ssh and the engine can fold every failure into 1 (Windows PowerShell does); the daemon cannot.
    fake_engine.when(is_start, exit_code=1)
    fake_engine.when(is_inspect, stdout=inspected(code=7))
    assert container.run(fake_engine.engine, spec).run.exit_code == 7


def test_a_timed_out_run_keeps_the_timeout_not_the_kill(fake_engine, spec_slow):
    fake_engine.when(is_inspect, stdout=inspected(code=137))
    done = container.run(fake_engine.engine, spec_slow).run
    assert (done.timed_out, done.exit_code) == (True, None)


@pytest.mark.parametrize("answer", [dict(stdout=inspected(status="running", code=0)), dict(stdout=b""), dict(exit_code=1), dict(stdout=b"[]")],
                         ids=["daemon_says_running", "empty_inspect_output", "inspect_failed", "inspect_lists_nothing"])
@pytest.mark.parametrize("client", [dict(exit_code=1), dict(exit_code=None, signal=9), dict(exit_code=0)],
                         ids=["client_exit_1", "client_killed_by_signal", "client_exit_0"])
def test_only_an_exited_container_gives_an_exit_code(fake_engine, spec, client, answer):
    fake_engine.when(is_start, **client)
    fake_engine.when(is_inspect, **answer)
    with pytest.raises(container.EngineUnavailable) as lost:
        container.run(fake_engine.engine, spec)
    assert lost.value.details["reason"] == "connection_lost" and lost.value.details["step"] == "test"
    assert not any(is_copy_out(call) for call in fake_engine.calls)  # never against a test that may still be running
    assert any(is_cleanup(call) for call in fake_engine.calls)


def test_a_test_that_exits_255_is_a_test_result(fake_engine, spec):
    fake_engine.when(is_start, exit_code=255)
    fake_engine.when(is_inspect, stdout=inspected(code=255))
    assert container.run(fake_engine.engine, spec).run.exit_code == 255


@pytest.mark.parametrize("answer", [dict(stdout=inspected(code=0)), dict(stdout=inspected(status="running", code=0)), dict(exit_code=1)],
                         ids=["container_exited_differently", "container_still_running", "inspect_failed"])
def test_a_255_the_container_did_not_produce_is_a_lost_connection(fake_engine, spec, answer):
    fake_engine.when(is_start, exit_code=255)
    fake_engine.when(is_inspect, **answer)
    with pytest.raises(container.EngineUnavailable) as lost:
        container.run(fake_engine.engine, spec)
    assert lost.value.details["reason"] == "connection_lost"


# --------------------------------------------------------------------------- cleanup reporting

def test_cleanup_failure_is_reported_with_the_result(fake_engine, spec):
    fake_engine.when(lambda args: args == ["volume", "rm", "-f", WORK], exit_code=1, stderr=b"volume is in use")
    done = container.run(fake_engine.engine, spec)
    assert done.cleanup_failed == (WORK,) and done.run.exit_code == 0
    assert ["volume", "rm", "-f", EVIDENCE] in fake_engine.calls  # one failure does not stop the rest


def test_cleanup_failure_travels_with_the_step_failure(fake_engine, spec):
    fake_engine.when(is_copy_in, exit_code=1)
    fake_engine.when(lambda args: args == ["rm", "-f", NAME + "-in"], exit_code=1, stderr=b"daemon busy")
    with pytest.raises(QaError) as failed:
        container.run(fake_engine.engine, spec)
    assert failed.value.code == "container_step_failed" and failed.value.details["cleanup_failed"] == [NAME + "-in"]


def test_cleanup_failure_is_not_lost_behind_another_exception(fake_engine, spec, monkeypatch):
    def broken(*_args, **_kw):
        raise ValueError("a bug")

    monkeypatch.setattr(container, "_read_evidence", broken)
    fake_engine.when(lambda args: args == ["rm", "-f", NAME], exit_code=1, stderr=b"daemon busy")
    with pytest.raises(QaError) as failed:
        container.run(fake_engine.engine, spec)
    assert failed.value.code == "container_cleanup_failed" and failed.value.details["cleanup_failed"] == [NAME]
    assert isinstance(failed.value.__cause__, ValueError)


def test_cleanup_failure_when_the_engine_cannot_be_spawned(fake_engine, spec):
    fake_engine.when(is_cleanup, spawn_error="not_found")
    done = container.run(fake_engine.engine, spec)
    assert NAME in done.cleanup_failed and WORK in done.cleanup_failed and EVIDENCE in done.cleanup_failed


def test_an_object_already_gone_is_not_a_cleanup_failure(fake_engine, spec):
    fake_engine.when(is_cleanup, exit_code=1, stderr=b"Error: No such container: x\n")
    assert container.run(fake_engine.engine, spec).cleanup_failed == ()


@pytest.mark.parametrize("answer", [
    dict(exit_code=1, stderr=b"cannot connect to the daemon socket: no such file or directory\n"),
    dict(exit_code=1, stderr=b"lookup of the engine host failed: no such host\n"),
    dict(exit_code=255, stderr=b"Error: No such container: x\n"),
    dict(exit_code=1, timed_out=True, stderr=b"Error: No such volume: x\n"),
    dict(exit_code=None, signal=9, stderr=b"Error: No such container: x\n"),
], ids=["socket_missing", "host_not_resolved", "transport_exit_with_the_phrase", "timeout_with_the_phrase", "killed_with_the_phrase"])
def test_a_transport_or_daemon_error_is_not_already_gone(fake_engine, spec, answer):
    fake_engine.when(is_cleanup, **answer)
    done = container.run(fake_engine.engine, spec)
    assert NAME in done.cleanup_failed and WORK in done.cleanup_failed and EVIDENCE in done.cleanup_failed


def test_a_volume_already_gone_is_not_a_cleanup_failure(fake_engine, spec):
    fake_engine.when(is_cleanup, exit_code=1, stderr=b"Error response from daemon: get x: no such volume\n")
    assert container.run(fake_engine.engine, spec).cleanup_failed == ()


# --------------------------------------------------------------------------- isolation check

EXITED = {"Status": "exited", "ExitCode": 0}


@pytest.mark.parametrize("report, mode", [(inspected(network="bridge"), "bridge"), (inspected(network="host"), "host"),
                                          (json.dumps([{"State": EXITED}]).encode(), "unknown"),
                                          (json.dumps([{"State": EXITED, "HostConfig": None}]).encode(), "unknown")],
                         ids=["bridge", "host", "no_host_config", "null_host_config"])
def test_network_that_is_not_none_is_an_anomaly(fake_engine, spec, report, mode):
    fake_engine.when(is_inspect, stdout=report)
    done = container.run(fake_engine.engine, spec)
    assert done.anomalies == ("network_not_isolated",) and done.network_mode == mode


def test_a_timed_out_run_whose_network_cannot_be_read_is_an_anomaly(fake_engine, spec_slow):
    fake_engine.when(is_inspect, exit_code=1)
    done = container.run(fake_engine.engine, spec_slow)
    assert done.anomalies == ("network_not_isolated",) and done.network_mode == "unknown"


# --------------------------------------------------------------------------- evidence

@pytest.mark.parametrize("members", [
    [("./ok.txt", tarfile.REGTYPE, b"ok"), ("./link", tarfile.SYMTYPE, "/etc/passwd")],
    [("./ok.txt", tarfile.REGTYPE, b"ok"), ("./pipe", tarfile.FIFOTYPE, None)],
    [("./ok.txt", tarfile.REGTYPE, b"ok"), ("./" + "../" * 3 + "escape", tarfile.REGTYPE, b"x")],
    [("/" + "etc/escape", tarfile.REGTYPE, b"x")],
], ids=["symlink", "fifo", "parent_traversal", "absolute_name"])
def test_copy_out_of_a_hostile_archive_is_report_missing(fake_engine, spec, members):
    fake_engine.when(is_copy_out, stdout=tar_of(members))
    done = container.run(fake_engine.engine, spec)
    assert done.evidence == {} and done.evidence_error == "archive_unsafe" and done.run.exit_code == 0


def test_evidence_in_subdirectories_comes_back(fake_engine, spec):
    fake_engine.when(is_copy_out, stdout=tar_of([(".", tarfile.DIRTYPE, None), ("./sub", tarfile.DIRTYPE, None),
                                                 ("./sub/a.xml", tarfile.REGTYPE, b"a"), ("./b.txt", tarfile.REGTYPE, b"b")]))
    assert container.run(fake_engine.engine, spec).evidence == {"sub/a.xml": b"a", "b.txt": b"b"}


def test_an_empty_evidence_volume_is_no_error(fake_engine, spec):
    fake_engine.when(is_copy_out, stdout=tar_of([(".", tarfile.DIRTYPE, None)]))
    done = container.run(fake_engine.engine, spec)
    assert (done.evidence, done.evidence_error) == ({}, None)


@pytest.mark.parametrize("answer, error", [(dict(exit_code=2), "copy_out_failed"), (dict(exit_code=None, timed_out=True), "copy_out_failed"),
                                           (dict(stdout_dropped=9), "evidence_too_large"), (dict(orphaned_output=True), "copy_out_failed"), (dict(stdout=b"junk" * 300), "archive_unsafe")],
                         ids=["tar_failed", "timed_out", "truncated", "pipe_left_open", "not_a_tar"])
def test_evidence_that_cannot_be_trusted_is_dropped(fake_engine, spec, answer, error):
    fake_engine.when(is_copy_out, **answer)
    done = container.run(fake_engine.engine, spec)
    assert (done.evidence, done.evidence_error) == ({}, error)


# --------------------------------------------------------------------------- what the spec may hold

@pytest.mark.parametrize("field, value", [
    ("image", "python:3.9-slim"), ("image", "--privileged@sha256:" + "a" * 64), ("image", "python@sha256:abc"),
    ("execution_id", "../other"), ("execution_id", "-x"), ("execution_id", "EXE 1"), ("execution_id", ""),
    ("workdir", "/etc"), ("workdir", "/work/../etc"), ("workdir", "/work/a b"), ("workdir", "work"), ("workdir", "/workspace"),
], ids=["tag_only", "flag_shaped_image", "short_digest", "traversal_id", "flag_shaped_id", "id_with_space", "empty_id",
        "workdir_outside", "workdir_escapes", "workdir_with_space", "workdir_relative", "workdir_prefix_only"])
def test_a_spec_that_could_change_a_command_starts_nothing(fake_engine, tmp_path, field, value):
    with pytest.raises(QaError) as refused:
        container.run(fake_engine.engine, make_spec(tmp_path, **{field: value}))
    assert refused.value.code == "container_spec_invalid" and refused.value.details["field"] == field
    assert fake_engine.calls == []


def test_a_workdir_inside_the_project_reaches_both_scripts(fake_engine, tmp_path):
    container.run(fake_engine.engine, make_spec(tmp_path, workdir="/work/pkg/sub", install_script="true"))
    assert all("--workdir=/work/pkg/sub" in call for call in fake_engine.calls if is_create(call) or is_install(call))


def test_engine_arguments_are_read_once(fake_engine):
    class Changes(list):
        def __iter__(self):
            self.reads = getattr(self, "reads", 0) + 1
            return iter(["ps"] if self.reads == 1 else ["ps", "$(x)"])

    fake_engine.engine.run(Changes(["ps"]), stdin_path=None, timeout_s=5, max_bytes=1000)
    assert fake_engine.calls[-1] == ["ps"]


# --------------------------------------------------------------------------- the project archive

def names_in(archive):
    with tarfile.open(str(archive)) as tar:
        return sorted(tar.getnames())


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], check=True, capture_output=True)


@pytest.fixture
def tmp_project(tmp_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\n")
    (root / ".gitignore").write_text("ignored.txt\n")
    (root / "ignored.txt").write_text("not for the container")
    (root / "untracked.txt").write_text("u")
    (root / ".qa").mkdir()
    (root / ".qa" / "ledger.jsonl").write_text("private")
    git(root, "init", "-q")
    git(root, "add", "src", ".gitignore")
    return root


@NEEDS_GIT
def test_ignored_files_and_qa_dir_stay_out_of_the_archive(tmp_project, tmp_path):
    archive = tmp_path / "out.tar"
    size = container.project_archive(tmp_project, archive, 1 << 20)
    assert size == archive.stat().st_size
    assert names_in(archive) == [".gitignore", "src/app.py", "untracked.txt"]


@NEEDS_GIT
def test_a_tracked_qa_or_nested_git_directory_stays_out(tmp_project, tmp_path):
    (tmp_project / ".QA").mkdir(exist_ok=True)
    (tmp_project / ".qa" / "config.json").write_text("{}")
    git(tmp_project, "add", "-f", ".qa/config.json")
    archive = tmp_path / "out.tar"
    container.project_archive(tmp_project, archive, 1 << 20)
    assert not [name for name in names_in(archive) if name.lower().startswith(".qa")]


@NEEDS_GIT
def test_a_deleted_tracked_file_is_passed_over(tmp_project, tmp_path):
    (tmp_project / "src" / "app.py").unlink()
    archive = tmp_path / "out.tar"
    container.project_archive(tmp_project, archive, 1 << 20)
    assert "src/app.py" not in names_in(archive)


def test_outside_git_a_walk_minus_the_two_directories(tmp_path):
    root = tmp_path / "plain"
    (root / ".qa").mkdir(parents=True)
    (root / ".qa" / "ledger").write_text("x")
    (root / "sub" / ".git").mkdir(parents=True)
    (root / "sub" / ".git" / "HEAD").write_text("x")
    (root / "sub" / "b.txt").write_text("b")
    (root / "a.txt").write_text("a")
    archive = tmp_path / "out.tar"
    container.project_archive(root, archive, 1 << 20)
    assert names_in(archive) == ["a.txt", "sub/b.txt"]


def test_a_git_directory_git_cannot_read_is_refused_not_walked(tmp_path, monkeypatch):
    root = tmp_path / "broken"
    (root / ".git").mkdir(parents=True)
    (root / "secret.env").write_text("ignored in the real repository")
    from qa import gitinfo
    monkeypatch.setattr(gitinfo, "project_files", lambda _root: None)
    with pytest.raises(QaError) as refused:
        container.project_archive(root, tmp_path / "out.tar", 1 << 20)
    assert refused.value.code == "project_unlistable" and not (tmp_path / "out.tar").exists()


def test_symlinks_stay_links_and_their_targets_stay_home(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    secret = tmp_path / "outside-secret"
    secret.write_text("SECRET-OUTSIDE")
    (root / "link").symlink_to(secret)
    (root / "dirlink").symlink_to(tmp_path)
    archive = tmp_path / "out.tar"
    container.project_archive(root, archive, 1 << 20)
    with tarfile.open(str(archive)) as tar:
        members = {member.name: member for member in tar.getmembers()}
    assert set(members) == {"link", "dirlink"} and members["link"].issym() and members["dirlink"].issym()
    assert b"SECRET-OUTSIDE" not in archive.read_bytes()


@NEEDS_GIT
def test_a_tracked_path_behind_a_link_leaves_its_target_at_home(tmp_project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "app.py").write_text("HOST-FILE-OUTSIDE-THE-PROJECT")
    shutil.rmtree(str(tmp_project / "src"))  # the index still lists src/app.py
    (tmp_project / "src").symlink_to(outside)
    archive = tmp_path / "out.tar"
    container.project_archive(tmp_project, archive, 1 << 20)
    assert "src/app.py" not in names_in(archive)
    assert b"HOST-FILE-OUTSIDE-THE-PROJECT" not in archive.read_bytes()
    with tarfile.open(str(archive)) as tar:
        assert tar.getmember("src").issym()  # the link itself is project content, and stays a link


@pytest.mark.parametrize("shape", ["absolute", "climbing", "climbing_inside", "dot_part"])
def test_a_listed_name_that_leaves_the_project_is_dropped(tmp_path, monkeypatch, shape):
    from qa import gitinfo

    root = tmp_path / "project"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("a")
    outside = tmp_path / "outside.txt"
    outside.write_text("HOST-FILE-OUTSIDE-THE-PROJECT")
    name = {"absolute": str(outside), "climbing": "../outside.txt", "climbing_inside": "sub/../../outside.txt", "dot_part": "sub/./../a.txt"}[shape]
    monkeypatch.setattr(gitinfo, "project_files", lambda _root: ["a.txt", name])
    archive = tmp_path / "out.tar"
    container.project_archive(root, archive, 1 << 20)
    assert names_in(archive) == ["a.txt"] and b"HOST-FILE-OUTSIDE-THE-PROJECT" not in archive.read_bytes()


def test_special_files_stay_out(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    os.mkfifo(str(root / "pipe"))
    (root / "a.txt").write_text("a")
    archive = tmp_path / "out.tar"
    container.project_archive(root, archive, 1 << 20)
    assert names_in(archive) == ["a.txt"]


def test_archive_says_nothing_of_the_host_user(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "a.txt").write_text("a")
    archive = tmp_path / "out.tar"
    container.project_archive(root, archive, 1 << 20)
    with tarfile.open(str(archive)) as tar:
        member = tar.getmember("a.txt")
    assert (member.uid, member.gid, member.uname, member.gname) == (0, 0, "", "")


def test_oversize_project_refused(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "big.bin").write_bytes(b"x" * (1 << 16))
    archive = tmp_path / "out.tar"
    with pytest.raises(QaError) as refused:
        container.project_archive(root, archive, 1 << 15)
    assert refused.value.code == "project_too_large" and not archive.exists()


def test_many_small_files_cannot_slip_past_the_cap(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    for number in range(40):
        (root / ("f%02d" % number)).write_bytes(b"x")
    archive = tmp_path / "out.tar"
    with pytest.raises(QaError) as refused:
        container.project_archive(root, archive, 1 << 13)
    assert refused.value.code == "project_too_large" and not archive.exists()


def test_an_unreadable_file_is_refused_by_name(tmp_path, monkeypatch):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "a.txt").write_text("a")

    def denied(*_args, **_kw):
        raise PermissionError("denied")

    monkeypatch.setattr(tarfile.TarFile, "add", denied)
    archive = tmp_path / "out.tar"
    with pytest.raises(QaError) as refused:
        container.project_archive(root, archive, 1 << 20)
    assert refused.value.code == "project_unreadable" and refused.value.details["name"] == "a.txt" and not archive.exists()


# --------------------------------------------------------------------------- a fixture service beside the tests

SVC, CTL = NAME + "-svc", NAME + "-ctl"
SVC_ID = "c" * 64
SERVICE_IMAGE = "python@sha256:" + "d" * 64
SERVICE_IMAGE_ID = "sha256:" + "e" * 64
SERVICE_SCRIPT = "exec python -m http.server --bind 127.0.0.1 8080"
PASSING = b'<testsuite><testcase classname="c" name="t"/></testsuite>'


def service_inspected(network="none", running=True, code=0, ident=SVC_ID):
    return json.dumps([{"Id": ident, "Image": SERVICE_IMAGE_ID, "HostConfig": {"NetworkMode": network},
                        "State": {"Status": "running" if running else "exited", "Running": running, "ExitCode": code}}]).encode()


def is_service(args):
    return args[:2] == ["run", "-d"]


def is_ctl_in(args):
    return args[0] == "run" and "--name=" + NAME + "-ctl-in" in args


def is_service_inspect(args):
    return args == ["inspect", SVC]


def is_logs(args):
    return args[:1] == ["logs"]


def service_daemon(fake_engine, service_mode="none", test_mode="container:" + SVC_ID, test_exit=0, junit=PASSING):
    fake_engine.when(is_start, exit_code=test_exit)
    fake_engine.when(is_inspect, stdout=inspected(network=test_mode, code=test_exit))
    fake_engine.when(is_service_inspect, stdout=service_inspected(network=service_mode))
    fake_engine.when(is_logs, stdout=b"serving\n", stderr=b"GET / 200\n")
    fake_engine.when(is_copy_out, stdout=tar_of([(".", tarfile.DIRTYPE, None), ("./junit.xml", tarfile.REGTYPE, junit)]))
    return fake_engine


def service_spec(tmp_path, user=None, **over):
    return make_spec(tmp_path, service=container.ServiceSpec(image=SERVICE_IMAGE, script=SERVICE_SCRIPT, user=user), **over)


def run_with(fake_engine, service_mode="none", test_mode="container:" + SVC_ID, test_exit=0, junit=PASSING, user=None, **over):
    """One run with a service, against a daemon that reports the two network modes given."""
    service_daemon(fake_engine, service_mode=service_mode, test_mode=test_mode, test_exit=test_exit, junit=junit)
    return run_service(fake_engine, user=user, **over)


def run_service(fake_engine, **over):
    """One run with a service, against whatever the daemon was told to answer."""
    with tempfile.TemporaryDirectory() as scratch:
        return container.run(fake_engine.engine, service_spec(Path(scratch), **over))


def test_service_and_tool_share_only_loopback(fake_engine):
    out = run_with(fake_engine)
    # copy in, the service script in, the service, the tests, copy out
    assert fake_engine.networks() == ["none", "none", "none", "none", "container:" + SVC, "none"]
    service = next(call for call in fake_engine.calls if is_service(call))
    assert "--network=none" in service and "--entrypoint=sh" in service and "-i" not in service
    assert set(CONFINED) <= set(service) and service[-2:] == [SERVICE_IMAGE, "/qa-ctl/service.sh"]
    assert [arg for arg in service if arg.startswith("--volume=")] == ["--volume=%s:/work:ro" % WORK, "--volume=%s:/qa-ctl:ro" % CTL]
    assert "--name=" + SVC in service and "--pull=never" in service and not any(arg.startswith("--user") for arg in service)
    tests = next(call for call in fake_engine.calls if is_create(call))
    assert "--network=container:" + SVC in tests and "--network=none" not in tests
    assert (out.network_mode, out.anomalies) == ("none", ())
    assert out.service == {"image": SERVICE_IMAGE, "image_id": SERVICE_IMAGE_ID, "network_mode": "none", "running_at_end": True,
                           "exit_code": None}


def test_the_service_script_travels_on_stdin_only(fake_engine):
    run_with(fake_engine)
    assert not any("http.server" in arg for call in fake_engine.calls for arg in call)
    call = next(call for call in fake_engine.calls if is_ctl_in(call))
    assert "--volume=%s:/qa-ctl" % CTL in call and "--network=none" in call and "--entrypoint=tar" in call
    assert call[-5:] == [IMAGE, "-x", "--no-same-owner", "-C", "/qa-ctl"]
    with tarfile.open(fileobj=io.BytesIO(spawn_of(fake_engine, is_ctl_in)["stdin_bytes"])) as tar:
        member = tar.getmember("service.sh")
        assert tar.getnames() == ["service.sh"] and member.isreg() and member.mode == 0o644 and (member.uid, member.uname) == (0, "")
        assert tar.extractfile(member).read() == SERVICE_SCRIPT.encode() + b"\n"


def test_the_steps_come_in_order(fake_engine):
    run_with(fake_engine, install_script="pip install x")
    kinds = []
    for call in fake_engine.calls:
        kind = ("ctl_in" if is_ctl_in(call) else "copy_in" if is_copy_in(call) else "prepare" if is_prepare(call) else
                "install" if is_install(call) else
                "service" if is_service(call) else "create" if is_create(call) else "start" if is_start(call) else
                "service_inspect" if is_service_inspect(call) else "logs" if is_logs(call) else "copy_out" if is_copy_out(call) else None)
        if kind is not None:
            kinds.append(kind)
    assert kinds == ["copy_in", "prepare", "ctl_in", "install", "service", "create", "start", "service_inspect", "logs", "copy_out"]
    volumes = [call[-1] for call in fake_engine.calls if call[:2] == ["volume", "create"]]
    assert volumes == [WORK, EVIDENCE, CTL]


def test_service_with_a_network_makes_the_run_not_isolated(fake_engine):
    out = run_with(fake_engine, service_mode="bridge", test_exit=0, junit=PASSING)
    assert out.network_mode != "none" and "network_not_isolated" in out.anomalies   # execution then: inconclusive
    assert out.service["network_mode"] == "bridge"


@pytest.mark.parametrize("test_mode", ["container:" + "f" * 64, "container:" + SVC, "bridge", "host", "none"],
                         ids=["another_container", "a_name_not_an_id", "bridge", "host", "did_not_join"])
def test_joining_another_container_is_not_isolated(fake_engine, test_mode):
    out = run_with(fake_engine, test_mode=test_mode)
    assert (out.network_mode, out.anomalies) == (test_mode, ("network_not_isolated",))


@pytest.mark.parametrize("answer", [dict(exit_code=1), dict(stdout=b"[]"), dict(stdout=service_inspected(ident="")),
                                    dict(stdout=json.dumps([{"State": {}}]).encode())],
                         ids=["inspect_failed", "inspect_lists_nothing", "empty_id", "no_id_no_mode"])
def test_a_service_the_daemon_will_not_describe_is_not_isolated(fake_engine, answer):
    service_daemon(fake_engine)
    fake_engine.when(is_service_inspect, **answer)
    out = run_service(fake_engine)
    assert out.network_mode != "none" and out.anomalies == ("network_not_isolated",)
    assert out.service["image"] == SERVICE_IMAGE


def test_a_service_that_stopped_says_how(fake_engine):
    service_daemon(fake_engine)
    fake_engine.when(is_service_inspect, stdout=service_inspected(running=False, code=3))
    out = run_service(fake_engine)
    assert (out.service["running_at_end"], out.service["exit_code"]) == (False, 3)
    assert (out.network_mode, out.anomalies) == ("none", ())


def test_the_service_log_comes_back_within_the_cap(fake_engine):
    out = run_with(fake_engine, max_bytes=4242)
    assert out.service_log == b"serving\nGET / 200\n"
    spawn = spawn_of(fake_engine, is_logs)
    assert fake_engine.calls[fake_engine.spawns.index(spawn)] == ["logs", SVC] and spawn["max_bytes"] == 4242


def test_a_run_without_a_service_has_none(fake_engine, spec):
    done = container.run(fake_engine.engine, spec)
    assert (done.service, done.service_log) == (None, b"")
    assert not any(is_service(call) or is_ctl_in(call) or is_logs(call) for call in fake_engine.calls)
    assert [call[-1] for call in fake_engine.calls if call[:2] == ["volume", "create"]] == [WORK, EVIDENCE]


def test_the_service_user_is_passed_only_to_the_service(fake_engine):
    run_with(fake_engine, user="1000:1000")
    service = next(call for call in fake_engine.calls if is_service(call))
    assert "--user=1000:1000" in service
    others = [arg for call in fake_engine.calls if not is_service(call) for arg in call if arg.startswith("--user")]
    assert others == ["--user=0"] * 4  # copy in, prepare, control in, copy out: the file steps run as root


@pytest.mark.parametrize("over, field", [
    (dict(image="python:3-slim"), "service.image"), (dict(image="--privileged@sha256:" + "a" * 64), "service.image"),
    (dict(user="root"), "service.user"), (dict(user="1000:"), "service.user"), (dict(user="-1"), "service.user"),
    (dict(user="1:2:3"), "service.user"), (dict(user="12345678901"), "service.user"), (dict(user=""), "service.user"),
    (dict(user="0 --privileged"), "service.user"), (dict(script=None), "service.script"),
], ids=["tag_only", "flag_shaped", "a_name", "empty_group", "negative", "three_parts", "eleven_digits", "empty", "with_a_flag",
        "no_script"])
def test_a_service_spec_that_could_change_a_command_starts_nothing(fake_engine, tmp_path, over, field):
    fields = dict(image=SERVICE_IMAGE, script=SERVICE_SCRIPT, user=None)
    fields.update(over)
    with pytest.raises(QaError) as refused:
        container.run(fake_engine.engine, make_spec(tmp_path, service=container.ServiceSpec(**fields)))
    assert refused.value.code == "container_spec_invalid" and refused.value.details["field"] == field
    assert fake_engine.calls == []


def test_a_service_image_the_engine_lacks_is_named(fake_engine):
    service_daemon(fake_engine)
    fake_engine.when(is_service, exit_code=125, stderr=b"docker: Unable to find image 'x' locally\n")
    with pytest.raises(QaError) as failed:
        run_service(fake_engine)
    assert failed.value.code == "container_image_missing" and failed.value.details["image"] == SERVICE_IMAGE
    assert not any(is_create(call) for call in fake_engine.calls)


def test_a_service_that_cannot_start_is_a_step_failure(fake_engine):
    service_daemon(fake_engine)
    fake_engine.when(is_service, exit_code=1, stderr=b"daemon busy\n")
    with pytest.raises(QaError) as failed:
        run_service(fake_engine)
    assert failed.value.code == "container_step_failed" and failed.value.details["step"] == "service_start"
    targets = [call[-1] for call in fake_engine.calls if is_cleanup(call)]
    assert SVC in targets and CTL in targets


def test_a_service_gone_before_the_tests_start_is_a_step_failure(fake_engine):
    service_daemon(fake_engine)
    fake_engine.when(is_service_inspect, stdout=service_inspected(running=False, code=1))
    fake_engine.when(is_start, exit_code=125, stderr=b"Error response from daemon: cannot join network of a non running container\n")
    fake_engine.when(lambda args: args == ["inspect", NAME], stdout=inspected(status="created"))
    with pytest.raises(QaError) as failed:
        run_service(fake_engine)
    assert failed.value.code == "container_step_failed" and failed.value.details["step"] == "start"
    assert not any(is_copy_out(call) for call in fake_engine.calls)
    targets = removed(fake_engine)
    assert SVC in targets and CTL in targets and NAME in targets and WORK in targets


def removed(fake_engine):
    return [call[-1] for call in fake_engine.calls if is_cleanup(call)]


@pytest.mark.parametrize("over", [dict(), dict(slow=True)], ids=["after_the_tests", "after_a_timeout"])
def test_every_service_object_is_removed_by_exact_name(fake_engine, over):
    service_daemon(fake_engine)
    if over:
        fake_engine.when(is_start, exit_code=None, timed_out=True)
    out = run_service(fake_engine)
    assert out.run.timed_out is bool(over) and out.cleanup_failed == ()
    targets = removed(fake_engine)
    assert sorted(targets) == sorted([NAME + "-in", NAME + "-prep", NAME + "-ctl-in", SVC, NAME, NAME + "-out", WORK, EVIDENCE, CTL])
    assert targets.index(SVC) < targets.index(CTL) and targets.index(SVC) < targets.index(WORK)  # a volume in use stays
    assert ["rm", "-f", SVC] in fake_engine.calls and ["volume", "rm", "-f", CTL] in fake_engine.calls
    assert not any(w in ("prune", "system", "ps", "ls", "stop") for call in fake_engine.calls for w in call)
    assert all(call[-1].startswith(NAME) for call in fake_engine.calls if call[0] in ("kill", "rm", "logs", "inspect"))


def test_a_service_object_that_stays_is_reported(fake_engine):
    service_daemon(fake_engine)
    fake_engine.when(lambda args: args == ["rm", "-f", SVC], exit_code=1, stderr=b"daemon busy")
    out = run_service(fake_engine)
    assert SVC in out.cleanup_failed


def test_a_lost_connection_while_reading_the_service_keeps_the_test_result(fake_engine):
    service_daemon(fake_engine)
    fake_engine.when(is_logs, exit_code=255)
    fake_engine.when(is_service_inspect, exit_code=255)
    lose_the_connection(fake_engine)
    out = run_service(fake_engine)
    assert out.run.exit_code == 0 and out.anomalies == ("network_not_isolated",) and out.service_log == b""


# --------------------------------------------------------------------------- inputs in the archive

def test_inputs_are_packed_under_qa_input(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "a.txt").write_text("a")
    source = tmp_path / "plan.yaml"
    source.write_bytes(b"plan: 1\n")
    archive = tmp_path / "out.tar"
    container.project_archive(root, archive, 1 << 20, inputs=[("security/plan.yaml", source)])
    with tarfile.open(str(archive)) as tar:
        member = tar.getmember(".qa-input/security/plan.yaml")
        assert tar.extractfile(member).read() == b"plan: 1\n" and member.mode == 0o644 and (member.uid, member.uname) == (0, "")
        directories = {m.name: m.mode for m in tar.getmembers() if m.isdir()}
    assert directories == {".qa-input": 0o755, ".qa-input/security": 0o755}
    assert names_in(archive) == [".qa-input", ".qa-input/security", ".qa-input/security/plan.yaml", "a.txt"]


def test_a_project_entry_named_qa_input_stays_out(tmp_path):
    root = tmp_path / "plain"
    (root / ".qa-input").mkdir(parents=True)
    (root / ".qa-input" / "planted.yaml").write_text("not an input")
    (root / ".QA-Input").mkdir(exist_ok=True)
    (root / "a.txt").write_text("a")
    archive = tmp_path / "out.tar"
    container.project_archive(root, archive, 1 << 20)
    assert names_in(archive) == ["a.txt"]


@pytest.mark.parametrize("name", ["../x", "/etc/x", "a/../../x", "./x", "a//b", "", ".", "a/"])
def test_an_input_name_that_leaves_qa_input_is_refused(tmp_path, name):
    root = tmp_path / "plain"
    root.mkdir()
    source = tmp_path / "f"
    source.write_bytes(b"x")
    archive = tmp_path / "out.tar"
    with pytest.raises(QaError) as refused:
        container.project_archive(root, archive, 1 << 20, inputs=[(name, source)])
    assert refused.value.code == "project_unreadable" and not archive.exists()


def test_an_input_source_that_is_a_link_is_refused(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    target = tmp_path / "secret"
    target.write_bytes(b"SECRET-OUTSIDE")
    (tmp_path / "link").symlink_to(target)
    archive = tmp_path / "out.tar"
    with pytest.raises(QaError) as refused:
        container.project_archive(root, archive, 1 << 20, inputs=[("f", tmp_path / "link")])
    assert refused.value.code == "project_unreadable" and not archive.exists()


def test_inputs_count_against_the_archive_cap(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    source = tmp_path / "big"
    source.write_bytes(b"x" * (1 << 15))
    with pytest.raises(QaError) as refused:
        container.project_archive(root, tmp_path / "out.tar", 1 << 14, inputs=[("big", source)])
    assert refused.value.code == "project_too_large"
