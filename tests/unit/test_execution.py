from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

import qa
from helpers import WRITE_JUNIT, req
from qa import authz, canonical, config, envinfo, execution, ids, ingest, integrity, ledger, schema, state, workspace
from qa.errors import QaError


# --------------------------------------------------------------------------- the brief's tests

def test_passed_structured_with_evidence(trusted, actor, py):
    r = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))
    ev = trusted.path("evidence", r["execution"])
    assert (r["status"], r["strength"], r["tests"]["total"]) == ("passed", "structured", 1)
    assert r["tests"] == {"total": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0} and r["findings"] == {"opened": 0, "updated": 0}
    assert {"command.json", "stdout.log", "stderr.log", "manifest.json"} <= {p.name for p in ev.iterdir()}
    st = state.load(trusted)
    assert st["executions"][r["execution"]]["command"]["argv"][-2] == "{evidence}/junit.xml" and len(st["results"]) == 1
    assert integrity.verify_evidence(trusted, st) == []


def test_stale_report_is_report_missing(trusted, actor, py):
    stale = trusted.root / "out" / "junit.xml"; stale.parent.mkdir()
    stale.write_text('<testsuite><testcase classname="c" name="t"/></testsuite>')
    r = execution.execute(trusted, actor, execution.RunRequest(command=py.argv("import sys; sys.exit(0)"), reports=(("junit", str(stale)),)))
    assert (r["status"], r["reason"]) == ("inconclusive", "report_missing")


def test_untrusted_is_blocked_before_spawn(ws, actor, py, tmp_path):
    config.record_initialized(ws, actor); marker = tmp_path / "ran"
    r = execution.execute(ws, actor, execution.RunRequest(command=py.argv(f"open({str(marker)!r}, 'w')")))
    assert (r["status"], r["reason"]) == ("blocked", "not_authorized") and not marker.exists()


def test_guard_deny_records_command_blocked(trusted, actor):
    r = execution.execute(trusted, actor, execution.RunRequest(command=["git", "push"]))
    assert (r["status"], r["reason"]) == ("blocked", "policy_denied") and state.load(trusted)["blocked_commands"]


def test_drift_blocks(trusted, actor, py):
    trusted.path("config.json").write_bytes(trusted.path("config.json").read_bytes() + b" ")
    assert execution.execute(trusted, actor, execution.RunRequest(command=py.argv("pass")))["reason"] == "config_drift"


def test_secret_in_output_redacted_and_hashed_after(trusted, actor, py):
    tok = "gh" + "p_" + "Q7w8E9r0T1" * 3 + "y2U3i4"   # through the environment, never argv (§12.5)
    r = execution.execute(trusted, actor, execution.RunRequest(command=py.argv("import os; print(os.environ['QA_TEST_OUT'])")),
                          environ={**os.environ, "QA_TEST_OUT": tok})
    ev = trusted.path("evidence", r["execution"]); m = json.loads((ev / "manifest.json").read_text())
    assert tok not in (ev / "stdout.log").read_text() and m["stdout.log"]["redactions"] == 1
    assert m["stdout.log"]["sha256"] == canonical.sha256_file(ev / "stdout.log")


def test_huge_binary_output_truncated_with_marker(trusted, actor, py):
    config.set_value(trusted, actor, "defaults.max_log_mb", 1, "test approval")
    r = execution.execute(trusted, actor, execution.RunRequest(command=py.argv("import sys; sys.stdout.buffer.write(b'\\xff\\xfe' + b'x' * (3 * 2**19))")))
    data = trusted.path("evidence", r["execution"], "stdout.log").read_bytes()
    assert data.startswith(b"\xff\xfe") and data.endswith(b" bytes omitted by qa]\n") and len(data) < 2**20 + 100


def test_orphaned_output_recorded(trusted, actor, py):
    code = "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])"
    r = execution.execute(trusted, actor, execution.RunRequest(command=py.argv(code)), grace_s=0.5)
    e = state.load(trusted)["executions"][r["execution"]]
    assert "orphaned_output" in e["limitations"] and "partial" in r["anomalies"]


def test_timeout_and_missing_tool(trusted, actor, py):
    assert execution.execute(trusted, actor, execution.RunRequest(command=py.argv("import time; time.sleep(30)"), timeout_s=1))["reason"] == "timeout"
    assert execution.execute(trusted, actor, execution.RunRequest(command=["qa-no-such-tool"]))["reason"] == "tool_missing"


def test_flaky_protocol(trusted, actor, py):
    outcomes = ["fail", "pass", "fail"]; first = None
    for o in outcomes:
        r = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", o, attempt_of=first)); first = first or r["execution"]
    v = execution.flaky_view(state.load(trusted), first)["c::t"]
    assert (v["attempts"], v["passes"], v["flaky"], v["status"], v["reproducible"]) == (3, 1, True, "failed", False)
    assert r["flaky"]["c::t"] == v   # the last attempt's summary carries the chain view
    extra = [execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "fail", attempt_of=first)) for _ in range(2)]
    assert extra[-1]["reason"] == "policy_denied"   # chain longer than 1 + flaky_reruns


def test_reproducible_failure(trusted, actor, py):
    first = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "fail"))["execution"]
    execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "fail", attempt_of=first))
    assert execution.flaky_view(state.load(trusted), first)["c::t"]["reproducible"] is True


def test_reclassify_limits(trusted, actor, py):
    r = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "fail"))
    res = next(iter(state.load(trusted)["results"]))
    execution.reclassify(trusted, actor, res, "blocked", "env was down", [r["execution"]])
    with pytest.raises(QaError): execution.reclassify(trusted, actor, res, "passed", "looks fine", [r["execution"]])


def test_ingest_strength_depends_on_manifest_match(trusted, actor, py, tmp_path):
    r = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))
    stored = trusted.path("evidence", r["execution"], "reports", "junit.xml")
    assert execution.ingest_file(trusted, actor, "junit", stored, from_execution=r["execution"])["strength"] == "structured"
    other = tmp_path / "x.xml"; other.write_text(stored.read_text().replace('name="t"', 'name="u"'))
    assert execution.ingest_file(trusted, actor, "junit", other, from_execution=r["execution"])["strength"] == "attested"


def test_shell_mode_quotes_evidence_path(trusted, actor, py):
    cmd = shlex.join(py.argv(WRITE_JUNIT)) + " {evidence}/junit.xml pass"   # the evidence path holds "My Skills"
    r = execution.execute(trusted, actor, execution.RunRequest(command=cmd, shell=True, reports=(("junit", "{evidence}/junit.xml"),)))
    assert (r["status"], r["strength"]) == ("passed", "structured")


def test_url_hosts_are_authorized_by_their_environment(trusted, actor, py):
    assert execution.execute(trusted, actor, execution.RunRequest(command=py.argv("pass", "http://localhost:8080/x")))["status"] == "passed"
    r = execution.execute(trusted, actor, execution.RunRequest(command=py.argv("pass", "https://example.com/x")))
    assert (r["status"], r["reason"]) == ("blocked", "not_authorized")   # undeclared host → production


def test_cwd_is_used_and_recorded(trusted, actor, py):
    sub = trusted.root / "sub dir"; sub.mkdir()
    r = execution.execute(trusted, actor, execution.RunRequest(command=py.argv("import os; print(os.getcwd())"), cwd=sub))
    assert trusted.path("evidence", r["execution"], "stdout.log").read_text().strip() == str(sub)
    assert state.load(trusted)["executions"][r["execution"]]["command"]["cwd"] == "sub dir"


def test_env_values_only_for_allowlist(trusted, actor, py):
    env = {**os.environ, "CI": "true", "NODE_ENV": "test", "OTHER_SETTING": "x"}
    r = execution.execute(trusted, actor, execution.RunRequest(command=py.argv("pass")), environ=env)
    e = state.load(trusted)["executions"][r["execution"]]
    assert e["env_values"] == {"CI": "true", "NODE_ENV": "test"} and "OTHER_SETTING" in e["env_names"]


# --------------------------------------------------------------------------- helpers for the rulings

PASS_XML = '<testsuite><testcase classname="c" name="t"/></testsuite>'
CTRF = '{"results": {"tests": [{"name": "u", "status": "passed"}]}}'
SARIF_CLEAN = '{"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "x"}}, "results": []}]}'
SARIF_ONE = ('{"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "x"}}, "results": '
             '[{"ruleId": "r", "level": "error", "message": {"text": "m"}}]}]}')
# python -c code: writes argv[1] to each of the paths that follow it.
WRITE_FILES = "import pathlib, sys; [pathlib.Path(p).write_text(sys.argv[1]) for p in sys.argv[2:]]"
STAGING = {"id": "staging", "class": "staging", "base_urls": ["https://staging.shop.example"]}
STAGING_HOST = "staging.shop.example"
STAGING_URL = "https://staging.shop.example/x"
APPROVAL = "test approval"
BRIEF_KEYS = {"execution", "status", "reason", "strength", "anomalies", "tests", "flaky", "evidence", "findings"}
TOKEN = "gh" + "p_" + "Q7w8E9r0T1" * 3 + "y2U3i4"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _run(ws, actor, py, code, *args, environ=None, grace_s=5.0, **kw):
    return execution.execute(ws, actor, execution.RunRequest(command=py.argv(code, *args), **kw),
                             environ=environ, grace_s=grace_s)


def _record(ws, result):
    return state.load(ws)["executions"][result["execution"]]


def _dir(ws, result):
    return ws.path("evidence", result["execution"])


def _manifest(ws, result):
    return json.loads((_dir(ws, result) / "manifest.json").read_text(encoding="utf-8"))


def _data(ws, type_):
    return [e["data"] for e in ledger.iter_all_events(ws) if e["type"] == type_]


def _touch(marker):
    return "open(%r, 'w').close()" % str(marker)


def _drift(ws):
    ws.path("config.json").write_bytes(ws.path("config.json").read_bytes() + b" ")


def _declare(ws, actor, *environments):
    cfg = config.load_config(ws)
    config.set_value(ws, actor, "environments", cfg["environments"] + list(environments), APPROVAL)


