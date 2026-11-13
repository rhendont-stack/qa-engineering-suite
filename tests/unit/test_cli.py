from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import test_compare
from helpers import LAUNCHER, WRITE_JUNIT, base
from qa import canonical, cli, config, ledger, schema, workspace


# --------------------------------------------------------------------------- the brief's tests

def test_init_is_json_and_idempotent(tmp_path, qa_cli):
    root = tmp_path / "Programación" / "My Skills"; root.mkdir(parents=True)
    code, out = qa_cli("init", cwd=root, workspace=None); assert code == 0 and out["ok"] and out["created"]
    code, out = qa_cli("init", cwd=root, workspace=None); assert code == 0 and not out["created"]
@pytest.mark.parametrize("outcome,code", [("pass", 0), ("fail", 1)])
def test_run_exit_codes(trusted, qa_cli, outcome, code):
    rc, out = qa_cli("run", "--report", "junit:{evidence}/junit.xml", "--", sys.executable, "-c", WRITE_JUNIT, "{evidence}/junit.xml", outcome)
    assert rc == code and out["status"] == {"pass": "passed", "fail": "failed"}[outcome]
def test_run_blocked_timeout_and_skipped(ws, trusted, qa_cli):
    assert qa_cli("run", "--", "git", "push")[0] == 3
    assert qa_cli("run", "--timeout", "1", "--", sys.executable, "-c", "import time; time.sleep(30)")[0] == 4
    skipped = "import sys, pathlib; pathlib.Path(sys.argv[1]).write_text('<testsuite><testcase classname=\"c\" name=\"t\"><skipped/></testcase></testsuite>')"
    assert qa_cli("run", "--report", "junit:{evidence}/s.xml", "--", sys.executable, "-c", skipped, "{evidence}/s.xml")[0] == 5
@pytest.mark.parametrize("cmd,code", [(["pytest", "-q"], 0), (["git", "push"], 3), (["npm", "install"], 3)])
def test_guard_check_exit_codes(ws, qa_cli, cmd, code):
    assert qa_cli("guard", "check", "--", *cmd)[0] == code
def test_gate_without_evidence_is_3(trusted, qa_cli):
    assert qa_cli("--role", "evaluator", "--independence", "I2", "gate", "evaluate", "--profile", "release")[0] == 3
def test_usage_error_is_json(ws, qa_cli):
    code, out = qa_cli("run"); assert code == 2 and out["error"]["code"] == "usage"
def test_run_refuses_independence_i0(trusted, qa_cli):
    assert qa_cli("--independence", "I0", "run", "--", "true")[0] == 2
def test_session_precedence_and_validation(ws, qa_cli):
    s = "S-" + "0" * 25 + "9"
    _, out = qa_cli("--session", s, "ledger", "show", env={"QA_SESSION": "S-" + "0" * 25 + "8"}); assert out["session"] == s
    code, out = qa_cli("--session", "S-../x", "ledger", "show"); assert code == 2 and out["error"]["code"] == "invalid_session"
def test_internal_error_writes_crash_log(ws, monkeypatch, capsys):
    monkeypatch.setattr(cli, "cmd_doctor", lambda args, ctx: 1 / 0)
    assert cli.main(["--workspace", str(ws.root), "doctor"]) == 70
    out = json.loads(capsys.readouterr().out); assert Path(out["error"]["crash_log"]).is_file()
def test_cli_in_non_ascii_path(ws, qa_cli):
    code, out = qa_cli("ledger", "verify"); assert code == 0 and "Programación" in str(ws.root)
def test_newer_schema_refuses_writes_and_flags_reads(ws, qa_cli):
    cfg = json.loads(ws.path("config.json").read_text()); cfg["schema_version"] = "2"; ws.path("config.json").write_bytes(workspace.config_bytes(cfg))
    code, out = qa_cli("state", "rebuild"); assert code == 2 and out["error"]["code"] == "schema_too_new"
    code, out = qa_cli("ledger", "show"); assert code == 0 and "schema_newer" in out["limitations"]
# One round trip per command group that the tests above do not reach: exit codes and JSON keys.
def test_authorize_list_revoke_round_trip(ws, qa_cli):
    code, out = qa_cli("authorize", "--environment", "local", "--activity", "load-test", "--target", "localhost", "--limit", "load-test.max_vus=5",
                       "--limit", "load-test.max_rps=10", "--limit", "load-test.max_duration_s=60", "--approval", "ok for today")
    assert code == 0 and out["id"] == "AUTH-001" and out["event"].startswith("EVT-")
    assert qa_cli("auth", "list")[1]["authorizations"]["AUTH-001"]["limits"] == {"load-test": {"max_vus": 5, "max_rps": 10, "max_duration_s": 60}}
    assert qa_cli("auth", "revoke", "AUTH-001", "--reason", "done")[0] == 0 and qa_cli("auth", "list")[1]["authorizations"]["AUTH-001"]["revoked_at"]
def test_config_show_set_accept_round_trip(ws, actor, qa_cli):
    config.record_initialized(ws, actor)  # not in the brief: the ws fixture accepts no config, and set_value refuses drift (T7)
    assert qa_cli("config", "show")[1]["config"]["project"]["trust"] == "untrusted"
    code, out = qa_cli("config", "set", "project.trust", "trusted", "--approval", "test approval"); assert code == 0 and out["hash"].startswith("sha256:")
    ws.path("config.json").write_bytes(ws.path("config.json").read_bytes() + b" ")
    assert qa_cli("config", "accept", "--approval", "I reviewed the edit")[0] == 0
    shown = qa_cli("config", "show")[1]
    assert shown["config"]["project"]["trust"] == "trusted" and shown["hash"] == shown["accepted_hash"]
