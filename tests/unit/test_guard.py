from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest

from helpers import CORPUS, finishes
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
    d = classify('psql "$EPH_DSN" -c "DROP TABLE users"', ws, shell=True, config=cfg)
    assert (d.decision, d.required_activities) == ("require_grant", ("test-execution-local", "db-ephemeral"))
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


def test_here_string_sql_on_ephemeral_dsn_needs_db_ephemeral(ws):
    cfg = workspace.default_config("x")
    cfg["environments"].append({"id": "eph", "class": "ephemeral", "ephemeral_basis": "user-declared", "base_urls": [], "dsn_env": "EPH_DSN"})
    d = classify('psql "$EPH_DSN" <<< "DROP TABLE users"', ws, shell=True, config=cfg)
    assert (d.decision, d.required_activities) == ("require_grant", ("test-execution-local", "db-ephemeral"))


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


# --- fix round 3 -----------------------------------------------------------------------

# A one-word here-string, quoted and unquoted: '<<' inside '<<<' must not open a here-document.
ONE_WORD_HERE_STRINGS = ['cat <<< "x"', "cat <<< 'x'", "cat <<< x", "cat <<<x"]


@pytest.mark.parametrize("head", ONE_WORD_HERE_STRINGS)
def test_extract_heredocs_leaves_a_one_word_here_string_alone(head):
    text = head + "\ngit push origin main"
    assert guard._extract_heredocs(text) == text


@pytest.mark.parametrize("head", ONE_WORD_HERE_STRINGS)
def test_shell_units_keep_the_command_after_a_one_word_here_string(head):
    units = guard._shell_units(head + "\ngit push origin main", 0, False)
    assert [u.argv for u in units] == [["cat"], ["git", "push", "origin", "main"]]
    assert units[0].pipeline == "cat x"   # the body stays with its own command


@pytest.mark.parametrize("head", ONE_WORD_HERE_STRINGS)
@pytest.mark.parametrize("following,rule_id", [
    ("git push origin main", "git-write"),
    ("rm -rf /", "delete-outside-qa"),
])
def test_one_word_here_string_does_not_hide_the_next_command(ws, head, following, rule_id):
    d = classify(head + "\n" + following, ws, shell=True)
    assert (d.decision, d.rule_id) == ("deny", rule_id)


def test_heredoc_after_a_one_word_here_string_is_still_read(ws):
    d = classify("cat <<< x\npsql <<EOF\nDROP TABLE users;\nEOF", ws, shell=True)
    assert (d.decision, d.rule_id) == ("deny", "destructive-sql")


# --- fix round 4 -----------------------------------------------------------------------

# A '<<' the shell does not read as a here-document operator: inside double quotes, inside
# single quotes, escaped with a backslash, inside a double-quoted string that spans lines, or
# in a comment.
NON_OPERATOR_MARKS = ['echo "<<x"', "echo '<<x'", "echo \\<<x", 'echo "a\n<<x"', "echo 'a <<x' b",
                      "echo hi # see <<x"]


@pytest.mark.parametrize("head", NON_OPERATOR_MARKS)
def test_extract_heredocs_leaves_a_non_operator_mark_alone(head):
    text = head + "\ngit push origin main"
    assert guard._extract_heredocs(text) == text


@pytest.mark.parametrize("head", NON_OPERATOR_MARKS)
def test_shell_units_keep_the_command_after_a_non_operator_mark(head):
    units = guard._shell_units(head + "\ngit push origin main", 0, False)
    assert [u.argv for u in units][-1] == ["git", "push", "origin", "main"]
    assert units[0].argv[0] == "echo"


@pytest.mark.parametrize("head", NON_OPERATOR_MARKS)
@pytest.mark.parametrize("following,rule_id", [
    ("git push origin main", "git-write"),
    ("rm -rf /", "delete-outside-qa"),
])
def test_non_operator_mark_does_not_hide_the_next_command(ws, head, following, rule_id):
    d = classify(head + "\n" + following, ws, shell=True)
    assert (d.decision, d.rule_id) == ("deny", rule_id)


def test_heredoc_after_a_quoted_mark_is_still_read(ws):
    d = classify('echo "<<x"\npsql <<EOF\nDROP TABLE users;\nEOF', ws, shell=True)
    assert (d.decision, d.rule_id) == ("deny", "destructive-sql")