def _stub(directory, name="curl", body="exit 0", mode=0o755):
    path = directory / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(mode)
    return str(path)


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repo(trusted):
    _git(trusted.root, "init", "-q", "-b", "main")
    (trusted.root / "a.txt").write_text("1")
    _git(trusted.root, "add", "a.txt")
    _git(trusted.root, "commit", "-q", "-m", "c1")
    return trusted


TS = "2026-11-08T12:00:00.000Z"
T0 = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
EXE_A, EXE_B, EXE_C = ("EXE-" + "0" * 25 + c for c in "ABC")
RES_A, RES_B, RES_C = ("RES-" + "0" * 25 + c for c in "ABC")


def _execution_data(exe_id, **changes):
    """Complete, schema-valid execution.recorded data, for events made by hand."""
    data = {
        "id": exe_id, "command": {"argv": ["t"], "shell": False, "cwd": "."},
        "started_at": TS, "ended_at": TS, "duration_ms": 0, "exit_code": 0, "signal": None, "timed_out": False,
        "git": None,
        "environment": {"class": "local", "platform": "p", "python": "3", "implementation": "CPython", "tools": {}},
        "environment_id": "local", "env_names": [], "env_values": {}, "isolation": "host",
        "evidence_manifest": None, "reports": [], "report": None,
        "status": "passed", "reason": None, "strength": "exit_code", "anomalies": [], "classification": "deterministic",
        "authorization": "trusted:local", "authorizations": [], "activity": "test-execution-local",
        "scope": "targeted", "category": "functional", "attempt_of": None,
        "config_hash": "sha256:" + "0" * 64, "kernel": {"version": "0.1.0-dev", "manifest": None},
        "actor": {"role": "verifier", "skill": None, "session": "S-" + "0" * 26},
        "independence": "I1", "redactions": 0, "limitations": [],
    }
    data.update(changes)
    return data


def _result_data(res_id, exe_id, status="failed", test_id="c::t", **changes):
    data = {"id": res_id, "execution": exe_id, "test_id": test_id, "status": status,
            "failure_kind": "assertion" if status == "failed" else None, "skip_reason": None, "flaky": False,
            "duration_ms": None}
    data.update(changes)
    return data


def _reclassified(res_id, new_status):
    return {"result": res_id, "new_status": new_status, "reason": "why", "evidence": [EXE_A]}


def _ingested(exe_id):
    return {"execution": exe_id, "format": "junit", "kind": "tests", "total": 1, "file": "ingested/x.xml",
            "sha256": "sha256:" + "0" * 64, "ev_id": "EV-" + "0" * 12, "strength": "attested", "category": "security"}


def _fold(*events):
    return state.fold([{"id": "EVT-%026d" % (i + 1), "type": t, "data": d} for i, (t, d) in enumerate(events)])


def _exe(exe_id, **changes):
    return "execution.recorded", _execution_data(exe_id, **changes)


def _res(res_id, exe_id, status="failed", **changes):
    return "result.recorded", _result_data(res_id, exe_id, status, **changes)


def _refusal(ws, actor, **fields):
    with pytest.raises(QaError) as e:
        execution.execute(ws, actor, execution.RunRequest(**fields))
    return e.value


# --------------------------------------------------------------------------- T14-R1: malformed requests

REFUSED = [
    ({"command": []}, "invalid_command"),
    ({"command": ""}, "invalid_command"),
    ({"command": ["ok", 1]}, "invalid_command"),
    ({"command": None}, "invalid_command"),
    ({"shell": True}, "invalid_command"),   # the request keeps its list command
    ({"scope": "everything"}, "invalid_scope"),
    ({"category": "nonsense"}, "invalid_category"),
    ({"timeout_s": 0}, "invalid_timeout"),
    ({"timeout_s": -1.5}, "invalid_timeout"),
    ({"timeout_s": float("nan")}, "invalid_timeout"),
    ({"reports": (("junit", "a.xml"), ("sarif", "b.sarif"))}, "kind_mismatch"),
    ({"environment": "nowhere"}, "unknown_environment"),
    ({"cwd": "no-such-directory"}, "invalid_cwd"),
    ({"activity": "nonsense"}, "invalid_activity"),
]


@pytest.mark.parametrize("changes,code", REFUSED, ids=[c + "-" + str(i) for i, (_, c) in enumerate(REFUSED)])
def test_malformed_request_is_refused_before_anything_runs(trusted, actor, py, tmp_path, changes, code):
    marker = tmp_path / "ran"
    before = len(ledger.iter_all_events(trusted))
    error = _refusal(trusted, actor, **{"command": py.argv(_touch(marker)), **changes})
    assert error.code == code and not marker.exists()
    assert len(ledger.iter_all_events(trusted)) == before


def test_report_format_without_a_parser_is_refused(trusted, actor, py, tmp_path):
    marker = tmp_path / "ran"
    error = _refusal(trusted, actor, command=py.argv(_touch(marker)), reports=(("cobertura", "a.xml"),))
    assert (error.code, error.details["detail"]) == ("report_invalid", "unknown_format") and not marker.exists()
    assert isinstance(error, ingest.ReportInvalid)


def test_format_kinds_name_every_parser():
    assert set(ingest.FORMAT_KINDS) == set(ingest._PARSERS)
    assert ingest.FORMAT_KINDS == {"junit": "tests", "ctrf": "tests", "sarif": "alerts"}


def test_string_without_the_shell_flag_goes_to_the_guard(trusted, actor):
    r = execution.execute(trusted, actor, execution.RunRequest(command="echo hi"))
    assert (r["status"], r["reason"]) == ("blocked", "policy_denied")
    assert state.load(trusted)["blocked_commands"][0]["rule_id"] == "shell-without-flag"


def test_unparseable_config_is_config_invalid_and_nothing_runs(trusted, actor, py, tmp_path):
    marker = tmp_path / "ran"
    trusted.path("config.json").write_bytes(b"{ not json")
    assert _refusal(trusted, actor, command=py.argv(_touch(marker))).code == "config_invalid"
    assert not marker.exists()


def test_cwd_outside_the_workspace_is_invalid(trusted, actor, py, tmp_path):
    marker = tmp_path / "ran"
    assert _refusal(trusted, actor, command=py.argv(_touch(marker)), cwd=tmp_path).code == "invalid_cwd"
    assert not marker.exists()


def test_cwd_that_is_a_file_is_invalid(trusted, actor, py, tmp_path):
    (trusted.root / "f.txt").write_text("x")
    assert _refusal(trusted, actor, command=py.argv("pass"), cwd="f.txt").code == "invalid_cwd"


def test_symlinked_cwd_leaving_the_workspace_is_invalid(trusted, actor, py, tmp_path):
    (trusted.root / "out").symlink_to(tmp_path)
    assert _refusal(trusted, actor, command=py.argv("pass"), cwd="out").code == "invalid_cwd"


def test_cwd_holding_a_nul_is_invalid(trusted, actor, py):
    assert _refusal(trusted, actor, command=py.argv("pass"), cwd="a\0b").code == "invalid_cwd"


def test_relative_cwd_is_read_against_the_workspace_root(trusted, actor, py):
    (trusted.root / "pkg").mkdir()
    r = _run(trusted, actor, py, "import os; print(os.getcwd())", cwd="pkg")
    assert (_dir(trusted, r) / "stdout.log").read_text().strip() == str(trusted.root / "pkg")
    assert _record(trusted, r)["command"]["cwd"] == "pkg"


def test_default_cwd_is_the_workspace_root_recorded_as_a_dot(trusted, actor, py):
    r = _run(trusted, actor, py, "import os; print(os.getcwd())")
    assert (_dir(trusted, r) / "stdout.log").read_text().strip() == str(trusted.root)
    assert _record(trusted, r)["command"]["cwd"] == "."


# --------------------------------------------------------------------------- T14-R2: stages 1-4 all run

def test_policy_denied_outranks_config_drift(trusted, actor, py):
    _drift(trusted)
    r = _run(trusted, actor, py, "pass", activity="deploy")
    assert (r["status"], r["reason"]) == ("blocked", "policy_denied")
    assert state.load(trusted)["config"]["drift"]    # the drift stage still ran


def test_config_drift_outranks_not_authorized(ws, actor, py):
    config.record_initialized(ws, actor)     # untrusted, so authorization fails too
    _drift(ws)
    assert _run(ws, actor, py, "pass")["reason"] == "config_drift"


def test_guard_deny_with_drift_still_reads_policy_denied_and_records_both(trusted, actor):
    _drift(trusted)
    r = execution.execute(trusted, actor, execution.RunRequest(command=["git", "push"]))
    st = state.load(trusted)
    assert r["reason"] == "policy_denied" and st["blocked_commands"] and st["config"]["drift"]


def test_attempt_violation_outranks_not_authorized(ws, actor, py):
    config.record_initialized(ws, actor)
    assert _run(ws, actor, py, "pass", attempt_of=EXE_A)["reason"] == "policy_denied"


def test_drift_is_checked_for_every_required_activity(trusted, actor):
    _drift(trusted)     # static-analysis skips the drift check; the guard adds install-dependencies
    r = execution.execute(trusted, actor, execution.RunRequest(command=["npm", "install"], activity="static-analysis"))
    assert r["reason"] == "config_drift"


def test_static_analysis_alone_is_not_blocked_by_drift(trusted, actor, py):
    _drift(trusted)
    assert _run(trusted, actor, py, "pass", activity="static-analysis")["status"] == "passed"


def test_only_a_guard_deny_records_command_blocked(trusted, actor, py):
    assert _run(trusted, actor, py, "pass", activity="deploy")["reason"] == "policy_denied"
    assert state.load(trusted)["blocked_commands"] == []


def test_command_blocked_shape_and_order(trusted, actor):
    r = execution.execute(trusted, actor, execution.RunRequest(command=["git", "push"]))
    entry = state.load(trusted)["blocked_commands"][0]
    assert set(entry) == {"event", "execution", "activity", "rule_id", "reason", "command"}
    assert (entry["execution"], entry["activity"], entry["rule_id"]) == (r["execution"], "test-execution-local", "git-write")
    assert entry["command"] == {"argv": ["git", "push"], "shell": False, "cwd": "."}
    order = [e["type"] for e in ledger.iter_all_events(trusted)]
    assert order.index("command.blocked") < order.index("execution.recorded")