def test_gate_policy_propose_accept_round_trip(trusted, qa_cli):
    code, p = qa_cli("gate", "policy", "propose", "suite_executed", "--override", '{"not_applicable": "docs-only change"}', "--rationale", "docs PR")
    assert code == 0 and qa_cli("gate", "policy", "accept", p["event"], "--approval", "yes, docs only")[0] == 0
    assert qa_cli("config", "show")[1]["config"]["gate"]["overrides"]["suite_executed"]["accepted_in"] == p["event"]
def test_ingest_and_reclassify_round_trip(trusted, qa_cli):
    run = qa_cli("run", "--report", "junit:{evidence}/junit.xml", "--", sys.executable, "-c", WRITE_JUNIT, "{evidence}/junit.xml", "fail")[1]
    stored = trusted.path("evidence", run["execution"], "reports", "junit.xml")
    code, out = qa_cli("ingest", "junit", str(stored), "--from", run["execution"])
    assert code == 0 and (out["execution"], out["strength"], out["kind"], out["total"]) == (run["execution"], "structured", "tests", 1)
    res = next(iter(qa_cli("state", "show", "results")[1]["state"]))
    assert qa_cli("result", "reclassify", res, "blocked", "--reason", "env was down", "--evidence", run["execution"])[1]["id"] == res
    assert qa_cli("result", "reclassify", res, "passed", "--reason", "looks fine", "--evidence", run["execution"])[0] == 2
def test_finding_round_trip(ws, qa_cli, tmp_path):
    (tmp_path / "f.json").write_text(json.dumps(base())); (tmp_path / "u.json").write_text(json.dumps({"title": "IDOR on GET /orders/{id}"}))
    code, out = qa_cli("finding", "add", "--json", str(tmp_path / "f.json")); assert code == 0 and (out["id"], out["created"]) == ("FND-001", True)
    assert qa_cli("finding", "update", "FND-001", "--json", str(tmp_path / "u.json"))[0] == 0
    assert qa_cli("finding", "state", "FND-001", "wont_fix", "--reason", "endpoint removed next release")[1]["state"] == "wont_fix"
    f = qa_cli("finding", "list")[1]["findings"]["FND-001"]
    assert (f["title"], f["state"]) == ("IDOR on GET /orders/{id}", "wont_fix")
def test_rubric_commands(ws, qa_cli):
    code, out = qa_cli("rubric", "severity"); assert code == 0 and len(out["rubric"]["anchors"]) == 28
    code, out = qa_cli("rubric", "priority"); assert code == 0 and "matrix" in out
def test_state_rebuild_and_show(trusted, qa_cli):
    code, out = qa_cli("state", "rebuild"); assert code == 0 and "summary" in out and trusted.path("state", "summary.json").is_file()
    assert qa_cli("state", "show", "config")[1]["state"]["accepted_hash"].startswith("sha256:")
def test_report_render_and_check(trusted, qa_cli, tmp_path):
    qa_cli("run", "--report", "junit:{evidence}/junit.xml", "--", sys.executable, "-c", WRITE_JUNIT, "{evidence}/junit.xml", "pass")
    code, out = qa_cli("report", "render"); assert code == 0 and out["id"] == "RPT-001"
    code, out = qa_cli("report", "check", str(trusted.path("reports", "RPT-001", "report.md"))); assert (code, out["problems"]) == (0, [])
    (tmp_path / "bad.md").write_text("All tests passed.\n")
    code, out = qa_cli("report", "check", str(tmp_path / "bad.md")); assert code == 1 and out["problems"]


# --------------------------------------------------------------------------- run and guard check (T18-R1 to R4)

PRINT_CWD = "import os; print(os.getcwd())"


def _executions(qa_cli):
    return qa_cli("state", "show", "executions")[1]["state"]


def _stdout_log(ws, out):
    return ws.path("evidence", out["execution"], "stdout.log").read_text(encoding="utf-8").strip()


def test_run_started_in_a_subdirectory_runs_there(trusted, qa_cli):  # T18-R1
    sub = trusted.root / "pkg" / "inner"
    sub.mkdir(parents=True)
    code, out = qa_cli("run", "--", sys.executable, "-c", PRINT_CWD, cwd=sub)
    assert code == 0 and _executions(qa_cli)[out["execution"]]["command"]["cwd"] == "pkg/inner"
    assert _stdout_log(trusted, out) == os.path.realpath(str(sub))


def test_run_started_outside_the_workspace_runs_at_its_root(trusted, qa_cli, tmp_path):  # T18-R1
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    code, out = qa_cli("run", "--", sys.executable, "-c", PRINT_CWD, cwd=outside)
    assert code == 0 and _executions(qa_cli)[out["execution"]]["command"]["cwd"] == "."
    assert _stdout_log(trusted, out) == os.path.realpath(str(trusted.root))


def test_guard_check_reads_relative_paths_from_where_it_started(ws, qa_cli, tmp_path):  # T18-R1: the answer run would get
    sub, outside = ws.root / "sub", tmp_path / "elsewhere"
    sub.mkdir()
    outside.mkdir()
    remove = ("guard", "check", "--", "rm", "-rf", "../.qa/work/old")
    assert qa_cli(*remove, cwd=sub)[1]["decision"] == "allow"
    assert qa_cli(*remove, cwd=outside)[1]["decision"] == "deny"