@pytest.mark.parametrize("text,expected", [
    # Real here-documents keep their behavior: the body folds into a here-string, the terminator goes.
    ("psql <<EOF\nDROP TABLE users;\nEOF\ngit status", "psql <<< 'DROP TABLE users;'\ngit status"),
    ("psql <<-END\n\tDROP TABLE users;\n\tEND\ngit status", "psql <<< '\tDROP TABLE users;'\ngit status"),
    ("psql <<'EOF'\nDROP TABLE users;\nEOF", "psql <<< 'DROP TABLE users;'"),
    ('psql <<"EOF"\nDROP TABLE users;\nEOF', "psql <<< 'DROP TABLE users;'"),
    # Quoted words before the operator do not hide it, and a '#' inside a word is not a comment.
    ('psql "$DSN" <<EOF\nDROP TABLE users;\nEOF', 'psql "$DSN" <<< \'DROP TABLE users;\''),
    ("psql 'a''b' <<EOF\nDROP TABLE users;\nEOF", "psql 'a''b' <<< 'DROP TABLE users;'"),
    ("psql a#b <<EOF\nDROP TABLE users;\nEOF", "psql a#b <<< 'DROP TABLE users;'"),
    # Only a line that is exactly the delimiter ends a '<<' body; an indented one does not.
    ("psql <<EOF\nDROP TABLE users;\n  EOF\ngit status", "psql <<< 'DROP TABLE users;   EOF git status'"),
    # '<<-' strips leading tabs, not spaces.
    ("psql <<-EOF\n\tx\n  EOF\n\tEOF\ngit status", "psql <<< '\tx   EOF'\ngit status"),
])
def test_extract_heredocs_follows_the_shell(text, expected):
    assert guard._extract_heredocs(text) == expected


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


# --------------------------------------------------------------------------- final review: C2, I7, I8, I10, I14, M5

def _with_env(**env):
    cfg = workspace.default_config("x")
    cfg["environments"].append(env)
    return cfg


def test_sql_write_on_an_ephemeral_dsn_needs_db_ephemeral(ws):
    cfg = _with_env(id="eph", **{"class": "ephemeral", "ephemeral_basis": "user-declared", "base_urls": [], "dsn_env": "EPH_DSN"})
    d = classify('psql "$EPH_DSN" -c "INSERT INTO t VALUES (1)"', ws, shell=True, config=cfg)
    assert d.decision == "require_grant" and d.rule_id == "sql-write" and "db-ephemeral" in d.required_activities
    assert "db-write" not in d.required_activities


def test_drop_on_an_ephemeral_dsn_still_needs_a_grant(ws):
    cfg = _with_env(id="eph", **{"class": "ephemeral", "ephemeral_basis": "user-declared", "base_urls": [], "dsn_env": "EPH_DSN"})
    d = classify('psql "$EPH_DSN" -c "DROP TABLE t"', ws, shell=True, config=cfg)
    assert (d.decision, d.required_activities) == ("require_grant", ("test-execution-local", "db-ephemeral"))


def test_an_inline_dsn_on_a_client_needs_db_write_even_when_another_is_ephemeral(ws):
    cfg = _with_env(id="eph", **{"class": "ephemeral", "ephemeral_basis": "user-declared", "base_urls": [], "dsn_env": "EPH_DSN"})
    d = classify(["psql", "postgres://db.example/app?from=EPH_DSN", "-c", "UPDATE t SET a = 1 WHERE b = 2"], ws, config=cfg)
    assert d.decision == "require_grant" and "db-write" in d.required_activities and d.hosts == ("db.example",)


def test_dsn_environments_are_named_for_authorization(ws):
    cfg = _with_env(id="prod", **{"class": "production", "base_urls": [], "dsn_env": "PROD_DSN"})
    assert classify('psql "$PROD_DSN" -c "SELECT 1"', ws, shell=True, config=cfg).environments == ("prod",)
    assert classify('psql "$OTHER_DSN" -c "SELECT 1"', ws, shell=True, config=cfg).environments == ()


@pytest.mark.parametrize("url", ["http://[::1/x", "http://exa[mple.com/x"])
def test_a_url_nobody_can_read_counts_as_undeclared(ws, url):
    d = classify(["curl", url], ws)
    assert d.decision == "require_grant" and "network-download" in d.required_activities


def test_static_analysis_by_path_is_not_a_known_analyzer(ws):
    d = classify(["./ruff", "check", "."], ws, activity="static-analysis")
    assert d.decision == "require_grant" and d.rule_id == "unlisted-analyzer"


