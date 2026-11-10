"""The one place that spawns the container engine: which engines exist, what may reach them, who may choose one."""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from helpers import finishes
from qa import config, container, integrity, runner, workspace
from qa.canonical import loads
from qa.errors import QaError

SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes", "-o", "LogLevel=ERROR")
VERSION_JSON = json.dumps({"Client": {"Version": "9"}, "Server": {"Version": "27.5.1", "Os": "linux", "Arch": "amd64"}}).encode()
ECHO_STDIN = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"


# --------------------------------------------------------------------------- engine shapes

@pytest.mark.parametrize("bad", ["ssh x; rm", "ssh -oProxyCommand=y docker", "bash", "ssh a wsl docker", ""],
                         ids=["command_separator", "ssh_option_as_alias", "other_program", "incomplete_wsl_form", "empty"])
def test_engine_shapes_refused(bad):
    with pytest.raises(QaError) as refused:
        container.parse_engine(bad)
    assert refused.value.code == "container_engine_invalid"


@pytest.mark.parametrize("text, argv", [
    ("docker", ("docker",)),
    ("ssh build.host-1 docker", ("ssh",) + SSH_OPTIONS + ("build.host-1", "docker")),
    ("ssh engine-host wsl -e docker", ("ssh",) + SSH_OPTIONS + ("engine-host", "wsl", "-e", "docker")),
])
def test_accepted_shapes(text, argv):
    assert container.parse_engine(text) == argv


def test_ssh_engine_gets_its_safety_options(fake_engine):
    engine = container.Engine("ssh engine-host wsl -e docker")
    fake_engine.engine = engine
    engine.run(["ps"], stdin_path=None, timeout_s=5, max_bytes=1000)
    assert fake_engine.spawns[0]["command"] == ["ssh", *SSH_OPTIONS, "engine-host", "wsl", "-e", "docker", "ps"]


def test_ssh_runs_quiet_so_banners_and_host_notices_stay_out_of_the_logs():  # I7
    argv = container.parse_engine("ssh engine-host docker")
    assert argv[argv.index("LogLevel=ERROR") - 1] == "-o" and argv.index("LogLevel=ERROR") < argv.index("engine-host")


# --------------------------------------------------------------------------- the token rule

@pytest.mark.parametrize("bad", ["a b", "x;y", "$HOME", "{{.Id}}", "`w`", "a,b", "a|b", "(x)", "'q'", ""],
                         ids=["space", "separator", "variable", "template", "backtick", "comma", "pipe", "group", "quote", "empty"])
def test_unsafe_token_refused_before_spawn(fake_engine, bad):
    with pytest.raises(QaError) as refused:
        fake_engine.engine.run(["run", bad], stdin_path=None, timeout_s=5, max_bytes=1000)
    assert refused.value.code == "container_unsafe_token"
    assert fake_engine.calls == []


def test_one_string_is_not_a_list_of_arguments(fake_engine):
    with pytest.raises(QaError) as refused:
        fake_engine.engine.run("ps", stdin_path=None, timeout_s=5, max_bytes=1000)
    assert refused.value.code == "container_unsafe_token" and fake_engine.calls == []


def test_the_tokens_a_pipeline_needs_reach_the_engine_unchanged(fake_engine):
    args = ["run", "-i", "--entrypoint=", "--label=qa.execution=exe-1", "--volume=qa-exe-work:/work",
            "python@sha256:" + "a" * 64, "tar", "-c", "-C", "/qa-evidence", "."]
    fake_engine.engine.run(args, stdin_path=None, timeout_s=5, max_bytes=1000)
    assert fake_engine.calls == [args]


# --------------------------------------------------------------------------- how it is spawned

def test_no_shell_a_neutral_directory_and_the_callers_limits(fake_engine, tmp_path):
    stream = tmp_path / "stream"
    fake_engine.engine.run(["ps"], stdin_path=stream, timeout_s=7, max_bytes=99)
    spawn = fake_engine.spawns[0]
    assert (spawn["shell"], spawn["timeout_s"], spawn["max_bytes"], spawn["stdin_path"]) == (False, 7, 99, stream)
    assert str(spawn["cwd"]) == os.path.abspath(os.sep)