@pytest.mark.parametrize("command", [("run",), ("guard", "check")], ids=["run", "guard"])
@pytest.mark.parametrize("words", [(), ("echo a", "echo b")], ids=["zero", "two"])
def test_shell_needs_exactly_one_argument(trusted, qa_cli, command, words):  # T18-R2
    code, out = qa_cli(*command, "--shell", "--", *words)
    assert (code, out["error"]["code"]) == (2, "usage")


def test_run_shell_with_one_argument_keeps_its_quoting(trusted, qa_cli):  # T18-R2
    text = "printf '%s\\n' 'a  b'"
    code, out = qa_cli("run", "--shell", "--", text)
    recorded = _executions(qa_cli)[out["execution"]]["command"]
    assert code == 0 and (recorded["argv"], recorded["shell"]) == ([text], True) and _stdout_log(trusted, out) == "a  b"


def test_guard_check_shell_with_one_argument(ws, qa_cli):  # T18-R2
    code, out = qa_cli("guard", "check", "--shell", "--", "pytest -q && git push")
    assert code == 3 and out["decision"] == "deny"


def test_a_shell_string_without_the_flag_stays_one_argument(ws, qa_cli):  # F7: corpus row shell-no-flag-argv
    code, out = qa_cli("guard", "check", "--", "pytest -q && git push")
    assert code == 3 and (out["decision"], out["rule_id"]) == ("deny", "shell-without-flag")


def test_everything_after_the_separator_belongs_to_the_command(trusted, qa_cli):
    show_argv = "import sys; print(sys.argv[1:])"
    code, out = qa_cli("run", "--", sys.executable, "-c", show_argv, "--workspace", "--", "-h", "--report")
    assert code == 0 and _stdout_log(trusted, out) == "['--workspace', '--', '-h', '--report']"


def test_run_output_has_the_briefs_keys_and_limitations(trusted, qa_cli):  # T18-R3
    code, out = qa_cli("run", "--", "git", "push")
    assert code == 3 and set(out) == {"ok", "command", "session", "execution", "status", "reason", "strength", "anomalies", "tests", "flaky",
                                      "evidence", "findings", "limitations"}
    assert out["limitations"] and out["limitations"] == _executions(qa_cli)[out["execution"]]["limitations"]


@pytest.mark.parametrize("report", ["junit", ":x.xml", "junit:"])
def test_report_needs_both_sides_of_its_colon(trusted, qa_cli, report):  # T18-R4
    code, out = qa_cli("run", "--report", report, "--", "true")
    assert (code, out["error"]["code"]) == (2, "usage")


def test_report_splits_at_the_first_colon(trusted, qa_cli):  # T18-R4
    pattern = "{evidence}/a:b.xml"
    code, out = qa_cli("run", "--report", "junit:" + pattern, "--", sys.executable, "-c", WRITE_JUNIT, pattern, "pass")
    assert code == 0 and _executions(qa_cli)[out["execution"]]["reports"][0]["pattern"] == pattern


@pytest.mark.parametrize("limit", ["load-test", "load-test.max_vus", "max_vus=5", ".max_vus=5", "load-test.=5"])
def test_limit_needs_an_activity_a_key_and_a_value(ws, qa_cli, limit):  # T18-R4
    code, out = qa_cli("authorize", "--environment", "local", "--activity", "load-test", "--limit", limit, "--approval", "ok")
    assert (code, out["error"]["code"]) == (2, "usage")


def test_limit_value_is_json_when_it_parses_and_text_otherwise(ws, qa_cli):
    code, out = qa_cli("authorize", "--environment", "local", "--activity", "data-egress", "write-tests",
                       "--limit", "data-egress.service=a=b", "--limit", "data-egress.data=logs",
                       "--limit", 'write-tests.paths=["tests/**"]', "--approval", "ok")
    assert code == 0
    assert qa_cli("auth", "list")[1]["authorizations"][out["id"]]["limits"] == {
        "data-egress": {"service": "a=b", "data": "logs"}, "write-tests": {"paths": ["tests/**"]}}


# --------------------------------------------------------------------------- ingest (T18-R5)

REPORTS = {
    "junit": '<testsuite><testcase classname="c" name="t"/></testsuite>',
    "ctrf": '{"results": {"tests": [{"name": "t", "status": "passed", "duration": 1}]}}',
    "sarif": '{"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "scanner"}}, "results": []}]}',
    "mutation": '{"schemaVersion": "2", "files": {"a.js": {"language": "javascript", "mutants": [{"id": "1", "mutatorName": "m", "status": "Killed", "location": {"start": {"line": 1, "column": 1}}}]}}}',
    "k6": '{"metrics": {"m": {"type": "trend", "values": {"avg": 1}, "thresholds": {"avg<2": {"ok": true}}}}}',
}


@pytest.fixture
def executed(trusted, qa_cli):
    """A recorded execution to attach reports to."""
    return qa_cli("run", "--", sys.executable, "-c", "pass")[1]["execution"]


def _report_file(tmp_path, fmt):
    path = tmp_path / ("report." + fmt)
    path.write_text(REPORTS[fmt])
    return str(path)


@pytest.mark.parametrize("fmt,category", [("junit", "functional"), ("ctrf", "functional"), ("sarif", "security"), ("mutation", "functional"),
                                          ("k6", "performance")])