def test_a_shell_string_is_judged_per_command_under_static_analysis(ws):
    d = classify("ruff check . && ./run-this", ws, shell=True, activity="static-analysis")
    assert d.decision == "require_grant" and "test-execution-local" in d.required_activities


def test_only_static_analysis_is_limited_to_known_analyzers(ws):
    assert classify(["./anything"], ws).decision == "allow"


def test_writing_into_the_workspace_record_is_denied(ws):
    for argv in (["cp", "a", ".qa/config.json"], ["mv", "a", ".qa/redaction.key"], ["truncate", "-s", "0", ".qa/ledger/x.jsonl"]):
        d = classify(argv, ws)
        assert (d.decision, d.rule_id) == ("deny", "delete-outside-qa"), argv


def test_writing_into_work_stays_allowed(ws):
    assert classify(["cp", "a", ".qa/work/a"], ws).decision == "allow"
    assert classify(["find", ".qa/work", "-name", "x", "-delete"], ws).decision == "allow"


@pytest.mark.parametrize("argv", [["GIT", "push"], ["Rm", "-rf", "/"], ["SUDO", "ls"], ["ENV", "git", "push"]])
def test_program_names_are_read_as_a_case_insensitive_filesystem_does(ws, argv):
    assert classify(argv, ws).decision == "deny", argv


@pytest.mark.parametrize("option", ["--namespace", "--super-prefix", "--config-env", "--attr-source"])
def test_git_global_options_that_take_a_value_hide_no_subcommand(ws, option):
    assert classify(["git", option, "status", "push"], ws).decision == "deny"


# --------------------------------------------------------------------------- guard residuals: C2, I8

@pytest.mark.parametrize("program,option,takes_a_value", [
    ("curl", "-o", True), ("curl", "-O", False), ("curl", "-s", False), ("curl", "-i", False), ("curl", "-j", False),
    ("wget", "-O", True), ("wget", "-i", True), ("wget", "-c", False), ("wget", "-d", False), ("wget", "-r", False), ("wget", "-H", False),
    ("aria2c", "-s", True), ("aria2c", "-o", True), ("aria2c", "-c", False), ("aria2c", "-D", False),
    ("http", "-o", True), ("http", "-a", True), ("http", "-d", False), ("http", "-F", False), ("xh", "-b", False), ("xh", "-o", True),
    ("/usr/bin/CURL", "-O", False),
])
def test_each_download_tool_has_its_own_value_options(program, option, takes_a_value):  # I8
    assert guard._download_targets([program, option, "word"]) == ([] if takes_a_value else ["word"])


def test_an_unknown_download_tool_swallows_nothing():  # I8: reading a value as a target asks for a grant, which is the safe side
    assert guard._download_targets(["other", "-o", "word"]) == ["word"]


@pytest.mark.parametrize("statement,writes", [
    ("UPDATE a JOIN b ON a.i = b.i SET a.x = 1", True), ("DELETE a FROM a JOIN b ON a.i = b.i", True),
    ("SELECT a FROM t FOR UPDATE", False), ("SELECT a FROM t FOR UPDATE; SET autocommit = 1", False),
    ("DELETE; SELECT a FROM t", False), ("SELECT REPLACE(a, 'x', 'y') FROM t", False), ("SELECT 1", False),
])
def test_a_multi_table_write_is_read_within_one_statement(statement, writes):  # C2
    assert guard._writes_sql(statement) is writes


def test_a_long_statement_with_no_set_is_read_in_linear_time():  # C2
    assert finishes(lambda: guard._writes_sql("UPDATE " * 100000), seconds=5) is False


def test_stdin_reaches_a_client_behind_a_wrapper(ws):  # C2
    d = classify("echo 'SET k v' | env A=1 redis-cli", ws, shell=True)
    assert (d.decision, d.rule_id) == ("require_grant", "redis-write")


def test_a_pipeline_after_a_separator_feeds_nothing(ws):  # C2
    assert classify("echo 'SET k v'; redis-cli GET k", ws, shell=True).decision == "allow"


# --------------------------------------------------------------------------- guard round 2: statement start, line continuations