@pytest.mark.parametrize("command, activity, needle", [
    (["git", "push"], "test-execution-local", "git-write"),   # the rule
    (["echo"], "deploy", "deploy"),                           # the activity
    (["npm", "install"], "test-execution-local", "install-dependencies"),   # a required activity
])
def test_every_denial_is_explained_in_limitations(trusted, actor, command, activity, needle):
    r = execution.execute(trusted, actor, execution.RunRequest(command=command, activity=activity))
    assert r["status"] == "blocked" and any(needle in text for text in r["limitations"])
    assert _record(trusted, r)["limitations"] == r["limitations"]


def test_denial_for_drift_and_attempt_are_explained(trusted, actor, py):
    _drift(trusted)
    assert any("drift" in text for text in _run(trusted, actor, py, "pass")["limitations"])
    limitations = _run(trusted, actor, py, "pass", attempt_of=EXE_A, activity="static-analysis")["limitations"]
    assert any("attempt_of" in text for text in limitations)


def test_execute_returns_the_brief_keys_plus_limitations(trusted, actor, py):
    assert set(execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))) == BRIEF_KEYS | {"limitations"}
    assert set(execution.execute(trusted, actor, execution.RunRequest(command=["git", "push"]))) == BRIEF_KEYS | {"limitations"}


def test_a_require_grant_decision_goes_to_authorization(trusted, actor):
    r = execution.execute(trusted, actor, execution.RunRequest(command=["npm", "install"]))
    assert (r["status"], r["reason"]) == ("blocked", "not_authorized")
    assert state.load(trusted)["blocked_commands"] == []


# --------------------------------------------------------------------------- T14-R3: runs blocked before stage 5

def test_blocked_run_has_no_evidence_and_starts_no_probe(ws, actor, py, tmp_path, monkeypatch):
    config.record_initialized(ws, actor)
    def refuse(**_kw):
        raise AssertionError("tool probes run only for an execution that reaches stage 5")
    monkeypatch.setattr(envinfo, "environment_fingerprint", refuse)
    r = _run(ws, actor, py, _touch(tmp_path / "ran"), reports=(("junit", "x.xml"),))
    e = _record(ws, r)
    assert r["evidence"] is None and e["evidence_manifest"] is None and e["report"] is None
    assert list(ws.path("evidence").iterdir()) == []
    assert e["started_at"] == e["ended_at"] and e["duration_ms"] == 0
    assert (e["exit_code"], e["signal"], e["timed_out"]) == (None, None, False)
    assert e["environment"] == {"class": "local", **envinfo.host_facts(), "tools": {}}
    assert e["reports"] == [{"format": "junit", "pattern": "x.xml", "files": [], "error": None}]
    assert e["redactions"] == 0 and r["tests"]["total"] == 0 and not (tmp_path / "ran").exists()


def test_a_run_that_reaches_stage_5_reports_its_evidence_path(trusted, actor, py):
    r = _run(trusted, actor, py, "pass")
    assert r["evidence"] == ".qa/evidence/" + r["execution"]


def test_a_run_that_reaches_stage_5_records_the_tool_fingerprint(trusted, actor, py):
    r = _run(trusted, actor, py, "pass")
    assert _record(trusted, r)["environment"] == {"class": "local", **envinfo.environment_fingerprint()}


def test_spawn_failure_is_blocked_with_clean_evidence(trusted, actor):
    r = execution.execute(trusted, actor, execution.RunRequest(command=["qa-no-such-tool"], reports=(("junit", "x.xml"),)))
    assert (r["status"], r["reason"], r["strength"]) == ("blocked", "tool_missing", None)
    assert r["evidence"] == ".qa/evidence/" + r["execution"]
    st = state.load(trusted)
    assert st["executions"][r["execution"]]["evidence_manifest"] is not None
    assert (st["executions"][r["execution"]]["exit_code"], st["executions"][r["execution"]]["signal"]) == (None, None)
    assert integrity.verify_evidence(trusted, st) == []


def test_not_executable_file_is_blocked(trusted, actor, tmp_path):
    r = execution.execute(trusted, actor, execution.RunRequest(command=[_stub(tmp_path, "tool", mode=0o644)]))
    assert (r["status"], r["reason"]) == ("blocked", "not_executable")


def test_shell_exit_127_is_tool_missing(trusted, actor):
    r = execution.execute(trusted, actor, execution.RunRequest(command="qa-no-such-tool --help", shell=True))
    assert (r["status"], r["reason"]) == ("blocked", "tool_missing")
    assert _record(trusted, r)["command"] == {"argv": ["qa-no-such-tool --help"], "shell": True, "cwd": "."}


# --------------------------------------------------------------------------- T14-R4: authorization groups

def test_per_target_activities_live_in_vocab():
    assert qa.vocab.PER_TARGET_ACTIVITIES == ("network-download", "dast-passive", "dast-active")
    assert not hasattr(authz, "_PER_TARGET")


def test_url_host_needs_a_grant_on_its_own_environment(trusted, actor, tmp_path):
    _declare(trusted, actor, STAGING)
    command = execution.RunRequest(command=[_stub(tmp_path), STAGING_URL])
    blocked = execution.execute(trusted, actor, command)
    assert (blocked["status"], blocked["reason"]) == ("blocked", "not_authorized")
    assert any("staging" in text for text in blocked["limitations"])
    grant = authz.grant(trusted, actor, environment_id="staging", targets=[STAGING_HOST],
                        activities=["test-execution-local"], limits={}, approval="ok", expires_at=None)
    allowed = execution.execute(trusted, actor, command)
    assert allowed["status"] == "passed"
    e = _record(trusted, allowed)
    assert e["authorization"] == "trusted:local"
    assert e["authorizations"] == [
        {"activity": "test-execution-local", "environment": "local", "authorization": "trusted:local"},
        {"activity": "test-execution-local", "environment": "staging", "authorization": grant["data"]["id"]},
    ]


def test_per_target_activity_skips_an_environment_no_host_resolves_to(trusted, actor, tmp_path):
    _declare(trusted, actor, STAGING)
    grant = authz.grant(trusted, actor, environment_id="staging", targets=[STAGING_HOST],
                        activities=["network-download"], limits={}, approval="ok", expires_at=None)
    r = execution.execute(trusted, actor, execution.RunRequest(command=[_stub(tmp_path), STAGING_URL], activity="network-download"))
    assert r["status"] == "passed"
    e = _record(trusted, r)
    assert e["authorization"] == grant["data"]["id"]
    assert e["authorizations"] == [{"activity": "network-download", "environment": "staging", "authorization": grant["data"]["id"]}]


def test_per_target_activity_with_no_host_at_all_is_checked_and_denied(trusted, actor, py):
    r = _run(trusted, actor, py, "pass", activity="network-download")
    assert (r["status"], r["reason"]) == ("blocked", "not_authorized")
    assert _record(trusted, r)["authorization"] is None


def test_authorization_is_null_when_the_first_check_denies(trusted, actor, tmp_path):
    _declare(trusted, actor, STAGING)
    r = execution.execute(trusted, actor, execution.RunRequest(command=[_stub(tmp_path), STAGING_URL], activity="db-readonly"))
    e = _record(trusted, r)
    assert e["authorization"] is None and e["authorizations"] == []


def test_authorization_is_null_when_only_a_later_check_allows(trusted, actor, tmp_path):
    _declare(trusted, actor, STAGING)
    grant = authz.grant(trusted, actor, environment_id="staging", targets=[STAGING_HOST], activities=["db-readonly"],
                        limits={}, approval="ok", expires_at=None)
    r = execution.execute(trusted, actor, execution.RunRequest(command=[_stub(tmp_path), STAGING_URL], activity="db-readonly"))
    e = _record(trusted, r)
    assert r["reason"] == "not_authorized" and e["authorization"] is None
    assert e["authorizations"] == [{"activity": "db-readonly", "environment": "staging", "authorization": grant["data"]["id"]}]


def test_an_authorized_activity_is_still_checked_on_every_group(trusted, actor, py):
    r = _run(trusted, actor, py, "pass", STAGING_URL)     # no environment declares this host here
    assert (r["status"], r["reason"]) == ("blocked", "not_authorized")
    assert any("<undeclared>" in text for text in r["limitations"])
    assert _record(trusted, r)["authorization"] == "trusted:local"


def test_groups_keep_the_runs_environment_first_then_first_seen(trusted, actor, py):
    second = {"id": "second", "class": "staging", "base_urls": ["https://second.shop.example"]}
    _declare(trusted, actor, second, STAGING)
    for env, host in (("staging", STAGING_HOST), ("second", "second.shop.example")):
        authz.grant(trusted, actor, environment_id=env, targets=[host], activities=["test-execution-local"],
                    limits={}, approval="ok", expires_at=None)
    r = _run(trusted, actor, py, "pass", "https://second.shop.example/a", STAGING_URL, "https://second.shop.example/b")
    assert [a["environment"] for a in _record(trusted, r)["authorizations"]] == ["local", "second", "staging"]


def test_undeclared_hosts_are_not_covered_by_an_environment_named_production(trusted, actor, py):
    _declare(trusted, actor, {"id": "production", "class": "ephemeral", "ephemeral_basis": "user-declared", "base_urls": []})
    config.set_value(trusted, actor, "standing_authorizations",
                     [{"environment": "production", "activities": ["test-execution-local"]}], APPROVAL)
    r = _run(trusted, actor, py, "pass", "https://example.com/x")
    assert (r["status"], r["reason"]) == ("blocked", "not_authorized")


def test_undeclared_host_counts_as_production_for_never_on_production(trusted, actor, py):
    r = _run(trusted, actor, py, "pass", "https://example.com/x", activity="db-write")
    assert (r["status"], r["reason"]) == ("blocked", "policy_denied")


# --------------------------------------------------------------------------- T14-R5: attempt rules

