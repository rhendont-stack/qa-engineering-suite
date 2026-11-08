from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest

from helpers import CORPUS
from qa import guard, workspace
from qa.errors import QaError

CASES = json.loads((CORPUS / "guard" / "cases.json").read_text(encoding="utf-8"))


def classify(command, ws, *, shell=False, config=None, activity="test-execution-local", **extra):
    # extra carries cwd only when a test passes it, so "cwd omitted" stays a real omission.
    return guard.classify(command, shell=shell, ws_root=ws.root, qa_dir=ws.qa_dir,
                          config=workspace.default_config("x") if config is None else config,
                          declared_activity=activity, **extra)


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_guard_corpus(case, ws):
    d = guard.classify(case["command"], shell=case["shell"], ws_root=ws.root, qa_dir=ws.qa_dir,
                       config=workspace.default_config("x"), declared_activity=case.get("activity", "test-execution-local"))
    assert d.decision == case["expect"] and set(d.hosts) == set(case.get("hosts", []))
    assert set(d.required_activities) - {case.get("activity", "test-execution-local")} == set(case.get("adds", []))
    if "rule_id" in case:
        assert d.rule_id == case["rule_id"]


def test_migration_on_ephemeral_dsn_needs_db_ephemeral(ws):
    cfg = workspace.default_config("x")
    cfg["environments"].append({"id": "eph", "class": "ephemeral", "ephemeral_basis": "user-declared", "base_urls": [], "dsn_env": "EPH_DSN"})
    d = guard.classify(["alembic", "-x", "dsn_env=EPH_DSN", "upgrade", "head"], shell=False, ws_root=ws.root, qa_dir=ws.qa_dir, config=cfg, declared_activity="test-execution-local")
    assert d.decision == "require_grant" and "db-ephemeral" in d.required_activities


def test_secret_in_argv_denied(ws):
    tok = "gh" + "p_" + "Q7w8E9r0T1" * 3 + "y2U3i4"   # built at runtime
    for argv in (["tool", "--header=Authorization: token " + tok], ["tool", "--token", "hunter22"], ["tool", "--api-key=hunter22"], ["tool", "--secret", "hunter22"]):
        d = guard.classify(argv, shell=False, ws_root=ws.root, qa_dir=ws.qa_dir, config=workspace.default_config("x"), declared_activity="test-execution-local")
        assert (d.decision, d.required_activities) == ("deny", ("test-execution-local",)), argv


def test_every_rule_has_a_corpus_case():
    rule_ids = {r["id"] for r in guard.load_rules()["rules"]}
    assert rule_ids <= {c.get("rule_id") for c in CASES} | {c["id"] for c in CASES}


# --- additions -------------------------------------------------------------------------

def test_destructive_sql_on_ephemeral_dsn_is_not_denied(ws):
    cfg = workspace.default_config("x")
    cfg["environments"].append({"id": "eph", "class": "ephemeral", "ephemeral_basis": "user-declared", "base_urls": [], "dsn_env": "EPH_DSN"})
    assert classify('psql "$EPH_DSN" -c "DROP TABLE users"', ws, shell=True, config=cfg).decision == "allow"
    d = classify('psql "$OTHER_DSN" -c "DROP TABLE users"', ws, shell=True, config=cfg)
    assert (d.decision, d.rule_id) == ("deny", "destructive-sql")


def test_migration_naming_a_non_ephemeral_dsn_needs_db_write(ws):
    cfg = workspace.default_config("x")
    cfg["environments"] += [
        {"id": "eph", "class": "ephemeral", "ephemeral_basis": "user-declared", "base_urls": [], "dsn_env": "EPH_DSN"},
        {"id": "stg", "class": "staging", "base_urls": [], "dsn_env": "STG_DSN"},
    ]
    d = classify(["alembic", "-x", "dsn_env=EPH_DSN", "-x", "copy_to=STG_DSN", "upgrade", "head"], ws, config=cfg)
    assert d.required_activities == ("test-execution-local", "db-write")


def test_deny_reason_never_repeats_the_secret(ws):
    d = classify(["tool", "--password=hunter22"], ws)
    assert d.rule_id == "secret-in-argv" and "hunter22" not in d.reason


def test_load_rules_is_cached():
    assert guard.load_rules() is guard.load_rules()