@pytest.mark.parametrize("text", [
    "INSERT t TABLE s", "insert t table s", "REPLACE t TABLE s", "UPDATE t SET a = 1", "DELETE t WHERE a = 1",
    "  \n INSERT t TABLE s", "-- note\nINSERT t TABLE s", "# note\nINSERT t TABLE s", "/* a */ /* b */ INSERT t TABLE s",
    "(INSERT t TABLE s)", "SELECT 1; REPLACE t TABLE s", "SELECT 1;\n-- note\nDELETE t WHERE a = 1",
    "--execute=INSERT t TABLE s", "-eDELETE t WHERE a = 1", "INSERT`t`TABLE s", "UPDATE/* a */t SET a = 1",
])
def test_a_statement_that_begins_with_a_write_verb_is_a_write(text):
    assert guard._begins_a_write(text) is True


@pytest.mark.parametrize("text", [
    "", "SELECT 1", "SHOW TABLES", "SELECT REPLACE(a, 'b', 'c') FROM t", "SELECT INSERT(a, 1, 2, 'b') FROM t",
    "REPLACE(a, 'b', 'c')", "update", "update.db", "delete-me.db", "replace_me", "-- note INSERT t TABLE s",
    "/* INSERT t TABLE s", "/* INSERT t TABLE s */ SELECT 1",
])
def test_anything_else_does_not_begin_a_write(text):
    assert guard._begins_a_write(text) is False


@pytest.mark.parametrize("text", [";" * 200000, "/* ; " * 50000, "; /* " * 50000, "(" * 200000, "; -- x\n" * 30000], ids=["a", "b", "c", "d", "e"])
def test_statement_starts_are_found_in_linear_time(text):
    assert finishes(lambda: guard._begins_a_write(text), seconds=5) is False


def _commands(text):
    return [unit.argv for unit in guard._shell_units(text, 0, False)]


@pytest.mark.parametrize("text,expected", [
    ("git \\\n  push", [["git", "push"]]),
    ("git \\\n\\\n  push", [["git", "push"]]),
    ("gi\\\nt push", [["git", "push"]]),
    ('echo "a\\\nb"', [["echo", "ab"]]),
    ("echo 'a\\\nb'", [["echo", "a\\\nb"]]),
    ("echo a\\\\\nb", [["echo", "a\\"], ["b"]]),
    ("# a \\\ngit status", [["git", "status"]]),
    ("echo $(git \\\n  status)", [["echo", guard._SUBST], ["git", "status"]]),
])
def test_a_line_continuation_is_removed_before_words_are_formed(text, expected):
    assert _commands(text) == expected


def test_a_heredoc_operator_after_a_continuation_still_opens_its_body():
    assert _commands("cat \\\n  <<EOF\ngit push\nEOF") == [["cat"]]


def test_a_heredoc_body_after_a_continued_operator_line_is_its_body():
    assert _commands("cat <<EOF \\\n  -n\ngit push\nEOF") == [["cat", "-n"]]


def test_an_unquoted_heredoc_body_loses_its_continuations():
    assert guard._extract_heredocs("cat <<EOF\na\\\nb\nEOF") == "cat <<< 'ab'"


def test_a_quoted_heredoc_body_keeps_them():
    assert "\\" in guard._extract_heredocs("cat <<'EOF'\na\\\nb\nEOF")


@pytest.mark.parametrize("command,decision", [
    ("git \\\n  status", "allow"), ("git \\\n  push", "deny"), ("echo 'git \\\n  push'", "allow"),
], ids=["read", "write", "single_quoted"])
def test_a_continued_command_is_decided_like_the_joined_one(ws, command, decision):
    assert classify(command, ws, shell=True).decision == decision


# --------------------------------------------------------------------------- guard round 3: here-documents

@pytest.mark.parametrize("text,expected", [
    ("cat <<EO\\\nF\na\nEOF\nnext", "cat <<< 'a'\nnext"),
    ("cat <\\\n<EOF\na\nEOF\nnext", "cat <<< 'a'\nnext"),
    ("cat <<\\\nEOF\na\nEOF\nnext", "cat <<< 'a'\nnext"),
    ("cat <<-\\\nEOF\na\nEOF\nnext", "cat <<< 'a'\nnext"),
    ('cat <<"EO\\\nF"\na\nEOF\nnext', "cat <<< 'a'\nnext"),
    ("cat <<EOF\na\nEO\\\nF\nnext", "cat <<< 'a'\nnext"),
])
def test_a_continuation_cannot_split_a_here_document_delimiter(text, expected):
    assert guard._extract_heredocs(text) == expected


