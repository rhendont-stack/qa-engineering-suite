from __future__ import annotations

import json
import socket
import subprocess

import pytest

from helpers import CORPUS
from qa import guard, workspace
from qa.errors import QaError

CASES = json.loads((CORPUS / "guard" / "cases.json").read_text(encoding="utf-8"))


def classify(command, ws, *, shell=False, config=None, activity="test-execution-local"):
    return guard.classify(command, shell=shell, ws_root=ws.root, qa_dir=ws.qa_dir,
                          config=workspace.default_config("x") if config is None else config,
                          declared_activity=activity)


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
])
def test_invalid_rule_files_refused(tmp_path, monkeypatch, rules, code):
    (tmp_path / "command-guard.json").write_text(json.dumps({"version": 1, "rules": rules}), encoding="utf-8")
    monkeypatch.setattr(guard, "resource_dir", lambda kind: tmp_path)
    monkeypatch.setattr(guard, "_RULES", None)
    monkeypatch.setattr(guard, "_COMPILED", None)
    with pytest.raises(QaError) as e:
        guard.load_rules()
    assert e.value.code == code


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