def test_ingest_category_defaults_by_format(executed, qa_cli, tmp_path, fmt, category):  # T18-R5
    code, out = qa_cli("ingest", fmt, _report_file(tmp_path, fmt), "--from", executed)
    assert code == 0 and out["event"].startswith("EVT-")
    assert [item["category"] for item in _executions(qa_cli)[executed]["ingested"]] == [category]


def test_ingest_takes_a_category(executed, qa_cli, tmp_path):  # T18-R5
    assert qa_cli("ingest", "junit", _report_file(tmp_path, "junit"), "--from", executed, "--category", "performance")[0] == 0
    assert _executions(qa_cli)[executed]["ingested"][0]["category"] == "performance"


def test_ingest_refuses_an_unknown_category(executed, qa_cli, tmp_path):  # T18-R5
    code, out = qa_cli("ingest", "junit", _report_file(tmp_path, "junit"), "--from", executed, "--category", "made-up")
    assert (code, out["error"]["code"]) == (2, "invalid_category")


def _unreadable(tmp_path, make):
    """A path that cannot be read as a file. No file modes: Task 20 runs as root, where they do not bind (T18-R17)."""
    path = tmp_path / "input"
    if make == "directory":
        path.mkdir()
    elif make == "fifo":
        os.mkfifo(str(path))
    return str(path)


UNREADABLE = ("missing", "directory", "fifo")


@pytest.mark.parametrize("make", UNREADABLE)
def test_ingest_of_an_unreadable_path_is_report_invalid(executed, trusted, tmp_path, make):
    code, data = _raw(trusted, "ingest", "junit", _unreadable(tmp_path, make), "--from", executed)
    error = json.loads(data)["error"]
    assert (code, error["code"], error["details"]["detail"]) == (2, "report_invalid", "unreadable")


# --------------------------------------------------------------------------- output (T18-R6, T18-R7)

def _raw(ws, *args, env=None, timeout=30):
    """The launcher's exit code and the bytes it wrote to stdout; ``timeout`` makes a FIFO fail instead of hanging."""
    done = subprocess.run([sys.executable, LAUNCHER, "--workspace", ws.root, *args], env=dict(os.environ, **(env or {})),
                          capture_output=True, timeout=timeout)
    return done.returncode, done.stdout


def test_stdout_is_one_line_of_canonical_json(ws):  # T18-R6
    code, data = _raw(ws, "ledger", "show")
    assert code == 0 and data.count(b"\n") == 1 and data.endswith(b"\n") and data[:-1] == canonical.dumps(json.loads(data))


@pytest.mark.parametrize("env", [{"LC_ALL": "C"}, {"PYTHONIOENCODING": "ascii"}, {"LC_ALL": "C", "PYTHONIOENCODING": "ascii"}],
                         ids=["c-locale", "ascii-stdio", "both"])
def test_non_ascii_data_prints_whatever_the_locale(ws, qa_cli, env):  # T18-R6
    quote = "¿Apruebas la traducción?"
    assert qa_cli("authorize", "--environment", "local", "--activity", "db-ephemeral", "--approval", quote)[0] == 0
    code, data = _raw(ws, "auth", "list", env=env)
    assert code == 0 and quote.encode("utf-8") in data


def test_a_surrogate_echoed_in_an_error_still_prints(ws, qa_cli):  # T18-R6
    code, out = qa_cli("--session", b"S-\xff", "ledger", "show")
    assert (code, out["error"]["code"], out["error"]["details"]["session"]) == (2, "invalid_session", "S-\udcff")


@pytest.mark.parametrize("argv,mentions", [(("--help",), "--workspace"), (("-h",), "--workspace"), (("run", "--help"), "--report"),
                                           (("gate", "-h"), "policy"), (("gate", "policy", "propose", "-h"), "--override")],
                         ids=["top-long", "top-short", "command", "group", "nested"])
def test_help_is_one_json_object_at_any_level(qa_cli, tmp_path, argv, mentions):  # T18-R7
    code, out = qa_cli(*argv, workspace=None, cwd=tmp_path)
    assert code == 0 and set(out) == {"ok", "command", "help"} and (out["ok"], out["command"]) == (True, "help") and mentions in out["help"]


def test_help_after_the_separator_belongs_to_the_command(ws, qa_cli):  # T18-R7
    code, out = qa_cli("guard", "check", "--", "pytest", "-h")
    assert code == 0 and out["decision"] == "allow"


def test_help_carries_no_terminal_colors(qa_cli, tmp_path):  # T18-R7: newer interpreters color it by default
    assert "\x1b" not in qa_cli("run", "--help", workspace=None, cwd=tmp_path, env={"FORCE_COLOR": "1"})[1]["help"]


# --------------------------------------------------------------------------- input errors (T18-R8)

modes_bind = pytest.mark.skipif(getattr(os, "geteuid", lambda: 0)() == 0, reason="file modes do not bind this user")  # T18-R17