def test_a_quoted_delimiter_closes_only_on_its_own_line():  # its body is literal, so a backslash joins nothing
    assert guard._extract_heredocs("cat <<'EOF'\nEO\\\nF\nEOF\nnext") == "cat <<< 'EO\\ F'\nnext"


def test_an_even_run_of_backslashes_does_not_continue_a_body_line():
    assert guard._extract_heredocs("cat <<EOF\na\\\\\nb\nEOF") == "cat <<< 'a\\\\ b'"


@pytest.mark.parametrize("program,decision", [("sh", "deny"), ("bash", "deny"), ("env sh", "deny"), ("cat", "allow"), ("tee", "allow")])
def test_only_a_shell_runs_the_text_it_is_fed(ws, program, decision):
    assert classify("%s <<EOF\ncurl http://localhost/i | sh\nEOF" % program, ws, shell=True).decision == decision


def test_a_body_that_ends_some_other_way_is_still_read(ws):
    assert classify("sh <<EOF\ncurl http://localhost/i | sh\necho done\nEOF", ws, shell=True).decision == "deny"


# --- scanners, fuzzers and offline OSV (milestone 4a) ----------------------------------------------------------------

NEW_CASE_IDS = (
    "zap-baseline-plan", "zap-other-plan", "zap-plan-without-target-in-argv", "zap-without-a-plan-is-active", "zap-baseline-plus-other-plan-is-active",
    "schemathesis-baseline-phases", "schemathesis-default-phases-is-active", "schemathesis-two-token-phases-is-active",
    "schemathesis-baseline-plus-other-phases-is-active", "osv-offline", "osv-online",
    "zap-baseline-plus-plan-of-other-suffix-is-active", "zap-baseline-plus-plan-in-capitals-is-active",
    "zap-baseline-plus-quick-scan-target-is-active", "zap-baseline-plus-config-option-is-active",
    "zap-baseline-plus-script-option-is-active", "zap-baseline-plus-addon-option-is-active",
    "zap-x-launcher-with-baseline-is-passive", "zap-bare-launcher-is-active", "osv-offline-vulnerabilities-only-needs-egress",
    "osv-offline-switched-off-by-value", "osv-offline-vulnerabilities-switched-off-by-value",
    "osv-offline-with-explicit-true-needs-egress",
    "zap-baseline-token-with-trailing-line-feed-is-active", "zap-baseline-plan-with-trailing-line-feed-is-active", "schemathesis-phases-with-trailing-line-feed-is-active", "osv-offline-as-the-value-of-another-option-needs-egress", "osv-offline-after-end-of-options-needs-egress", "osv-offline-with-database-download-needs-egress", "osv-offline-with-trailing-line-feed-needs-egress", "osv-offline-format-as-two-tokens-needs-egress",
    "k6-base-url-loopback", "k6-base-url-other-host", "spectral-lint", "oasdiff-breaking",
)
NEW_CASES = [c for c in CASES if c["id"] in NEW_CASE_IDS]
# decision, the activities the run needs beyond the declared one, the hosts the command line names
NEW_EXPECTED = {
    "zap-baseline-plan": ("require_grant", ("dast-passive",), ("127.0.0.1",)),
    "zap-other-plan": ("require_grant", ("dast-active",), ("127.0.0.1",)),
    "zap-plan-without-target-in-argv": ("require_grant", ("dast-active",), ()),
    "zap-without-a-plan-is-active": ("require_grant", ("dast-active",), ("127.0.0.1",)),
    "zap-baseline-plus-other-plan-is-active": ("require_grant", ("dast-active", "dast-passive"), ("127.0.0.1",)),
    "schemathesis-baseline-phases": ("require_grant", ("dast-passive",), ("127.0.0.1",)),
    "schemathesis-default-phases-is-active": ("require_grant", ("dast-active",), ("127.0.0.1",)),
    "schemathesis-two-token-phases-is-active": ("require_grant", ("dast-active",), ("127.0.0.1",)),
    "schemathesis-baseline-plus-other-phases-is-active": ("require_grant", ("dast-active", "dast-passive"), ("127.0.0.1",)),
    "osv-offline": ("allow", (), ()),
    "zap-baseline-token-with-trailing-line-feed-is-active": ("require_grant", ("dast-active", "dast-passive"), ()),
    "zap-baseline-plan-with-trailing-line-feed-is-active": ("require_grant", ("dast-active", "dast-passive"), ()),
    "schemathesis-phases-with-trailing-line-feed-is-active": ("require_grant", ("dast-active", "dast-passive"), ("127.0.0.1",)),
    "osv-offline-as-the-value-of-another-option-needs-egress": ("require_grant", ("data-egress",), ()),
    "osv-offline-after-end-of-options-needs-egress": ("require_grant", ("data-egress",), ()),
    "osv-offline-with-database-download-needs-egress": ("require_grant", ("data-egress",), ()),
    "osv-offline-with-trailing-line-feed-needs-egress": ("require_grant", ("data-egress",), ()),
    "osv-offline-format-as-two-tokens-needs-egress": ("require_grant", ("data-egress",), ()),
    "zap-baseline-plus-plan-of-other-suffix-is-active": ("require_grant", ("dast-active", "dast-passive"), ("127.0.0.1",)),
    "zap-baseline-plus-plan-in-capitals-is-active": ("require_grant", ("dast-active", "dast-passive"), ("127.0.0.1",)),
    "zap-baseline-plus-quick-scan-target-is-active": ("require_grant", ("dast-active", "dast-passive"), ("127.0.0.1",)),
    "zap-baseline-plus-config-option-is-active": ("require_grant", ("dast-active", "dast-passive"), ("127.0.0.1",)),
    "zap-baseline-plus-script-option-is-active": ("require_grant", ("dast-active", "dast-passive"), ("127.0.0.1",)),
    "zap-baseline-plus-addon-option-is-active": ("require_grant", ("dast-active", "dast-passive"), ("127.0.0.1",)),
    "zap-bare-launcher-is-active": ("require_grant", ("dast-active",), ()),
    "zap-x-launcher-with-baseline-is-passive": ("require_grant", ("dast-passive",), ()),
    "osv-offline-vulnerabilities-only-needs-egress": ("require_grant", ("data-egress",), ()),
    "osv-offline-switched-off-by-value": ("require_grant", ("data-egress",), ()),
    "osv-offline-vulnerabilities-switched-off-by-value": ("require_grant", ("data-egress",), ()),
    "osv-offline-with-explicit-true-needs-egress": ("require_grant", ("data-egress",), ()),
    "osv-online": ("require_grant", ("data-egress",), ()),
    "k6-base-url-loopback": ("require_grant", ("load-test",), ("127.0.0.1",)),
    "k6-base-url-other-host": ("require_grant", ("load-test",), ("192.0.2.5",)),
    "spectral-lint": ("allow", (), ()),
    "oasdiff-breaking": ("allow", (), ()),
}


