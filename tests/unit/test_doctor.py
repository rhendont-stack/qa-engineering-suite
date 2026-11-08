from __future__ import annotations

import os

import pytest

from qa import config, doctor, envinfo

modes_bind = pytest.mark.skipif(getattr(os, "geteuid", lambda: 0)() == 0, reason="file modes do not bind this user")  # T18-R17


@pytest.fixture
def no_tools(tmp_path, monkeypatch):
    """Nothing to find on PATH. The fingerprint cache comes back afterwards, so later tests see the real tools."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(envinfo, "_cached", None)
    return empty


# --------------------------------------------------------------------------- the brief's tests

def test_kernel_status_on_the_source_tree(ws, no_tools):
    kernel = doctor.doctor(ws)["kernel"]
    assert kernel["status"] == "absent" and kernel["source_checkout"] is True


def test_with_an_empty_path_every_tool_version_is_none(ws, no_tools):
    report = doctor.doctor(ws)
    assert set(report["environment"]["tools"].values()) == {None} and report["docker"] == {"server_version": None}


# --------------------------------------------------------------------------- a broken project (T18-R14)

@pytest.mark.parametrize("content", [b"{not json", b"caf\xe9", b"[]", b"{}", b'{"schema_version": "x"}'],
                         ids=["malformed", "not-utf8", "not-an-object", "not-a-config", "bad-version"])
def test_a_config_it_cannot_use_reads_as_drift(ws, qa_cli, no_tools, content):
    ws.path("config.json").write_bytes(content)
    code, out = qa_cli("doctor")
    assert (code, out["config"]) == (0, {"drift": True})


@modes_bind
def test_a_config_it_cannot_read_reads_as_drift(ws, qa_cli, no_tools):
    ws.path("config.json").chmod(0)
    code, out = qa_cli("doctor")
    assert (code, out["config"]) == (0, {"drift": True})


def test_an_invalid_config_reads_as_drift_even_as_the_accepted_one(ws, no_tools, monkeypatch):
    ws.path("config.json").write_bytes(b"{}")
    monkeypatch.setattr(config, "accepted_hash", lambda st: config.current_hash(ws))
    assert doctor.doctor(ws)["config"] == {"drift": True}


def test_an_accepted_config_has_no_drift(trusted, no_tools):
    assert doctor.doctor(trusted)["config"] == {"drift": False}


def test_an_edited_config_has_drift(trusted, no_tools):
    trusted.path("config.json").write_bytes(trusted.path("config.json").read_bytes() + b" ")
    assert doctor.doctor(trusted)["config"] == {"drift": True}


def test_a_broken_default_session_is_reported_on(ws, qa_cli, no_tools, monkeypatch):
    monkeypatch.delenv("QA_SESSION", raising=False)
    ws.path("default-session").write_bytes(b"nope\n")
    code, out = qa_cli("doctor")
    assert (code, out["session"]) == (0, None) and out["ledger"] == []


def test_a_session_it_was_given_is_still_checked(ws, qa_cli, no_tools):
    code, out = qa_cli("--session", "bad", "doctor")
    assert (code, out["error"]["code"]) == (2, "invalid_session")


def test_doctor_and_ledger_verify_give_one_answer(trusted, qa_cli, no_tools):
    path = next(trusted.path("ledger").glob("*.jsonl"))
    path.write_bytes(path.read_bytes() + b"not an event\n")
    code, verify = qa_cli("ledger", "verify")
    assert code == 1 and verify["problems"] and qa_cli("doctor")[1]["ledger"] == verify["problems"]


def test_a_ledger_it_cannot_fold_is_a_problem_not_a_crash(ws, no_tools, monkeypatch):
    def broken(_ws):
        raise OSError("disk trouble")
    monkeypatch.setattr(doctor.state, "load", broken)
    report = doctor.doctor(ws)
    assert [problem["kind"] for problem in report["ledger"]] == ["invalid"] and report["config"] == {"drift": True}


def test_doctor_writes_nothing(trusted, qa_cli, no_tools):
    def contents():
        return sorted((str(path), path.read_bytes()) for path in trusted.qa_dir.rglob("*") if path.is_file())
    before = contents()
    assert qa_cli("doctor")[0] == 0 and contents() == before


def test_the_doctor_command_with_no_docker_on_path(ws, qa_cli, no_tools):
    code, out = qa_cli("doctor")
    assert code == 0 and out["docker"] == {"server_version": None} and {"kernel", "ledger", "config", "environment", "docker"} <= set(out)


# --------------------------------------------------------------------------- the Docker probe (T18-R14): a stub, never Docker

@pytest.fixture
def fake_docker(no_tools, tmp_path):
    """``install(body)`` puts a ``docker`` on PATH that runs ``body``; returns the file listing the arguments of each call."""
    log = tmp_path / "docker-calls"

    def install(body):
        stub = no_tools / "docker"
        stub.write_text('#!/bin/sh\necho "$*" >> "%s"\n%s\n' % (log, body))
        stub.chmod(0o755)
        return log
    return install


def test_the_version_comes_from_the_read_only_probe(ws, fake_docker):
    log = fake_docker("echo 27.3.1")
    assert doctor.doctor(ws)["docker"] == {"server_version": "27.3.1"}
    calls = log.read_text().splitlines()
    assert calls[-1] == "info --format {{.ServerVersion}}" and set(calls) <= {"--version", "info --format {{.ServerVersion}}"}


@pytest.mark.parametrize("body", ["exit 1", "echo 27.3.1; exit 1", "true", "printf '\\377'"],
                         ids=["fails", "fails-after-printing", "prints-nothing", "prints-no-text"])
def test_any_docker_failure_reads_as_no_version(ws, fake_docker, body):
    fake_docker(body)
    assert doctor.doctor(ws)["docker"] == {"server_version": None}


def test_a_docker_that_cannot_start_reads_as_no_version(ws, no_tools):
    stub = no_tools / "docker"
    stub.write_text("#!/nonexistent/interpreter\n")
    stub.chmod(0o755)
    assert doctor.doctor(ws)["docker"] == {"server_version": None}


def test_a_docker_that_hangs_reads_as_no_version(ws, fake_docker, monkeypatch):
    monkeypatch.setattr(doctor, "_DOCKER_TIMEOUT_S", 0.2)
    fake_docker("exec /bin/sleep 30")
    assert doctor.doctor(ws)["docker"] == {"server_version": None}


def test_the_probe_waits_the_briefs_five_seconds():
    assert doctor._DOCKER_TIMEOUT_S == 5