@pytest.mark.parametrize("argv", [
    (), ("nonsense",), ("gate",), ("gate", "policy"), ("--no-such-option", "doctor"), ("ledger", "show", "--no-such-option"),
    ("ledger", "show", "extra"), ("--worksp", "x", "doctor"), ("config", "set", "only-a-key", "--approval", "q"), ("auth", "revoke", "AUTH-001"),
    ("authorize", "--environment", "local", "--approval", "q"), ("ingest", "xml", "f", "--from", "EXE-1"), ("ingest", "junit", "f"),
    ("report", "render", "--format", "pdf"), ("run", "--scope", "wide", "--", "true"), ("run", "--timeout", "soon", "--", "true"),
    ("run", "--timeout", "nan", "--", "true"), ("run", "--timeout", "inf", "--", "true"), ("run", "--timeou", "5", "--", "true"),
    ("run", "--"), ("run", "true"), ("guard", "check"), ("--role", "wizard", "ledger", "show"), ("--independence", "I9", "ledger", "show"),
    ("--", "doctor"), ("gate", "--", "evaluate"), ("--workspace", ".", "--", "doctor"),
], ids=" ".join)
def test_every_malformed_command_line_is_usage(ws, qa_cli, argv):  # T18-R8, on both interpreters
    code, out = qa_cli(*argv)
    assert (code, out["ok"], out["error"]["code"]) == (2, False, "usage")


def test_role_and_independence_take_every_value_of_their_enums(ws, qa_cli):  # T18-R8
    defs = schema.load_schema("defs.schema.json")["$defs"]
    for option, name in (("--role", "role"), ("--independence", "independence")):
        for value in defs[name]["enum"]:
            assert qa_cli(option, value, "ledger", "show")[0] == 0


FINDING_COMMANDS = pytest.mark.parametrize("command", [("finding", "add"), ("finding", "update", "FND-001")], ids=["add", "update"])


@FINDING_COMMANDS
@pytest.mark.parametrize("make", UNREADABLE)
def test_a_json_file_that_cannot_be_read_is_file_unreadable(ws, tmp_path, command, make):  # T18-R8
    code, data = _raw(ws, *command, "--json", _unreadable(tmp_path, make))
    assert (code, json.loads(data)["error"]["code"]) == (2, "file_unreadable")


@pytest.mark.parametrize("make", UNREADABLE)
def test_a_report_to_check_that_cannot_be_read_is_file_unreadable(ws, tmp_path, make):  # T18-R8
    code, data = _raw(ws, "report", "check", _unreadable(tmp_path, make))
    assert (code, json.loads(data)["error"]["code"]) == (2, "file_unreadable")


def test_a_report_to_check_that_is_not_utf8_is_file_unreadable(ws, qa_cli, tmp_path):  # T18-R8
    (tmp_path / "report.md").write_bytes(b"caf\xe9\n")
    code, out = qa_cli("report", "check", str(tmp_path / "report.md"))
    assert (code, out["error"]["code"]) == (2, "file_unreadable")


@FINDING_COMMANDS
@pytest.mark.parametrize("content", [b"{not json", b"caf\xe9", b"[1, 2]", b'"text"', b"3"], ids=["malformed", "not-utf8", "list", "string", "number"])
def test_a_json_file_must_hold_an_object(ws, qa_cli, tmp_path, command, content):  # T18-R8
    (tmp_path / "input.json").write_bytes(content)
    code, out = qa_cli(*command, "--json", str(tmp_path / "input.json"))
    assert (code, out["error"]["code"]) == (2, "invalid_json")


@pytest.mark.parametrize("override", ["{not json", "[1]"], ids=["malformed", "not-an-object"])
def test_an_override_must_be_a_json_object(ws, qa_cli, override):  # T18-R8
    code, out = qa_cli("gate", "policy", "propose", "suite_executed", "--override", override, "--rationale", "r")
    assert (code, out["error"]["code"]) == (2, "invalid_json")


def test_expires_that_does_not_parse_is_invalid_timestamp(ws, qa_cli):  # T18-R8
    code, out = qa_cli("authorize", "--environment", "local", "--activity", "db-ephemeral", "--approval", "q", "--expires", "tomorrow")
    assert (code, out["error"]["code"]) == (2, "invalid_timestamp")


def test_state_show_of_an_unknown_key_is_unknown_key(ws, qa_cli):  # T18-R8
    code, out = qa_cli("state", "show", "no-such-key")
    assert (code, out["error"]["code"]) == (2, "unknown_key")


def test_run_with_i0_is_independence_too_low(trusted, qa_cli):  # T18-R8
    code, out = qa_cli("--independence", "I0", "run", "--", "true")
    assert (code, out["error"]["code"]) == (2, "independence_too_low")


@pytest.mark.parametrize("given", [{"args": ("--session", "bad")}, {"env": {"QA_SESSION": "bad"}}, {"env": {"QA_SESSION": ""}}],
                         ids=["option", "environment", "empty-environment"])
def test_an_invalid_session_given_to_the_command_is_invalid_session(ws, qa_cli, given):  # T18-R8
    code, out = qa_cli(*given.get("args", ()), "ledger", "show", env=given.get("env"))
    assert (code, out["error"]["code"]) == (2, "invalid_session")


@pytest.mark.parametrize("broken", ["malformed", "not-utf8", "missing", "directory", "fifo"])
def test_an_invalid_default_session_is_invalid_session(ws, monkeypatch, broken):  # T18-R8
    monkeypatch.delenv("QA_SESSION", raising=False)
    path = ws.path("default-session")
    path.unlink()
    if broken == "malformed":
        path.write_bytes(b"nope\n")
    elif broken == "not-utf8":
        path.write_bytes(b"\xff\xfe")
    elif broken == "directory":
        path.mkdir()
    elif broken == "fifo":
        os.mkfifo(str(path))
    code, data = _raw(ws, "ledger", "show")
    assert (code, json.loads(data)["error"]["code"]) == (2, "invalid_session")


def test_init_with_an_invalid_session_creates_nothing(qa_cli, tmp_path):  # T18-R8
    code, out = qa_cli("--session", "bad", "init", cwd=tmp_path, workspace=None)
    assert (code, out["error"]["code"]) == (2, "invalid_session") and not (tmp_path / ".qa").exists()