def _denied_attempt(ws, actor, py, **kw):
    r = execution.execute(ws, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass", **kw))
    assert (r["status"], r["reason"]) == ("blocked", "policy_denied")
    return r


@pytest.mark.parametrize("attempt_of", ["EXE-" + "9" * 26, "not-an-id"])
def test_attempt_of_an_unknown_execution_is_refused_and_kept_as_requested(trusted, actor, py, attempt_of):
    r = _denied_attempt(trusted, actor, py, attempt_of=attempt_of)
    assert _record(trusted, r)["attempt_of"] == attempt_of and r["flaky"] == {} and r["evidence"] is None


def test_attempt_of_a_blocked_execution_is_refused(trusted, actor, py):
    first = execution.execute(trusted, actor, execution.RunRequest(command=["qa-no-such-tool"]))["execution"]
    _denied_attempt(trusted, actor, py, attempt_of=first)


def test_attempt_needs_test_execution_local_on_both_sides(trusted, actor, py):
    other = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass", activity="static-analysis"))["execution"]
    _denied_attempt(trusted, actor, py, attempt_of=other)
    first = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))["execution"]
    _denied_attempt(trusted, actor, py, attempt_of=first, activity="static-analysis")


def test_attempt_needs_the_same_environment(trusted, actor, py):
    _declare(trusted, actor, {"id": "second", "class": "local", "base_urls": []})
    first = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))["execution"]
    _denied_attempt(trusted, actor, py, attempt_of=first, environment="second")


@needs_git
def test_attempt_needs_the_same_diff(repo, actor, py):
    first = execution.execute(repo, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))["execution"]
    (repo.root / "a.txt").write_text("2")
    _denied_attempt(repo, actor, py, attempt_of=first)


@needs_git
def test_attempt_needs_the_same_commit(repo, actor, py):
    first = execution.execute(repo, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))["execution"]
    _git(repo.root, "commit", "-q", "--allow-empty", "-m", "c2")
    _denied_attempt(repo, actor, py, attempt_of=first)


@needs_git
def test_attempt_on_the_same_commit_and_diff_runs(repo, actor, py):
    first = execution.execute(repo, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))["execution"]
    assert execution.execute(repo, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass", attempt_of=first))["status"] == "passed"


def test_two_runs_without_git_count_as_equal_and_keep_their_limitation(trusted, actor, py):
    first = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))
    second = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass", attempt_of=first["execution"]))
    assert second["status"] == "passed"
    assert all("not_a_git_repo" in r["limitations"] for r in (first, second))


def test_chain_length_limit_counts_only_unblocked_attempts(trusted, actor, py):
    config.set_value(trusted, actor, "defaults.flaky_reruns", 1, APPROVAL)
    first = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "fail"))["execution"]
    blocked = execution.execute(trusted, actor, execution.RunRequest(command=["git", "push"], attempt_of=first))
    assert blocked["status"] == "blocked" and _record(trusted, blocked)["attempt_of"] == first
    assert execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass", attempt_of=first))["status"] == "passed"
    _denied_attempt(trusted, actor, py, attempt_of=first)


def test_chain_length_is_counted_from_the_chains_root(trusted, actor, py):
    config.set_value(trusted, actor, "defaults.flaky_reruns", 1, APPROVAL)
    first = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "fail"))["execution"]
    second = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass", attempt_of=first))["execution"]
    _denied_attempt(trusted, actor, py, attempt_of=second)


def test_zero_reruns_allows_no_attempt(trusted, actor, py):
    config.set_value(trusted, actor, "defaults.flaky_reruns", 0, APPROVAL)
    first = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "fail"))["execution"]
    _denied_attempt(trusted, actor, py, attempt_of=first)


def test_chain_root_follows_parents_and_stops_on_a_repeated_id():
    st = {"executions": {"A": {"attempt_of": None}, "B": {"attempt_of": "A"}, "C": {"attempt_of": "B"},
                         "O": {"attempt_of": "gone"}, "X": {"attempt_of": "Y"}, "Y": {"attempt_of": "X"},
                         "S": {"attempt_of": "S"}}}
    assert state.chain_root(st, "C") == "A" and state.chain_root(st, "A") == "A"
    assert state.chain_root(st, "O") == "O" and state.chain_root(st, "S") == "S"
    assert state.chain_root(st, "X") in ("X", "Y") and state.chain_root(st, "Y") in ("X", "Y")


def test_flaky_is_empty_without_attempt_of_and_without_results(trusted, actor, py):
    first = _run(trusted, actor, py, "pass")
    assert first["flaky"] == {}
    second = _run(trusted, actor, py, "pass", attempt_of=first["execution"])
    assert second["status"] == "passed" and second["flaky"] == {}


def test_flaky_is_empty_for_a_run_with_results_and_no_attempt_of(trusted, actor, py):
    r = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "fail"))
    assert r["tests"]["total"] == 1 and r["flaky"] == {}


def test_blocked_attempt_reports_the_view_of_its_chain(trusted, actor, py):
    first = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "fail"))["execution"]
    r = execution.execute(trusted, actor, execution.RunRequest(command=["git", "push"], attempt_of=first))
    assert r["flaky"]["c::t"]["attempts"] == 1


# --------------------------------------------------------------------------- T14-R6: report resolution

def test_stale_match_inside_a_glob_is_not_stored(trusted, actor, py):
    out = trusted.root / "out"; out.mkdir()
    (out / "old.xml").write_text(PASS_XML.replace('name="t"', 'name="old"'))
    r = _run(trusted, actor, py, WRITE_FILES, PASS_XML, "out/new.xml", reports=(("junit", "out/*.xml"),))
    assert r["tests"]["total"] == 1
    assert _record(trusted, r)["reports"] == [{"format": "junit", "pattern": "out/*.xml", "files": ["reports/new.xml"], "error": None}]


def test_glob_with_only_stale_matches_is_report_missing(trusted, actor, py):
    out = trusted.root / "out"; out.mkdir()
    (out / "old.xml").write_text(PASS_XML)
    r = _run(trusted, actor, py, "pass", reports=(("junit", "out/*.xml"),))
    assert (r["status"], r["reason"]) == ("inconclusive", "report_missing")
    assert _record(trusted, r)["reports"][0]["files"] == [] and any("out/*.xml" in t for t in r["limitations"])


def test_rewritten_file_with_the_same_content_counts_by_mtime(trusted, actor, py):
    out = trusted.root / "out"; out.mkdir()
    (out / "r.xml").write_text(PASS_XML)
    os.utime(out / "r.xml", ns=(10**18, 10**18))
    r = _run(trusted, actor, py, WRITE_FILES, PASS_XML, "out/r.xml", reports=(("junit", "out/r.xml"),))
    assert (r["status"], r["strength"]) == ("passed", "structured")


def test_changed_content_with_the_same_mtime_counts_by_hash(trusted, actor, py):
    out = trusted.root / "out"; out.mkdir()
    (out / "r.xml").write_text(PASS_XML)
    code = ("import os, pathlib, sys; p = pathlib.Path(sys.argv[2]); s = p.stat(); p.write_text(sys.argv[1]); "
            "os.utime(str(p), ns=(s.st_atime_ns, s.st_mtime_ns))")
    r = _run(trusted, actor, py, code, PASS_XML.replace('name="t"', 'name="u"'), "out/r.xml", reports=(("junit", "out/r.xml"),))
    assert (r["status"], r["strength"]) == ("passed", "structured") and "c::u" in {x["test_id"] for x in _data(trusted, "result.recorded")}


def test_relative_pattern_is_read_against_the_runs_cwd(trusted, actor, py):
    (trusted.root / "pkg" / "out").mkdir(parents=True)
    r = _run(trusted, actor, py, WRITE_FILES, PASS_XML, "out/a.xml", cwd="pkg", reports=(("junit", "out/*.xml"),))
    assert (r["status"], r["strength"]) == ("passed", "structured")
    assert _record(trusted, r)["reports"][0]["files"] == ["reports/a.xml"]


def test_recursive_glob(trusted, actor, py):
    (trusted.root / "a" / "b").mkdir(parents=True)
    r = _run(trusted, actor, py, WRITE_FILES, PASS_XML, "a/b/r.xml", reports=(("junit", "**/r.xml"),))
    assert (r["status"], r["strength"]) == ("passed", "structured")


def test_pattern_naming_an_existing_path_is_taken_literally(trusted, actor, py):
    out = trusted.root / "out[1]"; out.mkdir()
    r = _run(trusted, actor, py, WRITE_FILES, PASS_XML, "out[1]/r.xml", reports=(("junit", "out[1]/r.xml"),))
    assert (r["status"], r["strength"]) == ("passed", "structured")


def test_repeated_basename_gets_a_suffix_in_sorted_source_order(trusted, actor, py):
    for name in ("b", "a"):
        (trusted.root / name).mkdir()
    other = PASS_XML.replace('name="t"', 'name="u"')
    code = "import pathlib, sys; pathlib.Path('a/junit.xml').write_text(sys.argv[1]); pathlib.Path('b/junit.xml').write_text(sys.argv[2])"
    r = _run(trusted, actor, py, code, PASS_XML, other, reports=(("junit", "*/junit.xml"),))
    copies = _dir(trusted, r) / "reports"
    assert (copies / "junit.xml").read_text() == PASS_XML and (copies / "junit-2.xml").read_text() == other
    assert r["tests"]["total"] == 2
    assert _record(trusted, r)["reports"][0]["files"] == ["reports/junit.xml", "reports/junit-2.xml"]


def test_symlink_is_ignored(trusted, actor, py, tmp_path):
    code = "import os, pathlib, sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[3]); os.symlink(sys.argv[1], sys.argv[2])"
    r = _run(trusted, actor, py, code, str(tmp_path / "real.xml"), "{evidence}/link.xml", PASS_XML, reports=(("junit", "{evidence}/link.xml"),))
    assert (r["status"], r["reason"]) == ("inconclusive", "report_missing")


def test_directory_matching_a_pattern_is_ignored(trusted, actor, py):
    r = _run(trusted, actor, py, "import os, sys; os.mkdir(sys.argv[1])", "{evidence}/d.xml", reports=(("junit", "{evidence}/*.xml"),))
    assert (r["status"], r["reason"]) == ("inconclusive", "report_missing")