def test_a_real_spawn_carries_the_stream_exactly_and_no_host_secret(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text('#!/bin/sh\nenv > "$2"\nexec cat\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", "%s%s%s" % (bin_dir, os.pathsep, os.environ["PATH"]))
    monkeypatch.setenv("QA_TEST_HOST_SECRET", "not-for-the-engine")
    stream = tmp_path / "stream"
    payload = bytes(range(256)) * 16 + b"\r\n\0"
    stream.write_bytes(payload)
    env_dump = tmp_path / "env-dump"
    done = container.Engine("docker").run(["dump", str(env_dump)], stdin_path=stream, timeout_s=30, max_bytes=1 << 20)
    assert (done.exit_code, done.stdout) == (0, payload)
    seen = env_dump.read_text()
    assert "QA_TEST_HOST_SECRET" not in seen and "PATH=" in seen


# --------------------------------------------------------------------------- runner.stdin_path

def _run(py, tmp_path, **kw):
    return runner.run_process(py.argv(ECHO_STDIN), shell=False, cwd=tmp_path, env=py.env, timeout_s=30, max_bytes=1 << 20, **kw)


def test_stdin_path_is_the_process_stdin(py, tmp_path):
    stream = tmp_path / "stream"
    stream.write_bytes(b"a\r\nb\0c")
    assert _run(py, tmp_path, stdin_path=stream).stdout == b"a\r\nb\0c"


def test_stdin_stays_empty_without_a_path(py, tmp_path):
    done = _run(py, tmp_path)
    assert (done.exit_code, done.stdout) == (0, b"")


@pytest.mark.parametrize("make", [lambda path: None, os.mkfifo], ids=["missing", "fifo"])
def test_a_stream_that_is_not_a_regular_file_starts_nothing(py, tmp_path, make):
    stream = tmp_path / "stream"
    make(stream)
    done = finishes(lambda: _run(py, tmp_path, stdin_path=stream))
    assert (done.spawn_error, done.exit_code) == ("stdin_unreadable", None)


# --------------------------------------------------------------------------- facts and availability

def test_facts_ask_for_json_and_read_the_daemon(fake_engine):
    fake_engine.reply(stdout=VERSION_JSON)
    assert fake_engine.engine.facts() == container.DaemonFacts(version="27.5.1", os="linux", arch="amd64")
    assert fake_engine.calls == [["version", "--format", "json"]]


@pytest.mark.parametrize("reply, reason", [
    (dict(exit_code=255), "exit_nonzero"),
    (dict(timed_out=True, exit_code=None), "timeout"),
    (dict(stdout=b"<html>"), "malformed"),
    (dict(stdout=b'{"Server": null}'), "malformed"),
    (dict(stdout=b'{"Server": {"Version": "27", "Os": "linux"}}'), "malformed"),
], ids=["ssh_or_daemon_failure", "timeout", "not_json", "no_daemon_section", "field_missing"])
def test_unreachable_engine_raises_unavailable(fake_engine, reply, reason):
    fake_engine.reply(**reply)
    with pytest.raises(container.EngineUnavailable) as refused:
        fake_engine.engine.facts()
    assert isinstance(refused.value, QaError) and refused.value.details["reason"] == reason


def test_an_engine_that_is_not_installed_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(container.EngineUnavailable) as refused:
        container.Engine("docker").facts()
    assert refused.value.details["reason"] == "not_found"
    with pytest.raises(container.EngineUnavailable):
        container.Engine("docker").run(["ps"], stdin_path=None, timeout_s=5, max_bytes=1000)


def test_a_command_failing_inside_the_container_is_an_outcome_not_an_outage(fake_engine):
    # A process in the container can exit 255 too, so the code alone never says ssh failed.
    fake_engine.reply(exit_code=255)
    assert fake_engine.engine.run(["ps"], stdin_path=None, timeout_s=5, max_bytes=1000).exit_code == 255


def test_an_unreadable_stream_is_not_called_an_outage(fake_engine, tmp_path):
    fake_engine.reply(spawn_error="stdin_unreadable", exit_code=None)
    with pytest.raises(QaError) as refused:
        fake_engine.engine.run(["ps"], stdin_path=tmp_path / "gone", timeout_s=5, max_bytes=1000)
    assert refused.value.code == "container_stdin_unreadable"


# --------------------------------------------------------------------------- who may choose the engine

ENGINE = "ssh approved-host docker"


def _config_with_engine(ws, engine):
    """config.json changed by hand, the way a file arrives from outside the CLI."""
    cfg = loads(workspace.read_config(ws))
    cfg["container"] = {"engine": engine}
    workspace.write_atomic(ws.path("config.json"), workspace.config_bytes(cfg))


def _refused_engine(ws):
    with pytest.raises(QaError) as refused:
        config.container_engine(ws)
    assert refused.value.code == "container_engine_unapproved"


def test_the_engine_defaults_to_docker(ws, actor):
    config.record_initialized(ws, actor)
    assert config.container_engine(ws) == "docker"


def test_engine_setting_needs_a_recorded_approval(ws, actor):
    config.record_initialized(ws, actor)
    with pytest.raises(QaError) as missing:
        config.set_value(ws, actor, "container.engine", ENGINE, "  ")
    assert missing.value.code == "approval_required"
    assert config.container_engine(ws) == "docker"

    event = config.set_value(ws, actor, "container.engine", ENGINE, "the user said yes")
    assert (event["data"]["key"], event["data"]["new"], event["data"]["approval"]) == ("container.engine", ENGINE, "the user said yes")
    assert config.container_engine(ws) == ENGINE
    config.set_value(ws, actor, "defaults.flaky_reruns", 1, "unrelated change")
    assert config.container_engine(ws) == ENGINE


def test_an_engine_edited_by_hand_is_not_honored_even_once_accepted(ws, actor):
    config.record_initialized(ws, actor)
    config.set_value(ws, actor, "container.engine", ENGINE, "yes")
    _config_with_engine(ws, "ssh other-host docker")
    _refused_engine(ws)
    config.accept(ws, actor, "accepted a file that changed the engine")
    _refused_engine(ws)


def _engine_removed(ws):
    """config.json with the optional object taken out by hand."""
    cfg = loads(workspace.read_config(ws))
    del cfg["container"]
    workspace.write_atomic(ws.path("config.json"), workspace.config_bytes(cfg))


@pytest.mark.parametrize("edit", [lambda ws: _config_with_engine(ws, "docker"), _engine_removed],
                         ids=["value_changed_to_the_default", "object_removed"])
def test_a_hand_edit_back_to_the_local_engine_is_not_honored(ws, actor, edit):
    # The direction that matters most: project code would move from the remote daemon to the operator's own.
    config.record_initialized(ws, actor)
    config.set_value(ws, actor, "container.engine", ENGINE, "yes")
    edit(ws)
    _refused_engine(ws)
    config.accept(ws, actor, "accepted a file that changed the engine")
    _refused_engine(ws)


def test_going_back_to_the_default_through_config_set_is_honored(ws, actor):
    config.record_initialized(ws, actor)
    config.set_value(ws, actor, "container.engine", ENGINE, "yes")
    config.set_value(ws, actor, "container.engine", "docker", "back to the local engine")
    assert config.container_engine(ws) == "docker"


def test_a_malformed_engine_is_refused_as_configuration(ws, actor):
    config.record_initialized(ws, actor)
    with pytest.raises(QaError) as refused:
        config.set_value(ws, actor, "container.engine", "bash", "yes")
    assert refused.value.code == "config_invalid" and "container" not in loads(workspace.read_config(ws))


def test_the_container_object_cannot_be_set_whole(ws, actor):
    config.record_initialized(ws, actor)
    with pytest.raises(QaError) as refused:
        config.set_value(ws, actor, "container", '{"engine": "%s"}' % ENGINE, "yes")
    assert refused.value.code == "config_invalid"


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], check=True, capture_output=True)