def test_init_where_no_directory_can_be_made_is_refused(qa_cli, tmp_path):
    (tmp_path / "a-file").write_text("x")
    code, out = qa_cli("init", workspace=tmp_path / "a-file" / "below")
    assert (code, out["error"]["code"]) == (2, "invalid_workspace")


def test_a_working_directory_that_is_gone_is_no_workspace(tmp_path, monkeypatch, capsys):
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    assert cli.main(["ledger", "show"]) == 2 and json.loads(capsys.readouterr().out)["error"]["code"] == "no_workspace"


@modes_bind
def test_a_config_this_user_cannot_read_is_config_invalid(ws, qa_cli):
    ws.path("config.json").chmod(0)
    code, out = qa_cli("ledger", "show")
    assert (code, out["error"]["code"]) == (2, "config_invalid")


@modes_bind
def test_ingest_of_a_file_this_user_cannot_read_is_report_invalid(executed, qa_cli, tmp_path):
    path = Path(_report_file(tmp_path, "junit"))
    path.chmod(0)
    code, out = qa_cli("ingest", "junit", str(path), "--from", executed)
    assert (code, out["error"]["code"], out["error"]["details"]["detail"]) == (2, "report_invalid", "unreadable")


def test_text_the_ledger_cannot_store_is_an_input_error(ws, qa_cli):
    code, out = qa_cli("authorize", "--environment", "local", "--activity", "db-ephemeral", "--approval", b"caf\xe9")
    assert (code, out["error"]["code"]) == (2, "not_encodable")


def test_a_run_does_not_start_with_a_skill_the_ledger_cannot_store(trusted, qa_cli):
    marker = trusted.root / "ran"
    write = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('x')"
    code, out = qa_cli("--skill", b"caf\xe9", "run", "--", sys.executable, "-c", write, str(marker))
    assert (code, out["error"]["code"]) == (2, "not_encodable") and not marker.exists()


# --------------------------------------------------------------------------- the crash path (T18-R9)

def _crash(monkeypatch, capsys, name, argv):
    def explode(*args, **kwargs):
        raise RuntimeError("password=hunter22")
    monkeypatch.setattr(cli, name, explode)
    code = cli.main(argv)
    return code, capsys.readouterr().out


def test_a_crash_is_one_json_object_and_its_log_is_redacted(ws, monkeypatch, capsys):  # T18-R9
    code, text = _crash(monkeypatch, capsys, "cmd_doctor", ["--workspace", str(ws.root), "doctor"])
    out = json.loads(text)
    assert code == 70 and text.count("\n") == 1 and set(out) == {"ok", "error"} and out["ok"] is False
    assert set(out["error"]) == {"code", "crash_log"} and out["error"]["code"] == "internal"
    log = Path(out["error"]["crash_log"]).read_text(encoding="utf-8")
    assert "RuntimeError" in log and "hunter22" not in log


def test_a_crash_without_a_workspace_has_no_log(monkeypatch, capsys, tmp_path):  # T18-R9
    monkeypatch.chdir(tmp_path)
    code, text = _crash(monkeypatch, capsys, "build_parser", ["doctor"])
    assert code == 70 and json.loads(text) == {"ok": False, "error": {"code": "internal", "crash_log": None}}


@pytest.mark.parametrize("gone", ["work", "redaction.key"])
def test_a_crash_whose_redacted_log_cannot_be_written_has_no_log(ws, monkeypatch, capsys, gone):  # T18-R9
    path = ws.path(gone)
    path.rmdir() if path.is_dir() else path.unlink()
    code, text = _crash(monkeypatch, capsys, "cmd_doctor", ["--workspace", str(ws.root), "doctor"])
    assert code == 70 and json.loads(text) == {"ok": False, "error": {"code": "internal", "crash_log": None}}


# --------------------------------------------------------------------------- the exit-code table

def _nothing_left_to_show(qa_cli):
    """A change gate needs nothing more once the suite is declared not applicable, with the user's approval."""
    event = qa_cli("gate", "policy", "propose", "suite_executed", "--override", '{"not_applicable": "docs-only change"}', "--rationale", "docs PR")[1]["event"]
    assert qa_cli("gate", "policy", "accept", event, "--approval", "yes, docs only")[0] == 0


def test_gate_ready_is_0(trusted, qa_cli):
    _nothing_left_to_show(qa_cli)
    code, out = qa_cli("gate", "evaluate", "--profile", "change")
    assert (code, out["verdict"], out["ok"]) == (0, "READY", True)


def test_gate_ready_with_conditions_is_6(trusted, qa_cli):
    _nothing_left_to_show(qa_cli)
    code, out = qa_cli("--independence", "I0", "gate", "evaluate", "--profile", "change")
    assert (code, out["verdict"], out["ok"]) == (6, "READY_WITH_CONDITIONS", False)


def test_gate_not_ready_is_1(trusted, qa_cli, tmp_path):
    _nothing_left_to_show(qa_cli)
    (tmp_path / "f.json").write_text(json.dumps(base()))
    assert qa_cli("finding", "add", "--json", str(tmp_path / "f.json"))[0] == 0
    code, out = qa_cli("gate", "evaluate", "--profile", "change")
    assert (code, out["verdict"], out["ok"]) == (1, "NOT_READY", False)