def test_report_over_the_cap_is_not_stored_and_is_invalid(trusted, actor, py):
    config.set_value(trusted, actor, "defaults.max_log_mb", 1, APPROVAL)
    code = "import sys; open(sys.argv[1], 'wb').write(b' ' * (2**20 + 1))"
    r = _run(trusted, actor, py, code, "out.xml", reports=(("junit", "out.xml"),))
    assert (r["status"], r["reason"]) == ("inconclusive", "report_invalid")
    assert not (_dir(trusted, r) / "reports" / "out.xml").exists()
    assert _record(trusted, r)["reports"][0] == {"format": "junit", "pattern": "out.xml", "files": [], "error": "report_invalid"}
    assert any("out.xml" in t and "cap" in t for t in r["limitations"])


def test_stored_copy_that_fails_to_parse_is_invalid_and_kept(trusted, actor, py):
    r = _run(trusted, actor, py, WRITE_FILES, "not xml", "{evidence}/bad.xml", reports=(("junit", "{evidence}/bad.xml"),))
    assert (r["status"], r["reason"]) == ("inconclusive", "report_invalid")
    assert _record(trusted, r)["reports"][0] == {"format": "junit", "pattern": "{evidence}/bad.xml", "files": ["reports/bad.xml"], "error": "report_invalid"}
    assert "reports/bad.xml" in _manifest(trusted, r) and any("bad.xml" in t for t in r["limitations"])


@pytest.mark.parametrize("swap, reason", [(False, "report_missing"), (True, "report_invalid")])
def test_first_problem_in_declaration_order_gives_the_reason(trusted, actor, py, swap, reason):
    pairs = [("junit", "{evidence}/absent.xml"), ("junit", "{evidence}/bad.xml")]
    r = _run(trusted, actor, py, WRITE_FILES, "not xml", "{evidence}/bad.xml", reports=tuple(reversed(pairs) if swap else pairs))
    assert (r["status"], r["reason"]) == ("inconclusive", reason)
    assert any("absent.xml" in t for t in r["limitations"]) and any("bad.xml" in t for t in r["limitations"])


def test_summary_merges_the_copies_that_parsed(trusted, actor, py):
    r = _run(trusted, actor, py, WRITE_FILES, PASS_XML, "{evidence}/a.xml", "{evidence}/b.xml", reports=(("junit", "{evidence}/*.xml"),))
    assert r["tests"]["total"] == 2 and _record(trusted, r)["report"]["tests"] == 2


def test_two_test_formats_merge_as_mixed(trusted, actor, py):
    code = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[3]); pathlib.Path(sys.argv[2]).write_text(sys.argv[4])"
    r = _run(trusted, actor, py, code, "{evidence}/a.xml", "{evidence}/b.json", PASS_XML, CTRF,
             reports=(("junit", "{evidence}/a.xml"), ("ctrf", "{evidence}/b.json")))
    assert _record(trusted, r)["report"] == {"format": "mixed", "kind": "tests", "tests": 2, "failures": 0, "errors": 0, "skipped": 0}


def test_workspace_path_holding_glob_characters(tmp_path, actor, py):
    ws = workspace.init_workspace(tmp_path / "My [x] Skills")[0]
    config.record_initialized(ws, actor)
    config.set_value(ws, actor, "project.trust", "trusted", APPROVAL)
    (ws.root / "out").mkdir()
    r = _run(ws, actor, py, WRITE_FILES, PASS_XML, "{evidence}/a.xml", "out/b.xml",
             reports=(("junit", "{evidence}/*.xml"), ("junit", "out/*.xml")))
    assert (r["status"], r["strength"], r["tests"]["total"]) == ("passed", "structured", 2)


def test_no_report_declared_is_exit_code_strength(trusted, actor, py):
    r = _run(trusted, actor, py, "pass")
    assert (r["status"], r["strength"], r["reason"]) == ("passed", "exit_code", None)
    assert _record(trusted, r)["reports"] == [] and _record(trusted, r)["report"] is None


def test_file_matched_by_two_patterns_is_stored_and_counted_once(trusted, actor, py):
    r = _run(trusted, actor, py, WRITE_FILES, PASS_XML, "{evidence}/a.xml", reports=(("junit", "{evidence}/a.xml"), ("junit", "{evidence}/*.xml")))
    assert r["tests"]["total"] == 1 and len(_data(trusted, "result.recorded")) == 1
    assert [e["files"] for e in _record(trusted, r)["reports"]] == [["reports/a.xml"], ["reports/a.xml"]]


def test_a_report_written_into_the_evidence_directory_leaves_only_its_redacted_copy(trusted, actor, py):
    r = _run(trusted, actor, py, WRITE_FILES, PASS_XML, "{evidence}/junit.xml", reports=(("junit", "{evidence}/junit.xml"),))
    assert not (_dir(trusted, r) / "junit.xml").exists() and (_dir(trusted, r) / "reports" / "junit.xml").exists()


def test_a_report_written_straight_into_reports_stays_as_its_own_copy(trusted, actor, py):
    code = "import os, sys; os.makedirs(os.path.dirname(sys.argv[2])); open(sys.argv[2], 'w').write(sys.argv[1])"
    r = _run(trusted, actor, py, code, PASS_XML, "{evidence}/reports/junit.xml", reports=(("junit", "{evidence}/reports/junit.xml"),))
    assert (r["status"], r["strength"]) == ("passed", "structured")
    assert integrity.verify_evidence(trusted, state.load(trusted)) == []


def test_a_file_already_in_reports_keeps_its_name_when_another_shares_it(trusted, actor, py):
    code = ("import os, pathlib, sys\n"
            "for path, text in zip(sys.argv[1:3], sys.argv[3:]):\n"
            "    os.makedirs(os.path.dirname(path), exist_ok=True); pathlib.Path(path).write_text(text)\n")
    other = PASS_XML.replace('name="t"', 'name="u"')
    r = _run(trusted, actor, py, code, "{evidence}/a/junit.xml", "{evidence}/reports/junit.xml", other, PASS_XML,
             reports=(("junit", "{evidence}/a/junit.xml"), ("junit", "{evidence}/reports/junit.xml")))
    copies = _dir(trusted, r) / "reports"
    assert (copies / "junit.xml").read_text() == PASS_XML and (copies / "junit-2.xml").read_text() == other
    assert r["tests"]["total"] == 2 and not (_dir(trusted, r) / "a" / "junit.xml").exists()
    assert integrity.verify_evidence(trusted, state.load(trusted)) == []


def test_a_report_that_cannot_be_stored_is_invalid_not_a_crash(trusted, actor, py):
    r = _run(trusted, actor, py, WRITE_FILES, PASS_XML, "{evidence}/reports", reports=(("junit", "{evidence}/reports"),))
    assert (r["status"], r["reason"]) == ("inconclusive", "report_invalid")
    assert _record(trusted, r)["reports"][0]["files"] == [] and any("could not be stored" in t for t in r["limitations"])
    assert integrity.verify_evidence(trusted, state.load(trusted)) == []


LONE_SURROGATE = [
    ("ctrf", '{"results": {"tests": [{"name": "a\\ud800b", "status": "passed"}]}}'),
    ("sarif", '{"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "x"}}, "results": '
              '[{"ruleId": "r", "level": "error", "message": {"text": "\\ud800"}}]}]}'),
]


@pytest.mark.parametrize("fmt, body", LONE_SURROGATE, ids=["ctrf", "sarif"])
def test_a_report_holding_text_utf8_cannot_store_is_invalid_not_a_crash(trusted, actor, py, fmt, body):
    r = _run(trusted, actor, py, WRITE_FILES, body, "{evidence}/r.out", reports=((fmt, "{evidence}/r.out"),))
    assert (r["status"], r["reason"]) == ("inconclusive", "report_invalid")
    assert _data(trusted, "result.recorded") == [] and integrity.problems(trusted, state.load(trusted)) == []


def test_ingesting_text_utf8_cannot_store_is_refused(trusted, actor, tmp_path, base_run):
    bad = tmp_path / "x.json"
    bad.write_text(LONE_SURROGATE[0][1])
    with pytest.raises(ingest.ReportInvalid) as e:
        execution.ingest_file(trusted, actor, "ctrf", bad, from_execution=base_run["execution"])
    assert e.value.details["detail"] == "not_storable"
    assert not (_dir(trusted, base_run) / "ingested").exists()


@pytest.mark.parametrize("depth", [600, 985, 5000])
def test_a_deeply_nested_report_is_parsed_or_invalid_but_never_a_crash(tmp_path, depth):
    report = tmp_path / "deep.sarif"
    report.write_text('{"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "x"}}, "results": '
                      '[{"ruleId": "r", "level": "note", "message": {"text": "m"}, "partialFingerprints": {"a": %s}}]}]}'
                      % ("[" * depth + "1" + "]" * depth))
    try:
        ingest.parse_report("sarif", report, 2 ** 26)
    except ingest.ReportInvalid:
        pass    # which of the two depends on the interpreter's recursion limit


def test_allow_empty_decides_a_report_with_no_cases(trusted, actor, py):
    arguments = (WRITE_FILES, "<testsuite/>", "{evidence}/e.xml")
    reports = (("junit", "{evidence}/e.xml"),)
    assert _run(trusted, actor, py, *arguments, reports=reports)["reason"] == "no_tests_collected"
    r = _run(trusted, actor, py, *arguments, reports=reports, allow_empty=True)
    assert r["status"] == "passed" and "no_tests_collected" in r["anomalies"]


def test_report_token_in_the_middle_of_an_argument_is_substituted(trusted, actor, py):
    code = "import pathlib, sys; pathlib.Path(sys.argv[1].split('=', 1)[1]).write_text(sys.argv[2])"
    r = _run(trusted, actor, py, code, "--out={evidence}/a.xml", PASS_XML, reports=(("junit", "{evidence}/a.xml"),))
    assert (r["status"], r["strength"]) == ("passed", "structured")
    assert _record(trusted, r)["command"]["argv"][-2] == "--out={evidence}/a.xml"


# --------------------------------------------------------------------------- T14-R7: redaction