def test_the_new_cases_are_all_in_the_corpus():
    assert sorted(c["id"] for c in NEW_CASES) == sorted(NEW_CASE_IDS) and set(NEW_EXPECTED) == set(NEW_CASE_IDS)


@pytest.mark.parametrize("case", NEW_CASES, ids=[c["id"] for c in NEW_CASES])
def test_new_guard_cases(case, ws):
    decision, adds, hosts = NEW_EXPECTED[case["id"]]
    d = classify(case["command"], ws, shell=case["shell"], activity=case["activity"])
    assert d.decision == decision
    assert d.required_activities == (case["activity"],) + adds
    assert d.hosts == hosts
    assert d.rule_id == case["rule_id"]


def test_zap_af_plan_target_assignment_is_read_as_a_host(ws):
    plan = "/work/.qa-input/security/zap-baseline.yaml"
    d = classify("QA_TARGET=http://127.0.0.1:8080 zap.sh -cmd -autorun " + plan, ws, shell=True)
    assert (d.decision, d.required_activities, d.hosts) == ("require_grant", ("test-execution-local", "dast-passive"), ("127.0.0.1",))
    other = classify("QA_TARGET=http://192.0.2.5:8080 zap.sh -cmd -autorun " + plan, ws, shell=True)
    assert other.hosts == ("192.0.2.5",)


@pytest.mark.parametrize("program", ["zap.sh", "zap-x.sh", "zap.bat"])
def test_every_zap_launcher_is_active_unless_it_runs_the_baseline_plan(ws, program):
    d = classify([program, "-cmd", "-autorun", "/work/.qa-input/security/zap-af.yaml"], ws)
    assert d.required_activities == ("test-execution-local", "dast-active")
    d = classify([program, "-cmd", "-autorun", "/work/.qa-input/security/zap-baseline.yaml"], ws)
    assert d.required_activities == ("test-execution-local", "dast-passive")