@pytest.mark.parametrize("outcome,code,ok", [("pass", 0, True), ("fail", 1, False)])
def test_ok_is_true_exactly_when_the_exit_code_is_0(trusted, qa_cli, outcome, code, ok):
    rc, out = qa_cli("run", "--report", "junit:{evidence}/junit.xml", "--", sys.executable, "-c", WRITE_JUNIT, "{evidence}/junit.xml", outcome)
    assert (rc, out["ok"]) == (code, ok)


def test_a_refused_guard_check_is_not_ok(ws, qa_cli):
    code, out = qa_cli("guard", "check", "--", "git", "push")
    assert (code, out["ok"]) == (3, False)


# --------------------------------------------------------------------------- the commands (T18-R10 to R13, R16)

WRITE_ARGV = {
    "authorize": ("authorize", "--environment", "local", "--activity", "db-ephemeral", "--approval", "q"),
    "auth revoke": ("auth", "revoke", "AUTH-001", "--reason", "r"),
    "config set": ("config", "set", "project.trust", "trusted", "--approval", "q"),
    "config accept": ("config", "accept", "--approval", "q"),
    "gate policy propose": ("gate", "policy", "propose", "suite_executed", "--override", "{}", "--rationale", "r"),
    "gate policy accept": ("gate", "policy", "accept", "EVT-X", "--approval", "q"),
    "gate evaluate": ("gate", "evaluate", "--profile", "change"),
    "worktree add": ("worktree", "add", "HEAD"),
    "worktree remove": ("worktree", "remove", ".qa/work/wt-000000000000"),
    "run": ("run", "--", "true"),
    "ingest": ("ingest", "junit", "missing.xml", "--from", "EXE-X"),
    "result reclassify": ("result", "reclassify", "RES-X", "blocked", "--reason", "r", "--evidence", "EXE-X"),
    "finding add": ("finding", "add", "--json", "missing.json"),
    "finding update": ("finding", "update", "FND-001", "--json", "missing.json"),
    "finding state": ("finding", "state", "FND-001", "wont_fix", "--reason", "r"),
    "finding dedupe": ("finding", "dedupe"),
    "state rebuild": ("state", "rebuild"),
    "report render": ("report", "render"),
    "report bundle": ("report", "bundle", "--output", "x.tar.gz"),
    "plan create": ("plan", "create", "--template", "missing.json"),
    "plan update": ("plan", "update", "PLAN-0001", "S01", "--status", "blocked", "--reason", "r"),
    "req add": ("req", "add", "--json", "missing.json"),
    "req import": ("req", "import", "--json", "missing.json"),
    "req confirm": ("req", "confirm", "REQ-001", "--approval", "q"),
    "ac add": ("ac", "add", "--json", "missing.json"),
    "asm add": ("asm", "add", "--json", "missing.json"),
    "asm decide": ("asm", "decide", "ASM-001", "confirmed", "--approval", "q"),
    "risk add": ("risk", "add", "--json", "missing.json"),
    "tc add": ("tc", "add", "--json", "missing.json"),
    "tc import": ("tc", "import", "--json", "missing.json"),
    "trace link": ("trace", "link", "AC-001.1", "verified_by", "TC-001"),
    "trace scan": ("trace", "scan"),
    "trace matrix": ("trace", "matrix"),
    "fix add": ("fix", "add", "--json", "missing.json"),
    "changes record": ("changes", "record", "tests/x.py", "--kind", "created"),
    "revalidate": ("revalidate", "FIX-001", "--f2p", "EXE-X:EXE-Y", "--p2p", "EXE-X:EXE-Y", "--defect-test", "t.py::test_x"),
}
READ_ARGV = {
    "doctor": ("doctor",),
    "guard check": ("guard", "check", "--", "pytest", "-q"),
    "auth list": ("auth", "list"),
    "config show": ("config", "show"),
    "finding list": ("finding", "list"),
    "rubric severity": ("rubric", "severity"),
    "rubric priority": ("rubric", "priority"),
    "ledger verify": ("ledger", "verify"),
    "ledger show": ("ledger", "show"),
    "state show": ("state", "show"),
    "report check": ("report", "check", "FILE"),
    "detect stack": ("detect", "stack"),
    "detect tests": ("detect", "tests"),
    "plan status": ("plan", "status"),
    "catalog list": ("catalog", "list"),
    "req list": ("req", "list"),
    "changes list": ("changes", "list"),
    "compare": ("compare", "EXE-" + "0" * 25 + "A", "EXE-" + "0" * 25 + "B"),  # the test below records both
}
READ_COMMANDS = set(READ_ARGV) | {"init"}  # init writes, but runs without find_workspace and rewrites nothing


def _registered(parser, words=()):
    """The full name of every command ``parser`` can reach."""
    groups = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
    if not groups:
        return [" ".join(words)]
    return [name for group in groups for word, child in group.choices.items() for name in _registered(child, words + (word,))]


def test_every_command_is_listed_as_a_write_or_a_read():  # T18-R10
    registered = _registered(cli.build_parser())
    assert len(registered) == len(set(registered)) and set(registered) == set(cli.WRITE_COMMANDS) | READ_COMMANDS
    assert not set(cli.WRITE_COMMANDS) & READ_COMMANDS and set(WRITE_ARGV) == set(cli.WRITE_COMMANDS)


def _from_a_newer_schema(ws):
    """A config a later kernel wrote: a higher major, and a field this kernel's schema refuses."""
    cfg = json.loads(ws.path("config.json").read_text())
    cfg.update(schema_version="2", from_the_future=True)
    ws.path("config.json").write_bytes(workspace.config_bytes(cfg))