def test_secret_only_in_the_runs_environ_never_reaches_the_ledger(trusted, actor, py):
    secret = "run-only-" + "v4lue-42"
    environ = {**os.environ, "QA_RUN_TOKEN": secret}
    r = _run(trusted, actor, py, "import sys; print(sys.argv[1])", secret, environ=environ)
    ledger_text = "".join(p.read_text(encoding="utf-8") for p in trusted.path("ledger").iterdir())
    assert secret not in ledger_text and "[REDACTED:env_value:" in ledger_text
    for name in ("command.json", "stdout.log"):
        text = (_dir(trusted, r) / name).read_text(encoding="utf-8")
        assert secret not in text and "[REDACTED:env_value:" in text
    assert _manifest(trusted, r)["command.json"]["redactions"] == 1


def test_secret_in_stderr_redacted_and_hashed_after(trusted, actor, py):
    r = _run(trusted, actor, py, "import os, sys; sys.stderr.write(os.environ['QA_TEST_OUT'])",
             environ={**os.environ, "QA_TEST_OUT": TOKEN})
    stored = _dir(trusted, r) / "stderr.log"
    assert TOKEN not in stored.read_text() and _manifest(trusted, r)["stderr.log"]["redactions"] == 1
    assert _manifest(trusted, r)["stderr.log"]["sha256"] == canonical.sha256_file(stored)


def test_secret_in_a_report_is_gone_from_the_stored_copy_and_the_hash_covers_it(trusted, actor, py):
    xml = '<testsuite><testcase classname="c" name="t"><system-out>%s</system-out></testcase></testsuite>'
    code = "import os, sys; open(sys.argv[1], 'w').write(sys.argv[2] % os.environ['QA_TEST_OUT'])"
    r = _run(trusted, actor, py, code, "{evidence}/r.xml", xml, environ={**os.environ, "QA_TEST_OUT": TOKEN},
             reports=(("junit", "{evidence}/r.xml"),))
    copy = _dir(trusted, r) / "reports" / "r.xml"
    entry = _manifest(trusted, r)["reports/r.xml"]
    assert TOKEN not in copy.read_text() and entry["redactions"] == 1
    assert entry["sha256"] == canonical.sha256_file(copy) and r["tests"]["total"] == 1
    assert integrity.verify_evidence(trusted, state.load(trusted)) == []
    for path in _dir(trusted, r).rglob("*"):     # the tool's own unredacted file stays out of the store
        assert not path.is_file() or TOKEN.encode() not in path.read_bytes()


def test_event_redactions_sum_the_manifests_files(trusted, actor, py):
    code = "import os; print(os.environ['QA_TEST_OUT']); print(os.environ['QA_TEST_OUT'])"
    r = _run(trusted, actor, py, code, environ={**os.environ, "QA_TEST_OUT": TOKEN})
    manifest = _manifest(trusted, r)
    assert _record(trusted, r)["redactions"] == sum(e["redactions"] for e in manifest.values()) == 2


def test_a_secret_in_a_stored_file_name_reaches_neither_the_manifest_nor_the_ledger(trusted, actor, py):
    secret = "name-only-" + "v4lue-42"
    environ = {**os.environ, "QA_RUN_TOKEN": secret}
    code = "import os, pathlib, sys; pathlib.Path(sys.argv[1] + '/' + os.environ['QA_RUN_TOKEN'] + '.xml').write_text(sys.argv[2])"
    r = _run(trusted, actor, py, code, "{evidence}", PASS_XML, environ=environ, reports=(("junit", "{evidence}/*.xml"),))
    assert secret not in "".join(_manifest(trusted, r)) and r["status"] == "passed"
    assert secret not in trusted.path("ledger", actor.session + ".jsonl").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- T14-R8: tail and directories

def test_environment_error_signature_in_the_last_64_kib(trusted, actor, py):
    code = "import sys; sys.stderr.write('ModuleNotFoundError: x\\n'); sys.stderr.write('y' * 1000); sys.exit(1)"
    r = _run(trusted, actor, py, code)
    assert (r["status"], r["reason"]) == ("blocked", "environment_error")
    assert _record(trusted, r)["classification"] == "heuristic"


def test_signature_before_the_last_64_kib_is_not_seen(trusted, actor, py):
    code = "import sys; sys.stderr.write('ModuleNotFoundError: x\\n'); sys.stderr.write('y' * 70000); sys.exit(1)"
    assert _run(trusted, actor, py, code)["reason"] == "nonzero_exit"


def test_evidence_directories_are_private(trusted, actor, py):
    r = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))
    for path in (_dir(trusted, r), _dir(trusted, r) / "reports"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


# --------------------------------------------------------------------------- T14-R9, T14-R10: stored files

def test_command_json_is_the_request_as_recorded(trusted, actor, py):
    r = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass", timeout_s=30, scope="full",
                                              category="security", allow_empty=True))
    assert json.loads((_dir(trusted, r) / "command.json").read_text()) == {
        "argv": py.argv(WRITE_JUNIT, "{evidence}/junit.xml", "pass"), "shell": False, "cwd": ".",
        "reports": [{"format": "junit", "pattern": "{evidence}/junit.xml"}], "timeout_s": 30,
        "activity": "test-execution-local", "environment": "local", "scope": "full", "category": "security",
        "allow_empty": True, "attempt_of": None}


def test_command_json_records_the_effective_timeout(trusted, actor, py):
    r = _run(trusted, actor, py, "pass")
    assert json.loads((_dir(trusted, r) / "command.json").read_text())["timeout_s"] == 900


def test_shell_mode_records_the_string_as_one_argv_element(trusted, actor):
    r = execution.execute(trusted, actor, execution.RunRequest(command="echo {evidence}", shell=True))
    e = _record(trusted, r)
    assert e["command"] == {"argv": ["echo {evidence}"], "shell": True, "cwd": "."}
    document = json.loads((_dir(trusted, r) / "command.json").read_text())
    assert (document["argv"], document["shell"]) == (["echo {evidence}"], True)


def test_manifest_lists_every_stored_file_except_itself(trusted, actor, py):
    r = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))
    manifest = _manifest(trusted, r)
    assert set(manifest) == {"command.json", "stdout.log", "stderr.log", "reports/junit.xml"}
    assert {manifest[n]["media_type"] for n in ("stdout.log", "stderr.log")} == {"text/plain"}
    assert manifest["command.json"]["media_type"] == "application/json"
    assert manifest["reports/junit.xml"]["media_type"] == "application/xml"
    for name, entry in manifest.items():
        path = _dir(trusted, r) / name
        assert (entry["sha256"], entry["size"]) == (canonical.sha256_file(path), path.stat().st_size)
        assert set(entry) == {"sha256", "size", "media_type", "redactions"}


@pytest.mark.parametrize("fmt, body, media_type", [("ctrf", CTRF, "application/json"), ("sarif", SARIF_CLEAN, "application/sarif+json")])
def test_report_media_types(trusted, actor, py, fmt, body, media_type):
    r = _run(trusted, actor, py, WRITE_FILES, body, "{evidence}/out.txt", reports=((fmt, "{evidence}/out.txt"),))
    assert _manifest(trusted, r)["reports/out.txt"]["media_type"] == media_type


def test_evidence_manifest_names_the_files_and_their_ev_ids(trusted, actor, py):
    r = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))
    manifest = _manifest(trusted, r)
    record = _record(trusted, r)["evidence_manifest"]
    assert record["sha256"] == canonical.sha256_file(_dir(trusted, r) / "manifest.json")
    assert record["files"] == sorted(manifest)
    assert record["ids"] == {name: ids.ev_id(manifest[name]["sha256"]) for name in manifest}


def test_alert_report_shape(trusted, actor, py):
    r = _run(trusted, actor, py, WRITE_FILES, SARIF_ONE, "{evidence}/a.sarif", reports=(("sarif", "{evidence}/a.sarif"),))
    assert _record(trusted, r)["report"] == {"format": "sarif", "kind": "alerts", "alerts": 1, "runs": 1}
    assert r["tests"] == {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0} and r["status"] == "failed"
    assert _data(trusted, "result.recorded") == []


# --------------------------------------------------------------------------- T14-R11: the execution record

def test_execution_record_has_exactly_the_ruled_keys(trusted, actor, py):
    r = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))
    doc = schema.load_schema("events/execution.recorded.schema.json")
    assert doc["additionalProperties"] is False and set(doc["required"]) == set(doc["properties"])
    assert set(_data(trusted, "execution.recorded")[0]) == set(doc["required"])
    assert set(_record(trusted, r)) == set(doc["required"]) | {"ingested"}


def test_execution_record_carries_the_run_facts(trusted, actor, py):
    r = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "fail", timeout_s=30, scope="full",
                                              category="security"))
    e = _record(trusted, r)
    assert (e["status"], e["reason"], e["strength"], e["classification"]) == ("failed", "assertion", "structured", "deterministic")
    assert (e["exit_code"], e["signal"], e["timed_out"], e["isolation"]) == (1, None, False, "host")
    assert (e["scope"], e["category"], e["activity"], e["attempt_of"]) == ("full", "security", "test-execution-local", None)
    assert (e["environment_id"], e["authorization"], e["independence"]) == ("local", "trusted:local", "I1")
    assert e["actor"] == {"role": "verifier", "skill": "qa-test-execution", "session": actor.session}
    assert e["config_hash"] == config.current_hash(trusted) and e["duration_ms"] >= 0
    assert e["kernel"] == {"version": qa.KERNEL_VERSION, "manifest": None}
    assert e["report"] == {"format": "junit", "kind": "tests", "tests": 1, "failures": 1, "errors": 0, "skipped": 0}
    assert e["env_names"] == sorted(os.environ) and "ingested" in e and e["ingested"] == []


def test_signal_is_recorded(trusted, actor, py):
    r = _run(trusted, actor, py, "import os, signal; os.kill(os.getpid(), signal.SIGTERM)")
    e = _record(trusted, r)
    assert (e["exit_code"], e["signal"]) == (None, 15) and r["reason"] == "nonzero_exit"


def test_truncation_and_git_limitations(trusted, actor, py):
    config.set_value(trusted, actor, "defaults.max_log_mb", 1, APPROVAL)
    code = "import sys; sys.stdout.write('x' * (2**20 + 10)); sys.stderr.write('y' * (2**20 + 10))"
    limitations = _record(trusted, _run(trusted, actor, py, code))["limitations"]
    assert {"not_a_git_repo", "stdout_truncated", "stderr_truncated"} <= set(limitations)