def _clone_of(tmp_path, origin, *tracked):
    if shutil.which("git") is None:
        pytest.skip("tool missing: git")
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "add", "-f", *tracked)
    _git(origin, "commit", "-q", "-m", "ship the workspace")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    return clone


@pytest.fixture
def ws_tracked_ledger(tmp_path, actor):
    """A clone whose repository ships the ledger holding the approval of a shipped engine."""
    origin = workspace.init_workspace(tmp_path / "origin")[0]
    config.record_initialized(origin, actor)
    config.set_value(origin, actor, "container.engine", ENGINE, "shipped approval")
    return workspace.find_workspace(_clone_of(tmp_path, origin.root, ".qa"), None)


def test_shipped_config_engine_carries_no_authority(ws_tracked_ledger):
    assert integrity.authority(ws_tracked_ledger)["refused"]
    _refused_engine(ws_tracked_ledger)


def test_a_tracked_config_alone_cannot_choose_the_engine(tmp_path, actor):
    # config.json may be tracked, so authority has nothing to refuse here: only the missing approval stops it.
    origin = workspace.init_workspace(tmp_path / "origin")[0]
    _config_with_engine(origin, ENGINE)
    ws = workspace.init_workspace(_clone_of(tmp_path, origin.root, ".qa/config.json"))[0]
    config.record_initialized(ws, actor)
    assert integrity.authority(ws)["refused"] == []
    _refused_engine(ws)