@pytest.mark.parametrize("command", sorted(WRITE_ARGV))
def test_every_write_command_refuses_a_newer_schema(ws, qa_cli, command):  # T18-R10
    _from_a_newer_schema(ws)
    code, out = qa_cli(*WRITE_ARGV[command])
    assert (code, out["error"]["code"]) == (2, "schema_too_new")


@pytest.mark.parametrize("command", sorted(READ_ARGV))
def test_every_read_command_answers_and_flags_a_newer_schema(ws, actor, qa_cli, tmp_path, command):  # T18-R10
    if command == "compare":  # a run to compare must exist, or the answer is a refusal
        for suffix in "AB":
            ledger.append(ws, actor, "execution.recorded", test_compare._execution("EXE-" + "0" * 25 + suffix))
    _from_a_newer_schema(ws)
    (tmp_path / "empty.md").write_text("")
    argv = (str(tmp_path / "empty.md") if word == "FILE" else word for word in READ_ARGV[command])
    code, out = qa_cli(*argv, env={"PATH": str(tmp_path)})  # nothing on PATH: doctor must not reach this machine's Docker
    assert code == 0 and "schema_newer" in out["limitations"]


def test_init_below_a_workspace_makes_its_own(ws, qa_cli):  # T18-R10: init runs without find_workspace
    below = ws.root / "below"
    below.mkdir()
    code, out = qa_cli("init", cwd=below, workspace=None)
    assert (code, out["created"]) == (0, True) and (below / ".qa" / "config.json").is_file()


PROBLEM_FIELDS = ["detail", "kind", "where"]


def test_ledger_verify_prints_its_problems_as_objects(trusted, qa_cli):  # T18-R11
    code, out = qa_cli("ledger", "verify")
    assert (code, out["problems"]) == (0, [])
    path = next(trusted.path("ledger").glob("*.jsonl"))
    path.write_bytes(path.read_bytes() + b"not an event\n")
    code, out = qa_cli("ledger", "verify")
    assert code == 1 and out["problems"] and all(sorted(problem) == PROBLEM_FIELDS for problem in out["problems"])


def test_report_check_prints_its_problems_as_objects(ws, qa_cli, tmp_path):  # T18-R11
    (tmp_path / "bad.md").write_text("All tests passed.\n")
    code, out = qa_cli("report", "check", str(tmp_path / "bad.md"))
    assert code == 1 and out["problems"] and all(sorted(problem) == PROBLEM_FIELDS for problem in out["problems"])


GRANT = ("authorize", "--environment", "local", "--activity", "db-ephemeral", "--approval", "ok for today")


def test_authorize_on_a_drifted_config_records_the_grant_and_says_so(trusted, qa_cli):  # T18-R12
    trusted.path("config.json").write_bytes(trusted.path("config.json").read_bytes() + b" ")
    code, out = qa_cli(*GRANT)
    assert (code, out["limitations"]) == (0, ["config_drift"]) and out["id"] in qa_cli("auth", "list")[1]["authorizations"]


def test_authorize_on_an_accepted_config_has_no_limitations(trusted, qa_cli):  # T18-R12
    code, out = qa_cli(*GRANT)
    assert code == 0 and "limitations" not in out


LATER = "2031-01-01T00:00:00.000Z"


def _open_finding(qa_cli, tmp_path, name, **over):
    (tmp_path / name).write_text(json.dumps(base(**over)))
    code, out = qa_cli("finding", "add", "--json", str(tmp_path / name))
    assert code == 0 and out["created"]
    return out["id"]


def test_finding_state_takes_accepted_by_and_expires_at(ws, qa_cli, tmp_path):  # T18-R13
    finding = _open_finding(qa_cli, tmp_path, "f.json")
    code, out = qa_cli("finding", "state", finding, "accepted_risk", "--reason", "low traffic", "--accepted-by", "the user", "--expires-at", LATER)
    recorded = qa_cli("finding", "list")[1]["findings"][finding]
    assert (code, out["state"]) == (0, "accepted_risk") and (recorded["accepted_by"], recorded["expires_at"]) == ("the user", LATER)


def test_finding_state_takes_duplicate_of(ws, qa_cli, tmp_path):  # T18-R13
    first = _open_finding(qa_cli, tmp_path, "f.json")
    second = _open_finding(qa_cli, tmp_path, "g.json", refs=dict(base()["refs"], location="app/other.py", rule="other"),
                           component=dict(base()["component"], path="app/other.py"))
    code, out = qa_cli("finding", "state", second, "duplicate", "--reason", "same root cause", "--duplicate-of", first)
    assert (code, out["state"]) == (0, "duplicate") and qa_cli("finding", "list")[1]["findings"][second]["duplicate_of"] == first


def test_finding_state_without_what_the_step_needs_is_refused(ws, qa_cli, tmp_path):  # T18-R13
    finding = _open_finding(qa_cli, tmp_path, "f.json")
    code, out = qa_cli("finding", "state", finding, "accepted_risk", "--reason", "low traffic", "--expires-at", LATER)
    assert (code, out["error"]["code"]) == (2, "missing_field")


def test_authorize_help_says_only_declared_hosts_can_be_named(qa_cli, tmp_path):  # T18-R16: the ruling is about this text
    text = " ".join(qa_cli("authorize", "--help", workspace=None, cwd=tmp_path)[1]["help"].split())
    assert "declared in the configuration" in text and "environment entry" in text