def test_kernel_modified_is_a_limitation(trusted, actor, py, monkeypatch):
    report = {"status": "modified", "source_checkout": False, "manifest_sha256": "sha256:" + "a" * 64,
              "modified": ["qa/x.py"], "missing": [], "extra": []}
    monkeypatch.setattr(integrity, "kernel_integrity", lambda kernel_dir=None: report)
    e = _record(trusted, _run(trusted, actor, py, "pass"))
    assert "kernel_modified" in e["limitations"] and e["kernel"] == {"version": qa.KERNEL_VERSION, "manifest": "sha256:" + "a" * 64}


def test_an_intact_kernel_adds_no_limitation(trusted, actor, py):
    assert "kernel_modified" not in _record(trusted, _run(trusted, actor, py, "pass"))["limitations"]


@needs_git
def test_git_facts_are_read_before_the_run(repo, actor, py):
    r = _run(repo, actor, py, "import pathlib; pathlib.Path('a.txt').write_text('changed')")
    git = _record(repo, r)["git"]
    assert git["dirty"] is False and git["branch"] == "main" and git["commit"]
    assert (repo.root / "a.txt").read_text() == "changed"


def test_blocked_records_also_validate_and_fold(trusted, actor):
    execution.execute(trusted, actor, execution.RunRequest(command=["git", "push"], reports=(("junit", "x.xml"),)))
    assert state.load(trusted)["violations"] == []


# --------------------------------------------------------------------------- T14-R12: the other events

JUNIT_THREE = ('<testsuite>'
               '<testcase classname="c" name="a" time="0.5"/>'
               '<testcase classname="c" name="b"><error message="boom"/></testcase>'
               '<testcase classname="c" name="d"><skipped message="later"/></testcase>'
               '</testsuite>')


def test_one_result_event_per_case_as_parsed(trusted, actor, py):
    r = _run(trusted, actor, py, WRITE_FILES, JUNIT_THREE, "{evidence}/j.xml", reports=(("junit", "{evidence}/j.xml"),))
    data = _data(trusted, "result.recorded")
    assert [(d["test_id"], d["status"], d["failure_kind"], d["skip_reason"], d["flaky"], d["duration_ms"]) for d in data] == [
        ("c::a", "passed", None, None, False, 500), ("c::b", "failed", "error", None, False, None),
        ("c::d", "not_run", None, "later", False, None)]
    assert all(d["execution"] == r["execution"] and d["id"].startswith("RES-") for d in data)
    assert r["tests"] == {"total": 3, "passed": 1, "failed": 0, "errors": 1, "skipped": 1}


def test_results_follow_the_execution_event(trusted, actor, py):
    execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))
    order = [e["type"] for e in ledger.iter_all_events(trusted)]
    assert order.index("execution.recorded") < order.index("result.recorded")


@pytest.mark.parametrize("name", ["execution.recorded", "result.recorded", "result.reclassified", "command.blocked", "report.ingested"])
def test_event_schemas_are_closed(name):
    doc = schema.load_schema("events/%s.schema.json" % name)
    assert doc["additionalProperties"] is False and set(doc["required"]) == set(doc["properties"])


# --------------------------------------------------------------------------- T14-R13: state

def test_execution_and_result_get_their_folded_lists():
    st = _fold(_exe(EXE_A), _res(RES_A, EXE_A))
    assert st["executions"][EXE_A]["ingested"] == [] and st["results"][RES_A]["reclassifications"] == []
    assert st["violations"] == [] and set(st) == set(state.empty_state())


def test_reclassification_folded_before_its_result_still_applies(ws, actor, frozen_now):
    frozen_now.set(T0 + timedelta(seconds=10))
    ledger.append(ws, actor, "execution.recorded", _execution_data(EXE_A))
    ledger.append(ws, actor, "result.recorded", _result_data(RES_A, EXE_A))
    frozen_now.set(T0)
    ledger.append(ws, actor, "result.reclassified", _reclassified(RES_A, "blocked"))
    assert ledger.iter_all_events(ws)[0]["type"] == "result.reclassified"
    st = state.load(ws)
    assert st["violations"] == [] and st["results"][RES_A]["status"] == "blocked"


def test_ingest_folded_before_its_execution_still_applies(ws, actor, frozen_now):
    frozen_now.set(T0 + timedelta(seconds=10))
    ledger.append(ws, actor, "execution.recorded", _execution_data(EXE_A))
    frozen_now.set(T0)
    event = ledger.append(ws, actor, "report.ingested", _ingested(EXE_A))
    assert ledger.iter_all_events(ws)[0]["type"] == "report.ingested"
    st = state.load(ws)
    assert st["violations"] == [] and st["executions"][EXE_A]["ingested"] == [dict(_ingested(EXE_A), event=event["id"])]


def test_result_folded_before_its_execution_is_kept_and_counted(ws, actor, frozen_now):
    frozen_now.set(T0 + timedelta(seconds=10))
    ledger.append(ws, actor, "execution.recorded", _execution_data(EXE_A))
    frozen_now.set(T0)
    ledger.append(ws, actor, "result.recorded", _result_data(RES_A, EXE_A))
    st = state.load(ws)
    assert st["violations"] == [] and st["tests"]["c::t"][EXE_A]["attempts"] == 1


def test_reclassification_records_from_to_reason_and_evidence():
    st = _fold(_exe(EXE_A), _res(RES_A, EXE_A), ("result.reclassified", _reclassified(RES_A, "blocked")))
    event_id = "EVT-%026d" % 3
    assert st["results"][RES_A]["status"] == "blocked"
    assert st["results"][RES_A]["reclassifications"] == [
        {"event": event_id, "from": "failed", "to": "blocked", "reason": "why", "evidence": [EXE_A]}]


def test_reclassification_decides_on_the_current_status():
    st = _fold(_exe(EXE_A), _res(RES_A, EXE_A),
               ("result.reclassified", _reclassified(RES_A, "blocked")),
               ("result.reclassified", _reclassified(RES_A, "inconclusive")),
               ("result.reclassified", _reclassified(RES_A, "passed")))
    assert st["results"][RES_A]["status"] == "inconclusive"
    assert [(c["from"], c["to"]) for c in st["results"][RES_A]["reclassifications"]] == [("failed", "blocked"), ("blocked", "inconclusive")]
    assert st["violations"] == [{"event": "EVT-%026d" % 5, "kind": "reclassify_not_allowed"}]


@pytest.mark.parametrize("old, new, allowed", [("passed", "inconclusive", True), ("passed", "failed", False), ("failed", "passed", False), ("not_run", "blocked", True)])
def test_reclassification_limits_hold_in_the_fold(old, new, allowed):
    st = _fold(_exe(EXE_A), _res(RES_A, EXE_A, old), ("result.reclassified", _reclassified(RES_A, new)))
    assert st["results"][RES_A]["status"] == (new if allowed else old)
    assert bool(st["violations"]) is not allowed


def test_violation_kinds():
    st = _fold(_exe(EXE_A, status="failed"), _exe(EXE_A, status="passed"),
               _res(RES_A, EXE_A), _res(RES_A, EXE_A, "passed"),
               _res(RES_B, EXE_B),
               ("report.ingested", _ingested(EXE_B)),
               ("result.reclassified", _reclassified(RES_C, "blocked")))
    assert st["violations"] == [
        {"event": "EVT-%026d" % 2, "kind": "duplicate_id"}, {"event": "EVT-%026d" % 4, "kind": "duplicate_id"},
        {"event": "EVT-%026d" % 5, "kind": "unknown_execution"}, {"event": "EVT-%026d" % 6, "kind": "unknown_execution"},
        {"event": "EVT-%026d" % 7, "kind": "unknown_result"}]
    assert st["executions"][EXE_A]["status"] == "failed" and st["results"][RES_A]["status"] == "failed"
    assert RES_B in st["results"]


def test_blocked_command_entries_carry_their_event():
    data = {"execution": EXE_A, "activity": "test-execution-local", "rule_id": "git-write", "reason": "no",
            "command": {"argv": ["git"], "shell": False, "cwd": "."}}
    assert _fold(("command.blocked", data))["blocked_commands"] == [dict(data, event="EVT-%026d" % 1)]


# --------------------------------------------------------------------------- T14-R14: per-test chain views

def test_tests_entry_has_the_ruled_keys():
    entry = _fold(_exe(EXE_A), _res(RES_A, EXE_A))["tests"]["c::t"][EXE_A]
    assert entry == {"result": RES_A, "execution": EXE_A, "attempts": 1, "passes": 0, "ratio": 0.0, "flaky": False,
                     "status": "failed", "reproducible": False}


def test_repeated_test_id_with_fail_then_pass_reads_failed():
    st = _fold(_exe(EXE_A), _res(RES_A, EXE_A, "failed"), _res(RES_B, EXE_A, "passed"))
    entry = st["tests"]["c::t"][EXE_A]
    assert (entry["attempts"], entry["passes"], entry["status"], entry["flaky"]) == (1, 0, "failed", False)
    assert entry["result"] == RES_B   # the chain's latest result in fold order


def test_repeated_test_id_with_pass_then_not_run_reads_passed():
    entry = _fold(_exe(EXE_A), _res(RES_A, EXE_A, "passed"), _res(RES_B, EXE_A, "not_run"))["tests"]["c::t"][EXE_A]
    assert (entry["attempts"], entry["passes"], entry["status"]) == (1, 1, "passed")


def test_flaky_case_in_a_single_attempt_reads_flaky_and_failed():
    entry = _fold(_exe(EXE_A), _res(RES_A, EXE_A, "passed", flaky=True))["tests"]["c::t"][EXE_A]
    assert (entry["attempts"], entry["passes"], entry["ratio"], entry["flaky"], entry["status"]) == (1, 1, 1.0, True, "failed")


def test_all_not_run_has_no_attempts_and_a_null_ratio():
    entry = _fold(_exe(EXE_A), _res(RES_A, EXE_A, "not_run"))["tests"]["c::t"][EXE_A]
    assert (entry["attempts"], entry["ratio"], entry["status"], entry["reproducible"]) == (0, None, "not_run", False)