@pytest.mark.parametrize("phases", ["--phases=examples", "--phases=coverage", "--phases=examples,coverage", "--phases=coverage,examples"])
@pytest.mark.parametrize("program", ["schemathesis", "st"])
def test_only_the_four_baseline_phase_forms_are_passive(ws, program, phases):
    d = classify([program, "run", "spec.json", phases], ws)
    assert d.required_activities == ("test-execution-local", "dast-passive")


@pytest.mark.parametrize("args", [["--phases=fuzzing"], ["--phases=examples,fuzzing"], ["--phases=Examples"], ["--phases="],
                                  ["--phases", "coverage"], ["--phases=stateful,examples"], []],
                         ids=["fuzzing", "list_with_fuzzing", "case", "empty", "two_tokens", "stateful", "none"])
def test_any_other_schemathesis_phase_choice_is_active(ws, args):
    d = classify(["schemathesis", "run", "spec.json"] + args, ws)
    assert d.required_activities == ("test-execution-local", "dast-active")


def test_osv_scanner_needs_data_egress_unless_it_runs_fully_offline(ws):
    assert classify(["osv-scanner", "scan", "source", "/work"], ws).required_activities == ("test-execution-local", "data-egress")
    assert classify(["osv-scanner", "scan", "source", "--offline", "/work"], ws).required_activities == ("test-execution-local",)
    assert classify(["osv-scanner", "scan", "source", "--offline", "/work"], ws, activity="static-analysis").decision == "allow"
    # the vulnerability lookup alone is offline; manifest resolution still reaches the network
    assert classify(["osv-scanner", "scan", "source", "--offline-vulnerabilities", "/work"], ws).required_activities == ("test-execution-local", "data-egress")
    assert classify(["osv-scanner", "scan", "source", "."], ws, activity="static-analysis").required_activities == ("static-analysis", "data-egress")


@pytest.mark.parametrize("tokens", [["scan", "--offline", "/work"], ["scan", "source", "--offline", "--recursive", "/work/sub/dir"],
                                    ["scan", "source", "--offline", "--format=json", "--output-file=/qa-evidence/osv.json", "/work"]])
def test_the_offline_scan_steps_exact_arguments_need_no_grant(ws, tokens):
    assert classify(["osv-scanner"] + tokens, ws).required_activities == ("test-execution-local",)


@pytest.mark.parametrize("extra", [["--format", "sarif"], ["--"], ["--download-offline-databases"], ["--data-source=native"], ["--maven-registry=x"],
                                   ["--offline\n"], ["/work\n"], ["."], ["/other"], ["--output-file=/tmp/x"], ["--serve"], ["-L", "a"]])
def test_any_other_osv_argument_needs_data_egress_even_next_to_offline(ws, extra):
    assert classify(["osv-scanner", "scan", "source", "--offline", "/work"] + extra, ws).required_activities == ("test-execution-local", "data-egress")


@pytest.mark.parametrize("flag", ["--offline=false", "--offline=true", "-offline=false", "--offline-vulnerabilities=false"])
def test_an_offline_flag_with_an_explicit_value_needs_data_egress(ws, flag):
    for argv in (["osv-scanner", "scan", "source", "--offline", flag, "."], ["osv-scanner", "scan", "source", flag, "--offline", "."]):
        assert classify(argv, ws).required_activities == ("test-execution-local", "data-egress")


@pytest.mark.parametrize("extra", [["-quickurl", "http://127.0.0.1:8080"], ["-autorun", "plan.YAML"], ["-autorun", "plan"], ["-config", "a=b"],
                                   ["-script", "s.js"], ["-addoninstall", "x"], ["-port", "8090"], ["-silent"], [""], ["-cmd\n"], ["-autorun\n"]])
def test_zap_is_passive_only_for_the_exact_baseline_arguments(ws, extra):
    baseline = ["zap.sh", "-cmd", "-notel", "-autorun", "/work/.qa-input/security/zap-baseline.yaml"]
    assert classify(baseline, ws).required_activities == ("test-execution-local", "dast-passive")
    assert classify(baseline + extra, ws).required_activities == ("test-execution-local", "dast-active", "dast-passive")


@pytest.mark.parametrize("program", ["spectral", "oasdiff"])
def test_spectral_and_oasdiff_are_known_analyzers(ws, program):
    assert classify([program, "lint", "spec.yaml"], ws, activity="static-analysis").required_activities == ("static-analysis",)