@pytest.mark.parametrize("rules,code", [
    ([{"id": "a", "effect": "deny", "reason": "r", "match": {"program": ["x"]}}] * 2, "guard_policy_invalid"),
    ([{"id": "a", "effect": "require_grant:no-such-activity", "reason": "r", "match": {"program": ["x"]}}], "guard_policy_invalid"),
    ([{"id": "a", "effect": "deny", "reason": "r", "match": {"args_regex": "("}}], "guard_policy_invalid"),
    ([{"id": "a", "effect": "deny", "reason": "r", "match": {}}], "schema_invalid"),
    ([{"id": "a", "effect": "deny", "reason": "r", "match": {"check": "no_such_check"}}], "schema_invalid"),
    ([{"id": "a", "effect": "deny", "reason": "r", "match": {"program": ["x"], "no_args": False}}], "schema_invalid"),
])
def test_invalid_rule_files_refused(tmp_path, monkeypatch, rules, code):
    (tmp_path / "command-guard.json").write_text(json.dumps({"version": 1, "rules": rules}), encoding="utf-8")
    monkeypatch.setattr(guard, "resource_dir", lambda kind: tmp_path)
    monkeypatch.setattr(guard, "_RULES", None)
    monkeypatch.setattr(guard, "_COMPILED", None)
    with pytest.raises(QaError) as e:
        guard.load_rules()
    assert e.value.code == code


def test_relative_paths_resolve_against_cwd(ws):
    sub = ws.root / "sub dir"
    d = classify(["rm", "-rf", ".qa/work/tmp"], ws, cwd=sub)   # sub dir/.qa is not the workspace's .qa
    assert (d.decision, d.rule_id) == ("deny", "delete-outside-qa")
    d = classify(["cp", "a", "../CLAUDE.md"], ws, cwd=sub)
    assert (d.decision, d.rule_id) == ("deny", "protected-config-write")
    d = classify(["rm", "-rf", "work/tmp"], ws, cwd=ws.qa_dir)
    assert (d.decision, d.rule_id) == ("allow", None)
    assert classify(["rm", "-rf", "work/tmp"], ws).rule_id == "delete-outside-qa"   # read against the root


def test_redirect_targets_resolve_against_cwd(ws):
    sub = ws.root / "sub dir"
    assert classify("pytest -q > ../.qa/work/pytest.log", ws, shell=True, cwd=sub).decision == "allow"
    d = classify("pytest -q > .qa/work/pytest.log", ws, shell=True, cwd=sub)
    assert (d.decision, d.rule_id) == ("deny", "delete-outside-qa")


@pytest.mark.parametrize("command,shell", [
    (["rm", "-rf", ".qa/work/tmp"], False),
    (["cp", "a", "../CLAUDE.md"], False),
    ("pytest -q > ../.qa/work/pytest.log", True),
])
def test_relative_cwd_resolves_against_the_root(ws, command, shell):
    assert classify(command, ws, shell=shell, cwd=Path("sub dir")) == classify(command, ws, shell=shell, cwd=ws.root / "sub dir")


def test_omitted_cwd_means_the_workspace_root(ws):
    for case in CASES:
        kw = {"shell": case["shell"], "activity": case["activity"]}
        omitted = classify(case["command"], ws, **kw)
        assert omitted == classify(case["command"], ws, cwd=None, **kw) == classify(case["command"], ws, cwd=ws.root, **kw), case["id"]


# --- fix round 1 -----------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "2>/dev/null git push origin main",
    "2>>build.log git push",
    "1>&2 git push",
    "2>&1 rm -rf /",
    "git push 2>/dev/null",
])
def test_leading_fd_redirect_does_not_bypass_a_deny(ws, command):
    # A file-descriptor designator before the program must not be read as the program.
    assert classify(command, ws, shell=True).decision == "deny"


def test_numeric_argument_before_a_spaced_redirect_stays_an_argument(ws):
    # A space before '>' means 777 is chmod's argument, not an fd, so chmod-777 still fires.
    d = classify("chmod 777 . > .qa/work/chmod.log", ws, shell=True)
    assert (d.decision, d.rule_id) == ("deny", "chmod-777")


def test_dsn_ephemerality_does_not_leak_across_commands(ws):
    cfg = workspace.default_config("x")
    cfg["environments"].append({"id": "eph", "class": "ephemeral", "ephemeral_basis": "user-declared", "base_urls": [], "dsn_env": "EPH_DSN"})
    d = classify('psql "$EPH_DSN" -c \'select 1\' && psql "$DATABASE_URL" -c \'DROP TABLE users\'', ws, shell=True, config=cfg)
    assert (d.decision, d.rule_id) == ("deny", "destructive-sql")