def test_a_reclassified_result_counts_as_its_new_status():
    st = _fold(_exe(EXE_A), _res(RES_A, EXE_A, "failed"), ("result.reclassified", _reclassified(RES_A, "blocked")))
    entry = st["tests"]["c::t"][EXE_A]
    assert (entry["attempts"], entry["status"]) == (0, "blocked")


def test_results_are_grouped_by_chain_root():
    st = _fold(_exe(EXE_A), _exe(EXE_B, attempt_of=EXE_A), _exe(EXE_C),
               _res(RES_A, EXE_A), _res(RES_B, EXE_B, "passed"), _res(RES_C, EXE_C, "passed"))
    assert set(st["tests"]["c::t"]) == {EXE_A, EXE_C}
    chain = st["tests"]["c::t"][EXE_A]
    assert (chain["attempts"], chain["passes"], chain["ratio"], chain["flaky"], chain["status"]) == (2, 1, 0.5, True, "failed")
    assert (chain["result"], chain["execution"]) == (RES_B, EXE_B)


def test_two_failing_attempts_are_reproducible():
    st = _fold(_exe(EXE_A), _exe(EXE_B, attempt_of=EXE_A), _res(RES_A, EXE_A), _res(RES_B, EXE_B))
    assert st["tests"]["c::t"][EXE_A]["reproducible"] is True


def test_all_attempts_passing_read_passed():
    st = _fold(_exe(EXE_A), _exe(EXE_B, attempt_of=EXE_A), _res(RES_A, EXE_A, "passed"), _res(RES_B, EXE_B, "passed"))
    entry = st["tests"]["c::t"][EXE_A]
    assert (entry["status"], entry["flaky"], entry["ratio"]) == ("passed", False, 1.0)


def test_result_of_an_unknown_execution_stays_out_of_tests():
    st = _fold(_res(RES_A, EXE_B))
    assert RES_A in st["results"] and st["tests"] == {}


def test_flaky_view_has_the_six_keys_for_any_member_of_the_chain():
    st = _fold(_exe(EXE_A), _exe(EXE_B, attempt_of=EXE_A), _res(RES_A, EXE_A), _res(RES_B, EXE_B, "passed"))
    view = execution.flaky_view(st, EXE_B)
    assert view == execution.flaky_view(st, EXE_A)
    assert set(view["c::t"]) == {"attempts", "passes", "ratio", "flaky", "status", "reproducible"}


# --------------------------------------------------------------------------- T14-R15: reclassify

@pytest.fixture
def failed_result(trusted, actor, py):
    r = execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "fail"))
    return r, next(iter(state.load(trusted)["results"]))


def test_reclassify_returns_event_id_and_status(trusted, actor, failed_result):
    r, result_id = failed_result
    out = execution.reclassify(trusted, actor, result_id, "blocked", "the service was down", [r["execution"]])
    assert set(out) == {"event", "id", "status"} and out["event"].startswith("EVT-")
    assert (out["id"], out["status"]) == (result_id, "blocked")
    assert state.load(trusted)["results"][result_id]["status"] == "blocked"


def test_reclassify_accepts_an_ev_reference(trusted, actor, failed_result):
    r, result_id = failed_result
    ev = _record(trusted, r)["evidence_manifest"]["ids"]["stdout.log"]
    assert execution.reclassify(trusted, actor, result_id, "inconclusive", "bad oracle", [ev])["status"] == "inconclusive"


def test_reclassify_accepts_an_ingested_copys_ev_id(trusted, actor, tmp_path, failed_result):
    r, result_id = failed_result
    other = tmp_path / "x.xml"
    other.write_text(PASS_XML)
    execution.ingest_file(trusted, actor, "junit", other, from_execution=r["execution"])
    ev = _record(trusted, r)["ingested"][0]["ev_id"]
    assert execution.reclassify(trusted, actor, result_id, "not_run", "skipped by hand", [ev])["status"] == "not_run"


@pytest.mark.parametrize("change, why", [
    ({"result_id": "RES-" + "9" * 26}, "unknown_result"),
    ({"new_status": "passed"}, "not_allowed"),
    ({"reason": "   "}, "blank_reason"),
    ({"evidence": []}, "no_evidence"),
    ({"evidence": ["REQ-001"]}, "unknown_reference"),
    ({"evidence": ["EXE-" + "9" * 26]}, "unknown_reference"),
    ({"evidence": ["EV-" + "0" * 12]}, "unknown_reference"),
], ids=["unknown_result", "upgrade", "blank_reason", "no_evidence", "bad_prefix", "unknown_exe", "unknown_ev"])
def test_reclassify_refusals(trusted, actor, failed_result, change, why):
    r, result_id = failed_result
    args = dict({"result_id": result_id, "new_status": "blocked", "reason": "why", "evidence": [r["execution"]]}, **change)
    with pytest.raises(QaError) as e:
        execution.reclassify(trusted, actor, **args)
    assert (e.value.code, e.value.details["why"]) == ("reclassify_refused", why)
    assert state.load(trusted)["results"][result_id]["status"] == "failed"


# --------------------------------------------------------------------------- T14-R16: ingest_file

@pytest.fixture
def base_run(trusted, actor, py):
    return execution.execute(trusted, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))


def test_ingest_unknown_execution_is_refused(trusted, actor, tmp_path):
    with pytest.raises(QaError) as e:
        execution.ingest_file(trusted, actor, "junit", tmp_path / "x.xml", from_execution="EXE-" + "9" * 26)
    assert e.value.code == "unknown_execution"


def test_ingest_bad_file_stores_nothing(trusted, actor, tmp_path, base_run):
    bad = tmp_path / "x.xml"
    bad.write_text("not xml")
    with pytest.raises(ingest.ReportInvalid):
        execution.ingest_file(trusted, actor, "junit", bad, from_execution=base_run["execution"])
    assert not (_dir(trusted, base_run) / "ingested").exists() and _record(trusted, base_run)["ingested"] == []


def test_ingest_applies_the_size_cap(trusted, actor, tmp_path, base_run):
    config.set_value(trusted, actor, "defaults.max_log_mb", 1, APPROVAL)
    big = tmp_path / "big.xml"
    big.write_bytes(b" " * (2**20 + 1))
    with pytest.raises(ingest.ReportInvalid) as e:
        execution.ingest_file(trusted, actor, "junit", big, from_execution=base_run["execution"])
    assert e.value.details["detail"] == "too_large"


def test_ingest_invalid_category_is_refused(trusted, actor, tmp_path, base_run):
    with pytest.raises(QaError) as e:
        execution.ingest_file(trusted, actor, "junit", tmp_path / "x.xml", from_execution=base_run["execution"], category="nope")
    assert e.value.code == "invalid_category"


def test_ingest_returns_the_ruled_keys_and_folds(trusted, actor, tmp_path, base_run):
    other = tmp_path / "x.xml"
    other.write_text(PASS_XML)
    out = execution.ingest_file(trusted, actor, "junit", other, from_execution=base_run["execution"], category="functional")
    assert set(out) == {"event", "execution", "strength", "kind", "total"}
    assert (out["execution"], out["strength"], out["kind"], out["total"]) == (base_run["execution"], "attested", "tests", 1)
    entry = _record(trusted, base_run)["ingested"][0]
    copy = _dir(trusted, base_run) / "ingested" / "x.xml"
    assert entry["event"] == out["event"] and entry["file"] == "ingested/x.xml" and entry["category"] == "functional"
    assert entry["sha256"] == canonical.sha256_file(copy) and entry["ev_id"] == ids.ev_id(entry["sha256"])
    assert stat.S_IMODE(copy.parent.stat().st_mode) == 0o700


def test_ingest_alert_report_counts_alerts(trusted, actor, tmp_path, base_run):
    other = tmp_path / "x.sarif"
    other.write_text(SARIF_ONE)
    out = execution.ingest_file(trusted, actor, "sarif", other, from_execution=base_run["execution"])
    assert (out["kind"], out["total"]) == ("alerts", 1)


def test_ingest_repeated_basename_gets_a_suffix(trusted, actor, tmp_path, base_run):
    other = tmp_path / "x.xml"
    other.write_text(PASS_XML)
    for _ in range(2):
        execution.ingest_file(trusted, actor, "junit", other, from_execution=base_run["execution"])
    assert [e["file"] for e in _record(trusted, base_run)["ingested"]] == ["ingested/x.xml", "ingested/x-2.xml"]


def test_ingested_copy_is_redacted(trusted, actor, tmp_path, base_run):
    other = tmp_path / "x.xml"
    other.write_text('<testsuite><testcase classname="c" name="t"><system-out>%s</system-out></testcase></testsuite>' % TOKEN)
    execution.ingest_file(trusted, actor, "junit", other, from_execution=base_run["execution"])
    assert TOKEN not in (_dir(trusted, base_run) / "ingested" / "x.xml").read_text()


def test_tampered_manifest_reads_attested(trusted, actor, base_run):
    stored = _dir(trusted, base_run) / "reports" / "junit.xml"
    with open(_dir(trusted, base_run) / "manifest.json", "ab") as handle:
        handle.write(b" ")
    assert execution.ingest_file(trusted, actor, "junit", stored, from_execution=base_run["execution"])["strength"] == "attested"


def test_missing_manifest_reads_attested(trusted, actor, base_run):
    stored = _dir(trusted, base_run) / "reports" / "junit.xml"
    (_dir(trusted, base_run) / "manifest.json").unlink()
    assert execution.ingest_file(trusted, actor, "junit", stored, from_execution=base_run["execution"])["strength"] == "attested"


def test_ingest_into_a_blocked_execution_is_attested_and_verifiable(trusted, actor, tmp_path):
    r = execution.execute(trusted, actor, execution.RunRequest(command=["git", "push"]))
    other = tmp_path / "x.xml"
    other.write_text(PASS_XML)
    assert execution.ingest_file(trusted, actor, "junit", other, from_execution=r["execution"])["strength"] == "attested"
    assert stat.S_IMODE(_dir(trusted, r).stat().st_mode) == 0o700
    assert integrity.verify_evidence(trusted, state.load(trusted)) == []