def test_destructive_sql_piped_is_still_caught(ws):
    assert classify("echo 'DROP TABLE users;' | psql", ws, shell=True).rule_id == "destructive-sql"


def test_migration_with_an_inline_production_dsn_needs_db_write(ws):
    cfg = workspace.default_config("x")
    cfg["environments"].append({"id": "eph", "class": "ephemeral", "ephemeral_basis": "user-declared", "base_urls": [], "dsn_env": "EPH_DSN"})
    d = classify(["alembic", "-x", "url=postgres://prod.db/app", "-x", "note=EPH_DSN", "upgrade", "head"], ws, config=cfg)
    assert d.required_activities == ("test-execution-local", "db-write")


@pytest.mark.parametrize("argv", [
    ["tool", "--bypass-cache=true"],
    ["tool", "--passthrough=value"],
    ["tool", "--password-file=/run/secrets/db"],
])
def test_secret_free_flags_with_a_pass_substring_are_allowed(ws, argv):
    assert classify(argv, ws).decision == "allow"


def test_pgpassfile_assignment_naming_a_file_is_allowed(ws):
    assert classify('PGPASSFILE=.qa/work/pgpass psql -c "select 1"', ws, shell=True).decision == "allow"


def test_a_real_password_assignment_is_still_denied(ws):
    d = classify(["env", "PGPASSWORD=hunter22", "psql"], ws)
    assert (d.decision, d.rule_id) == ("deny", "secret-in-argv")


# --- fix round 2 -----------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    'psql <<< "DROP TABLE users"',
    "psql <<EOF\nDROP TABLE users;\nEOF",
    'psql -d "$DATABASE_URL" <<< "DROP TABLE users"',
    "psql <<-END\n\tDROP TABLE users;\nEND",
])
def test_here_string_and_heredoc_sql_is_still_caught(ws, command):
    assert classify(command, ws, shell=True).rule_id == "destructive-sql"


def test_here_string_sql_on_ephemeral_dsn_is_allowed(ws):
    cfg = workspace.default_config("x")
    cfg["environments"].append({"id": "eph", "class": "ephemeral", "ephemeral_basis": "user-declared", "base_urls": [], "dsn_env": "EPH_DSN"})
    assert classify('psql "$EPH_DSN" <<< "DROP TABLE users"', ws, shell=True, config=cfg).decision == "allow"


@pytest.mark.parametrize("key", ["SECRET_KEY", "PASSWORD_PROD", "CLIENT_SECRET", "API_TOKEN", "API_KEY", "PRIVATE_KEY", "DB_PASSWORD"])
def test_keyword_suffix_secret_assignments_stay_denied(ws, key):
    value = "abcd" + "1234" + "efgh"   # built at runtime; at or above Task 4's entropy threshold
    d = classify(["env", "%s=%s" % (key, value), "make"], ws)
    assert (d.decision, d.rule_id) == ("deny", "secret-in-argv"), key


@pytest.mark.parametrize("argv", [
    ["tool", "--bypass-cache=true"],
    ["tool", "--passthrough=value"],
    ["tool", "--password-file=/run/secrets/db"],
    ["tool", "--token-file=/run/secrets/t"],
])
def test_secret_free_and_file_flags_remain_allowed(ws, argv):
    assert classify(argv, ws).decision == "allow"


def test_classify_runs_nothing_and_writes_nothing(ws, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the guard must not run commands or open connections")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    before = sorted(str(p) for p in ws.root.rglob("*"))
    for case in CASES:
        classify(case["command"], ws, shell=case["shell"], activity=case["activity"])
    assert sorted(str(p) for p in ws.root.rglob("*")) == before


@pytest.mark.parametrize("command,shell,activity,code", [
    ([], False, "test-execution-local", "invalid_command"),
    ("", True, "test-execution-local", "invalid_command"),
    (["git", 1], False, "test-execution-local", "invalid_command"),
    ('echo "unterminated', True, "test-execution-local", "invalid_command"),
    (["pytest"], False, "no-such-activity", "invalid_activity"),
])
def test_invalid_input_refused(ws, command, shell, activity, code):
    with pytest.raises(QaError) as e:
        classify(command, ws, shell=shell, activity=activity)
    assert e.value.code == code
