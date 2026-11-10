"""The bundled scripts of qa-codebase-intelligence: hotspots, import graph, test inventory, and the map writer."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from helpers import FIXTURES, REPO

SCRIPTS = REPO / "skills" / "qa-codebase-intelligence" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "shared" / "kernel"))
import _repo  # noqa: E402
import git_hotspots  # noqa: E402
import import_graph  # noqa: E402
import inventory_tests  # noqa: E402
import write_map  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="tool missing: git")
EXE = "EXE-01M7131W2HAYFN1Q4XYMYT474T"


def git(root, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
                   + list(args), check=True, capture_output=True, env=env)


def put(root, files):
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def repo(tmp_path, files, commit=True):
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-q")
    put(root, files)
    if commit:
        git(root, "add", "-A")
        git(root, "commit", "-qm", "init")
    return root


def run(script, *args, stdin=None):
    done = subprocess.run([sys.executable, "-I", str(SCRIPTS / script)] + [str(a) for a in args], capture_output=True, text=True,
                          input=stdin, timeout=120)
    return done.returncode, (json.loads(done.stdout) if done.stdout.strip() else None), done.stderr


# ------------------------------------------------------------------------------------------------ _repo

def test_files_leave_out_the_workspace_agent_files_and_dependency_trees(tmp_path):
    root = repo(tmp_path, {"app.py": "x = 1\n", "node_modules/a/index.js": "1\n"}, commit=False)
    put(root, {".qa/config.json": "{}", ".claude/skills/s/SKILL.md": "x", "web/src/a.js": "1\n"})
    assert _repo.files(root) == ["app.py", "web/src/a.js"]


def test_files_of_a_directory_that_is_not_a_repository(tmp_path):
    put(tmp_path, {"a.py": "1\n", "sub/b.py": "2\n", "__pycache__/c.pyc": "x"})
    assert _repo.files(tmp_path) == ["a.py", "sub/b.py"]


def test_tree_state_is_clean_after_a_commit_and_ignores_the_workspace(tmp_path):
    root = repo(tmp_path, {"a.py": "1\n"})
    put(root, {".qa/map/x.json": "{}", ".claude/x": "y"})
    state = _repo.tree_state(root)
    assert state["dirty"] is False and len(state["commit"]) == 40 and state["tree"].startswith("sha256:")


def test_tree_state_changes_with_an_edit_and_returns_when_it_is_undone(tmp_path):
    root = repo(tmp_path, {"a.py": "1\n"})
    clean = _repo.tree_state(root)
    put(root, {"a.py": "2\n"})
    dirty = _repo.tree_state(root)
    assert dirty["dirty"] is True and dirty["tree"] != clean["tree"]
    put(root, {"a.py": "3\n"})
    assert _repo.tree_state(root)["tree"] != dirty["tree"]
    put(root, {"a.py": "1\n"})
    assert _repo.tree_state(root) == clean


def test_tree_state_outside_git_has_no_commit(tmp_path):
    put(tmp_path, {"a.py": "1\n"})
    state = _repo.tree_state(tmp_path)
    assert state["commit"] is None and state["dirty"] is False


# ------------------------------------------------------------------------------------------------ git_hotspots

def test_numstat_counts_each_commit_once_per_file_and_skips_binary_entries():
    log = "@@a\n3\t1\tx.py\n-\t-\timg.png\n@@b\n2\t0\tx.py\n1\t1\ty.py\n"
    assert git_hotspots.parse_numstat(log) == {"x.py": {"commits": 2, "lines_changed": 6}, "y.py": {"commits": 1, "lines_changed": 2}}


def test_complexity_counts_decision_points_plus_one():
    assert git_hotspots.complexity("x = 1\n") == 1
    assert git_hotspots.complexity("if a and b:\n  pass\nelif c or d:\n  pass\nfor i in x:\n  pass\n") == 6


def test_the_busy_complex_file_ranks_first_and_lockfiles_and_deleted_files_do_not_appear(tmp_path):
    root = repo(tmp_path, {"busy.py": "if a:\n    pass\n", "calm.py": "x = 1\n", "package-lock.json": "{}\n", "gone.py": "1\n"})
    for n in range(3):
        put(root, {"busy.py": "if a:\n    pass\n" * (n + 2), "package-lock.json": "{%d}\n" % n})
        git(root, "add", "-A")
        git(root, "commit", "-qm", "c%d" % n)
    git(root, "rm", "-q", "gone.py")
    git(root, "commit", "-qm", "rm")
    out = git_hotspots.hotspots(root)
    paths = [h["path"] for h in out["hotspots"]]
    assert paths[0] == "busy.py" and "package-lock.json" not in paths and "gone.py" not in paths
    first = out["hotspots"][0]
    assert first["commits"] == 4 and first["score"] == first["commits"] * first["complexity"] and out["commits_read"] == 5


def test_a_one_commit_history_is_said_to_rank_by_complexity_only(tmp_path):
    out = git_hotspots.hotspots(repo(tmp_path, {"a.py": "x = 1\n"}))
    assert any("1 commit" in line for line in out["limitations"])


def test_no_repository_gives_no_ranking_and_says_why(tmp_path):
    put(tmp_path, {"a.py": "x = 1\n"})
    out = git_hotspots.hotspots(tmp_path)
    assert out["hotspots"] == [] and out["commit"] is None and out["limitations"]


def test_hotspots_cli_prints_one_json_object_and_refuses_a_bad_root(tmp_path):
    root = repo(tmp_path, {"a.py": "x = 1\n"})
    code, body, _ = run("git_hotspots.py", "--root", root, "--top", 1)
    assert code == 0 and body["commit"] and len(body["hotspots"]) == 1
    assert run("git_hotspots.py", "--root", tmp_path / "missing")[0] == 2
    assert run("git_hotspots.py", "--root", root, "--top", 0)[0] == 2


# ------------------------------------------------------------------------------------------------ import_graph

def graph(tmp_path, files):
    root = repo(tmp_path, files)
    return root, {m["path"]: m for m in import_graph.analyse(root)["modules"]}


def test_python_absolute_relative_and_from_imports_resolve_to_repository_files(tmp_path):
    _, mods = graph(tmp_path, {
        "pkg/__init__.py": "", "pkg/a.py": "import os\nfrom . import b\nfrom .c import f\n", "pkg/b.py": "x = 1\n", "pkg/c.py": "def f(): pass\n",
        "app.py": "import pkg.a\nfrom pkg import b\nimport requests\n"})
    assert mods["pkg/a.py"]["imports"] == ["pkg/__init__.py", "pkg/b.py", "pkg/c.py"] and mods["pkg/a.py"]["external"] == ["os"]
    assert mods["app.py"]["imports"] == ["pkg/__init__.py", "pkg/a.py", "pkg/b.py"] and mods["app.py"]["external"] == ["requests"]
    assert mods["pkg/b.py"]["fan_in"] == 2 and mods["app.py"]["fan_out"] == 3 and mods["app.py"]["approximate"] is False


def test_python_in_a_src_layout_resolves_against_the_first_directory(tmp_path):
    _, mods = graph(tmp_path, {"src/shop/__init__.py": "", "src/shop/money.py": "x = 1\n", "src/shop/cart.py": "from shop import money\n"})
    assert mods["src/shop/cart.py"]["imports"] == ["src/shop/__init__.py", "src/shop/money.py"]


def test_a_python_file_that_does_not_parse_is_reported_not_described(tmp_path):
    root = repo(tmp_path, {"bad.py": "def (:\n", "ok.py": "x = 1\n"})
    out = import_graph.analyse(root)
    assert [m["path"] for m in out["modules"]] == ["ok.py"] and any("bad.py" in line for line in out["limitations"])


def test_javascript_relative_imports_resolve_and_packages_are_external_and_approximate(tmp_path):
    _, mods = graph(tmp_path, {
        "web/src/a.js": "import { b } from './b.js';\nimport axios from 'axios';\nconst fs = require('node:fs');\nconst u = require('./util');\n",
        "web/src/b.js": "export const b = 1;\n", "web/src/util/index.js": "module.exports = {};\n",
        "web/src/c.ts": "import x from '@scope/pkg/deep';\n"})
    assert mods["web/src/a.js"]["imports"] == ["web/src/b.js", "web/src/util/index.js"]
    assert mods["web/src/a.js"]["external"] == ["axios"] and mods["web/src/a.js"]["approximate"] is True
    assert mods["web/src/c.ts"]["external"] == ["@scope/pkg"]


def test_java_imports_resolve_by_class_path_and_others_stay_external(tmp_path):
    _, mods = graph(tmp_path, {
        "src/main/java/shop/Cart.java": "package shop;\nimport shop.money.Price;\nimport java.sql.Connection;\n",
        "src/main/java/shop/money/Price.java": "package shop.money;\npublic class Price {}\n"})
    cart = mods["src/main/java/shop/Cart.java"]
    assert cart["imports"] == ["src/main/java/shop/money/Price.java"] and cart["external"] == ["java.sql.Connection"]


def test_signals_name_the_file_and_line_of_drivers_clients_and_environment_variables(tmp_path):
    root = repo(tmp_path, {"db.py": "import os\nimport sqlite3\nimport psycopg2.extras\nURL = os.environ['PAYMENTS_URL']\nimport requests\n"})
    signals = {(s["kind"], s["name"], s["line"]) for s in import_graph.analyse(root)["signals"]}
    assert {("data-store", "sqlite", 2), ("data-store", "postgresql", 3), ("env-var", "PAYMENTS_URL", 4), ("http-client", "requests", 5)} <= signals


def test_a_project_without_any_data_store_import_has_no_such_signal(tmp_path):
    root = repo(tmp_path, {"a.py": "import re\nimport json\n", "web/x.js": "import fs from 'fs';\n"})
    assert not [s for s in import_graph.analyse(root)["signals"] if s["kind"] == "data-store"]


def test_entry_hints_find_main_blocks_routes_and_package_scripts(tmp_path):
    root = repo(tmp_path, {"cli.py": "def main():\n    pass\n\nif __name__ == '__main__':\n    main()\n",
                           "api.py": "@app.route('/orders')\ndef orders():\n    pass\n",
                           "web/package.json": json.dumps({"main": "index.js", "scripts": {"start": "node index.js"}}),
                           "Main.java": "class Main { public static void main(String[] a) {} }\n"})
    hints = {(h["kind"], h["path"]) for h in import_graph.analyse(root)["entry_hints"]}
    assert {("main", "cli.py"), ("route", "api.py"), ("main", "web/package.json"), ("main", "Main.java")} <= hints


def test_import_graph_cli_prints_the_four_keys(tmp_path):
    code, body, _ = run("import_graph.py", "--root", repo(tmp_path, {"a.py": "import re\n"}))
    assert code == 0 and set(body) == {"modules", "signals", "entry_hints", "limitations"}


# ------------------------------------------------------------------------------------------------ inventory_tests

def suites(tmp_path, files):
    root = repo(tmp_path, files)
    return {s["framework"]: s for s in inventory_tests.inventory(root)["suites"]}


def test_pytest_is_found_from_its_config_and_tests_are_counted(tmp_path):
    found = suites(tmp_path, {"pytest.ini": "[pytest]\n", "tests/test_a.py": "def test_x():\n    pass\n\nclass T:\n    def test_y(self):\n        pass\n",
                              "tests/helper.py": "def test_not_a_file_of_tests(): pass\n"})
    assert found["pytest"]["files"] == ["tests/test_a.py"] and found["pytest"]["test_count"] == 2
    assert found["pytest"]["command"] == "python3 -m pytest" and found["pytest"]["command_source"] == "inferred"


def test_unittest_is_recognised_when_there_is_no_pytest_config(tmp_path):
    found = suites(tmp_path, {"test_a.py": "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n"})
    assert found["unittest"]["command"] == "python3 -m unittest"


def test_javascript_framework_and_command_come_from_package_json(tmp_path):
    found = suites(tmp_path, {"web/package.json": json.dumps({"devDependencies": {"vitest": "3"}, "scripts": {"test": "vitest run"}}),
                              "web/test/p.test.js": "import { it } from 'vitest';\nit('a', () => {});\ntest('b', () => {});\n  it.skip('c', () => {});\n"})
    assert found["vitest"]["test_count"] == 3 and found["vitest"]["command"] == "cd web && npm test" and found["vitest"]["command_source"] == "declared"


def test_java_junit_and_the_gradle_wrapper_give_the_command(tmp_path):
    found = suites(tmp_path, {"jvm/build.gradle": "dependencies { testImplementation 'org.junit.jupiter:junit-jupiter' }\n", "jvm/gradlew": "#!/bin/sh\n",
                              "jvm/src/test/java/StockTest.java": "class StockTest {\n  @Test\n  void a() {}\n  @ParameterizedTest\n  void b() {}\n}\n"})
    assert found["junit"]["test_count"] == 2 and found["junit"]["command"] == "cd jvm && ./gradlew test"


def test_no_tests_is_reported_as_a_limitation_not_an_empty_success(tmp_path):
    root = repo(tmp_path, {"a.py": "x = 1\n"})
    out = inventory_tests.inventory(root)
    assert out["suites"] == [] and any("no test files" in line for line in out["limitations"])


# ------------------------------------------------------------------------------------------------ write_map

def finished(root, **over):
    """The draft of ``root`` completed the way an author would: a summary, purposes, and an unknown."""
    doc = write_map.draft(root)
    doc["summary"] = {"text": "A small project.", "evidence": [{"path": doc["modules"][0]["path"]}]}
    for module in doc["modules"]:
        module["purpose"] = "does one thing"
    doc["unknowns"] = [{"topic": "database", "why": "no driver or configuration found", "searched": ["imports"]}]
    doc.update(over)
    return doc


@pytest.fixture
def project(tmp_path):
    return repo(tmp_path, {"app/__init__.py": "", "app/core.py": "import re\n\ndef f(x):\n    return x\n", "tests/test_core.py": "def test_f():\n    pass\n",
                           "pytest.ini": "[pytest]\n"})


def test_status_without_a_map_says_no_map(project):
    body = write_map.status(project)
    assert body["fresh"] is False and body["reason"] == "no_map" and body["commit"]


def test_a_draft_carries_evidence_for_everything_it_lists(project):
    doc = write_map.draft(project)
    assert [m["path"] for m in doc["modules"]] == ["app/__init__.py", "app/core.py", "tests/test_core.py"]
    assert all(m["evidence"] for m in doc["modules"]) and doc["tests"][0]["files"] == ["tests/test_core.py"]
    assert {"_todo", "_signals", "_entry_hints", "_limitations"} <= set(doc) and doc["data_stores"] == []


def test_a_written_map_is_valid_pinned_fresh_and_logged_once(project):
    body = write_map.write(project, finished(project))
    saved = json.loads((project / ".qa/map/codebase-map.json").read_text())
    assert body["ok"] and saved["commit"] == _repo.head(project) and saved["dirty"] is False and not [k for k in saved if k.startswith("_")]
    assert write_map.schema.errors(saved, write_map.schema.load_schema("codebase-map.schema.json")) == []
    assert "app/core.py" in (project / ".qa/map/codebase-map.md").read_text()
    assert len((project / ".qa/map/writes.jsonl").read_text().splitlines()) == 1
    assert write_map.status(project)["fresh"] is True


def test_a_second_write_for_the_same_tree_is_refused_with_exit_3_and_changes_nothing(project):
    write_map.write(project, finished(project))
    before = (project / ".qa/map/codebase-map.json").read_text()
    with pytest.raises(write_map.MapError) as refused:
        write_map.write(project, finished(project))
    assert refused.value.code == "fresh_map_exists"
    assert (project / ".qa/map/codebase-map.json").read_text() == before
    assert len((project / ".qa/map/writes.jsonl").read_text().splitlines()) == 1


def test_force_rebuilds_and_the_log_shows_it(project):
    write_map.write(project, finished(project))
    write_map.write(project, finished(project), force=True)
    lines = [json.loads(line) for line in (project / ".qa/map/writes.jsonl").read_text().splitlines()]
    assert [line["forced"] for line in lines] == [False, True]


def test_the_map_is_stale_after_a_new_commit_and_after_an_uncommitted_edit(project):
    write_map.write(project, finished(project))
    put(project, {"app/core.py": "import re\n\ndef f(x):\n    return x + 1\n"})
    assert write_map.status(project)["reason"] == "tree_changed"
    git(project, "add", "-A")
    git(project, "commit", "-qm", "edit")
    assert write_map.status(project)["reason"] == "commit_changed"
    write_map.write(project, finished(project))
    assert write_map.status(project)["fresh"] is True


def test_cli_exit_codes_for_ok_refused_and_invalid(project):
    code, body, _ = run("write_map.py", "draft", "--root", project)
    assert code == 0
    body["summary"] = {"text": "A small project.", "evidence": [{"path": "app/core.py"}]}
    for module in body["modules"]:
        module["purpose"] = "p"
    assert run("write_map.py", "write", "--root", project, "--draft", "-", stdin=json.dumps(body))[0] == 0
    code, refused, _ = run("write_map.py", "write", "--root", project, "--draft", "-", stdin=json.dumps(body))
    assert code == 3 and refused["error"] == "fresh_map_exists" and refused["ok"] is False
    assert run("write_map.py", "validate", project / ".qa/map/codebase-map.json")[0] == 0
    assert run("write_map.py", "write", "--root", project, "--draft", "-", "--force", stdin="not json")[0] == 2


@pytest.mark.parametrize("edit,code", [
    (lambda d: d["modules"][0].update(purpose=""), "schema_invalid"),
    (lambda d: d["modules"][0].pop("evidence"), "schema_invalid"),
    (lambda d: d.pop("summary"), "schema_invalid"),
    (lambda d: d["modules"][0].update(evidence=[{"path": "../outside.py"}]), "schema_invalid"),
    (lambda d: d["modules"][0].update(evidence=[{"path": "app/missing.py"}]), "path_not_found"),
    (lambda d: d["tests"][0]["files"].append("tests/test_missing.py"), "path_not_found"),
    (lambda d: d["data_stores"].append({"kind": "postgresql", "name": "main", "evidence": [{"path": "db/schema.sql"}]}), "path_not_found"),
    (lambda d: d["integrations"].append({"kind": "http-api", "name": "bank", "evidence": [{"exe": EXE}]}), "execution_not_recorded"),
])
def test_a_map_that_breaks_a_rule_is_refused_and_nothing_is_written(project, edit, code):
    doc = finished(project)
    edit(doc)
    with pytest.raises(write_map.MapError) as refused:
        write_map.write(project, doc)
    assert refused.value.code == code and not (project / ".qa/map").exists()


def test_an_execution_that_is_in_the_ledger_may_be_cited(project):
    (project / ".qa/ledger").mkdir(parents=True)
    (project / ".qa/ledger/S-1.jsonl").write_text(json.dumps({"data": {"id": EXE}}) + "\n", encoding="utf-8")
    doc = finished(project)
    doc["integrations"].append({"kind": "http-api", "name": "bank", "evidence": [{"exe": EXE}]})
    assert write_map.write(project, doc)["ok"] is True


def test_a_map_outside_git_has_a_null_commit_and_still_validates(tmp_path):
    put(tmp_path, {"a.py": "x = 1\n", "tests/test_a.py": "def test_a():\n    pass\n"})
    write_map.write(tmp_path, finished(tmp_path))
    saved = json.loads((tmp_path / ".qa/map/codebase-map.json").read_text())
    assert saved["commit"] is None and write_map.status(tmp_path)["fresh"] is True


def test_validate_reports_the_errors_of_a_bad_map(tmp_path):
    (tmp_path / "m.json").write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    code, body, _ = run("write_map.py", "validate", tmp_path / "m.json")
    assert code == 2 and body["valid"] is False and body["errors"]


def test_the_fixture_maps_with_no_data_store_and_all_its_tests(tmp_path):
    root = tmp_path / "tested-app"
    shutil.copytree(str(FIXTURES / "tested-app"), str(root), ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "initial import")
    write_map.write(root, finished(root))
    saved = json.loads((root / ".qa/map/codebase-map.json").read_text())
    listed = {f for suite in saved["tests"] for f in suite["files"]}
    assert saved["data_stores"] == [] and {"tests/test_pricing.py", "tests/test_sync.py", "tests/test_text.py"} <= listed
    assert {s["language"] for s in saved["stack"]} == {"python", "javascript", "java"}


def test_the_markdown_copy_repeats_every_citation(project):
    doc = finished(project)
    doc["critical_components"] = [{"path": "app/core.py", "tags": ["other"], "reason": "the only logic", "evidence": [{"path": "app/core.py", "lines": "3-4"}]}]
    write_map.write(project, doc)
    text = (project / ".qa/map/codebase-map.md").read_text()
    assert "`app/core.py:3-4`" in text and "## Unknowns" in text and "database" in text


# ------------------------------------------------------------------------------------------------ descriptions and authoring flags

@pytest.mark.parametrize("language,text,expected", [
    ("python", '"""Price arithmetic. Amounts are cents.\n\nMore.\n"""\nx = 1\n', "Price arithmetic. Amounts are cents."),
    ("python", "x = 1\n", None),
    ("javascript", "// Price helpers for the storefront.\nexport const a = 1;\n", "Price helpers for the storefront."),
    ("javascript", "/*\n * Copyright 2020 Someone\n * Cart totals.\n */\nexport const a = 1;\n", "Cart totals."),
    ("java", "package a;\n\n/** How many units a shop holds. */\npublic final class Stock {}\n", "How many units a shop holds."),
    ("java", "package a;\npublic class A {}\n", None),
])
def test_the_first_description_line_of_a_module(language, text, expected):
    assert import_graph.first_description(text, language) == expected


def test_a_draft_fills_purposes_from_the_files_and_says_when_there_is_none(tmp_path):
    root = repo(tmp_path, {"a.py": '"""Does a."""\n', "b.py": "x = 1\n"})
    purposes = {m["path"]: m["purpose"] for m in write_map.draft(root)["modules"]}
    assert purposes == {"a.py": "Does a.", "b.py": "No description in the file."}


def test_authoring_flags_write_a_complete_map_without_any_json_on_the_command_line(project):
    code, body, _ = run("write_map.py", "write", "--root", project, "--summary", "A small project with one module.", "--cite", "app/core.py:1-4",
                        "--framework", "python|pytest|pytest.ini", "--entry", "library-api|f|app/core.py:3",
                        "--integration", "http-api|bank|app/core.py,app/__init__.py", "--critical", "app/core.py:3-4|money-arithmetic,other|sums amounts",
                        "--purpose", "app/core.py|the core function", "--unknown", "database|no driver or DSN found|imports;*.sql")
    assert code == 0 and body["ok"] is True
    saved = json.loads((project / ".qa/map/codebase-map.json").read_text())
    assert saved["summary"]["evidence"] == [{"path": "app/core.py", "lines": "1-4"}]
    assert next(s for s in saved["stack"] if s["language"] == "python")["frameworks"] == ["pytest"]
    assert saved["entry_points"][0]["path"] == "app/core.py" and saved["integrations"][0]["evidence"] == [{"path": "app/core.py"}, {"path": "app/__init__.py"}]
    assert saved["critical_components"][0]["tags"] == ["money-arithmetic", "other"] and saved["data_stores"] == []
    assert next(m for m in saved["modules"] if m["path"] == "app/core.py")["purpose"] == "the core function"
    assert saved["unknowns"] == [{"topic": "database", "why": "no driver or DSN found", "searched": ["imports", "*.sql"]}]


@pytest.mark.parametrize("flags,error", [
    (["--entry", "main|only two"], "bad_flag"),
    (["--purpose", "app/missing.py|text"], "bad_flag"),
    (["--framework", "ruby|rails|Gemfile"], "bad_flag"),
    (["--integration", "http-api|bank|"], "bad_flag"),
    (["--summary", "No evidence given."], "schema_invalid"),
    (["--summary", "Cites a missing file.", "--cite", "nowhere.py"], "path_not_found"),
])
def test_a_bad_flag_is_refused_with_a_reason_and_nothing_is_written(project, flags, error):
    code, body, _ = run("write_map.py", "write", "--root", project, *flags)
    assert code == 2 and body["error"] == error and not (project / ".qa/map").exists()


def test_flags_apply_on_top_of_a_draft_file(project, tmp_path):
    doc = write_map.draft(project)
    doc["summary"] = {"text": "From the file.", "evidence": [{"path": "app/core.py"}]}
    (tmp_path / "d.json").write_text(json.dumps(doc), encoding="utf-8")
    code, body, _ = run("write_map.py", "write", "--root", project, "--draft", tmp_path / "d.json", "--unknown", "queue|none found|imports")
    assert code == 0 and json.loads((project / ".qa/map/codebase-map.json").read_text())["unknowns"][0]["topic"] == "queue"


# ------------------------------------------------------------------------------------------------ hostile repositories (fix round 1)

def _commit_all(root):
    git(root, "add", "-A")
    git(root, "commit", "-qm", "more")


def _summary(*extra):
    return ["--summary", "A tiny project.", "--cite", "app/core.py", *extra]


def test_write_refuses_a_qa_directory_that_is_a_symlink(tmp_path):
    root = repo(tmp_path, {"app/core.py": "x = 1\n"}, commit=False)
    (tmp_path / "outside").mkdir()
    os.symlink("../outside", str(root / ".qa"))
    _commit_all(root)
    code, body, _ = run("write_map.py", "write", "--root", root, *_summary())
    assert code == 2 and body["error"] == "unsafe_path"
    assert list((tmp_path / "outside").iterdir()) == []


def test_write_refuses_a_map_directory_that_is_a_symlink(tmp_path):
    root = repo(tmp_path, {"app/core.py": "x = 1\n"}, commit=False)
    (tmp_path / "outside").mkdir()
    (root / ".qa").mkdir()
    os.symlink(str(tmp_path / "outside"), str(root / ".qa" / "map"))
    code, body, _ = run("write_map.py", "write", "--root", root, *_summary())
    assert code == 2 and body["error"] == "unsafe_path" and list((tmp_path / "outside").iterdir()) == []


@pytest.mark.parametrize("name", ["writes.jsonl", "codebase-map.json", "codebase-map.md"])
def test_write_refuses_to_append_or_write_through_a_planted_file_link(tmp_path, name):
    root = repo(tmp_path, {"app/core.py": "x = 1\n"})
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n", encoding="utf-8")
    (root / ".qa" / "map").mkdir(parents=True)
    os.symlink(str(victim), str(root / ".qa" / "map" / name))
    code, body, _ = run("write_map.py", "write", "--root", root, *_summary())
    assert code == 2 and body["error"] == "unsafe_path" and victim.read_text(encoding="utf-8") == "keep\n"
    assert sorted(p.name for p in (root / ".qa" / "map").iterdir()) == [name]


def test_a_hostile_javascript_file_of_the_size_limit_is_scanned_in_linear_time(tmp_path):
    chunk = 1 << 20
    root = repo(tmp_path, {"app/core.py": "x = 1\n", "a.js": ("import " * chunk)[:chunk - 8], "b.js": ("export " * chunk)[:chunk - 8],
                           "c.js": ("import {" * chunk)[:chunk - 8]})
    for script, args in (("import_graph.py", ["--root", root]), ("write_map.py", ["draft", "--root", root])):
        done = subprocess.run([sys.executable, "-I", str(SCRIPTS / script)] + [str(a) for a in args], capture_output=True, text=True, timeout=30)
        assert done.returncode == 0 and json.loads(done.stdout)


def test_the_schema_accepts_a_space_in_a_path_and_still_refuses_the_dangerous_forms():
    for good in ("app/my mod.py", "a b/c d.py", "..hidden/x.py", "a..b"):
        assert write_map.path_allowed(good), good
    for bad in ("../x.py", "a/../x.py", "/etc/passwd", "C:x.py", "c:/x.py", "a\\b.py", "a\nb.py", "a/.."):
        assert not write_map.path_allowed(bad), bad


def test_write_stays_possible_with_odd_file_names(tmp_path):
    root = repo(tmp_path, {"app/core.py": "def f(x):\n    return x\n", "app/my mod.py": "if True:\n    pass\n", "app/back\\slash.py": "x = 1\n"},
                commit=False)
    try:
        with open(os.path.join(os.fsencode(str(root)), b"app", b"bad-\xff.py"), "wb") as handle:
            handle.write(b"x = 2\n")
    except OSError:
        pytest.skip("this file system refuses non-UTF-8 names")
    _commit_all(root)
    doc = write_map.draft(root)
    paths = [m["path"] for m in doc["modules"]]
    assert "app/my mod.py" in paths and "app/back\\slash.py" not in paths and all("\\" not in p for p in paths)
    notes = " ".join(doc["_limitations"])
    assert "back" in notes and "schema" in notes and "UTF-8" in notes and "bad-" in notes
    code, body, _ = run("write_map.py", "write", "--root", root, *_summary())
    assert code == 0 and body["ok"] is True
    saved = json.loads((root / ".qa/map/codebase-map.json").read_text(encoding="utf-8"))
    assert "app/my mod.py" in [m["path"] for m in saved["modules"]]


def test_a_committed_symlink_is_not_read_through_by_hotspots_or_the_listing(tmp_path):
    secret = tmp_path / "secret.py"
    secret.write_text("if a and b or c:\n    pass\n" * 20, encoding="utf-8")
    root = repo(tmp_path, {"app/real.py": "if x:\n    pass\n"}, commit=False)
    os.symlink(str(secret), str(root / "app" / "link.py"))
    _commit_all(root)
    assert _repo.read_text(root, "app/link.py") is None and _repo.read_text(root, "app/real.py")
    assert [h["path"] for h in git_hotspots.hotspots(root)["hotspots"]] == ["app/real.py"]
    assert "app/link.py" not in _repo.files(root)


def test_the_tree_fingerprint_hashes_a_symlink_target_string_not_the_file_behind_it(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("one\n", encoding="utf-8")
    root = repo(tmp_path, {"app/core.py": "x = 1\n"})
    os.symlink(str(target), str(root / "link"))
    before = _repo.tree_state(root)["tree"]
    target.write_text("two, and longer\n", encoding="utf-8")
    assert _repo.tree_state(root)["tree"] == before
    os.remove(str(root / "link"))
    os.symlink(str(tmp_path / "elsewhere"), str(root / "link"))
    assert _repo.tree_state(root)["tree"] != before


def test_the_tree_fingerprint_does_not_hang_on_a_changed_path_that_is_a_fifo(tmp_path):
    root = repo(tmp_path, {"app/core.py": "x = 1\n", "app/pipe.py": "y = 1\n"})
    os.remove(str(root / "app" / "pipe.py"))
    os.mkfifo(str(root / "app" / "pipe.py"))
    script = "import sys; sys.path.insert(0, %r); import _repo; print(_repo.tree_state(%r)['dirty'])" % (str(SCRIPTS), str(root))
    done = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0 and done.stdout.strip() == "True"


def test_an_oversized_changed_file_is_fingerprinted_by_type_and_size(tmp_path, monkeypatch):
    root = repo(tmp_path, {"app/core.py": "x = 1\n", "big.txt": "a" * 64})
    monkeypatch.setattr(_repo, "HASH_CAP", 16)
    (root / "big.txt").write_text("b" * 64, encoding="utf-8")
    first = _repo.tree_state(root)["tree"]
    (root / "big.txt").write_text("c" * 64, encoding="utf-8")
    assert _repo.tree_state(root)["tree"] == first
    (root / "big.txt").write_text("c" * 65, encoding="utf-8")
    assert _repo.tree_state(root)["tree"] != first


def test_a_repository_config_cannot_run_a_program_through_the_read_only_git_calls(tmp_path):
    root = repo(tmp_path, {".gitattributes": "*.py filter=spy diff=spy\n", "app/core.py": "a = 1\n"})
    marks = tmp_path / "marks"
    marks.mkdir()
    programs = {"fsmonitor": "#!/bin/sh\ntouch '%s/fsmonitor'\n", "clean": "#!/bin/sh\ncat\ntouch '%s/clean'\n",
                "textconv": "#!/bin/sh\ncat \"$1\"\ntouch '%s/textconv'\n", "extdiff": "#!/bin/sh\ntouch '%s/extdiff'\n",
                "hook": "#!/bin/sh\ntouch '%s/hook'\n"}
    for name, body in programs.items():
        script = tmp_path / (name + ".sh")
        script.write_text(body % marks, encoding="utf-8")
        script.chmod(0o755)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "post-index-change").write_text(programs["hook"] % marks, encoding="utf-8")
    (hooks / "post-index-change").chmod(0o755)
    for key, value in (("core.fsmonitor", tmp_path / "fsmonitor.sh"), ("filter.spy.clean", tmp_path / "clean.sh"),
                       ("diff.spy.textconv", tmp_path / "textconv.sh"), ("diff.external", tmp_path / "extdiff.sh"),
                       ("core.hooksPath", hooks)):
        git(root, "config", key, str(value))
    (root / "app" / "core.py").write_text("a = 2\n", encoding="utf-8")  # same size: git must compare content
    assert _repo.tree_state(root)["dirty"] is True
    git_hotspots.hotspots(root)
    _repo.files(root)
    write_map.draft(root)
    assert sorted(p.name for p in marks.iterdir()) == []


# ------------------------------------------------------------------------------------------------ fix round 2

@pytest.mark.parametrize("name,line", [("a.js", 'import "'), ("b.java", "import a;\n"), ("c.go", 'import "x"\n'), ("d.js", "import 'x';\n"),
                                       ("e.go", 'import (\n"x"\n)\n')])
def test_a_hostile_file_full_of_import_matches_is_scanned_in_linear_time(tmp_path, name, line):
    limit = 1 << 20
    root = repo(tmp_path, {"app/core.py": "x = 1\n", name: (line * (limit // len(line)))[:limit - 8]})
    for script, args in (("import_graph.py", ["--root", root]), ("write_map.py", ["draft", "--root", root])):
        done = subprocess.run([sys.executable, "-I", str(SCRIPTS / script)] + [str(a) for a in args], capture_output=True, text=True, timeout=6)
        assert done.returncode == 0 and json.loads(done.stdout)


def test_line_numbers_of_signals_are_still_right(tmp_path):
    root = repo(tmp_path, {"a.js": "\n\nimport x from 'axios';\n", "B.java": "\nimport java.sql.Connection;\nclass B {}\n"})
    mods = {m["path"]: m for m in import_graph.analyse(root)["modules"]}
    sig = {(s["path"], s["line"]) for s in import_graph.analyse(root)["signals"]}
    assert ("a.js", 3) in sig and ("B.java", 2) in sig and set(mods) == {"a.js", "B.java"}


def _marks(tmp_path):
    marks = tmp_path / "marks"
    marks.mkdir()
    script = tmp_path / "spy.sh"
    script.write_text("#!/bin/sh\ncat\ntouch '%s/ran'\n" % marks, encoding="utf-8")
    script.chmod(0o755)
    return marks, script


def _exercise(root):
    _repo.tree_state(root)
    git_hotspots.hotspots(root)
    write_map.draft(root)
    run("write_map.py", "draft", "--root", root)


def test_a_filter_brought_in_by_an_include_does_not_run(tmp_path):
    marks, script = _marks(tmp_path)
    root = repo(tmp_path, {".gitattributes": "*.py filter=spy\n", "app/core.py": "a = 1\n"})
    (tmp_path / "extra.cfg").write_text('[filter "spy"]\n\tclean = %s\n' % script, encoding="utf-8")
    git(root, "config", "include.path", str(tmp_path / "extra.cfg"))
    (root / "app" / "core.py").write_text("a = 2\n", encoding="utf-8")
    _exercise(root)
    assert list(marks.iterdir()) == []


def test_a_filter_in_the_worktree_config_does_not_run(tmp_path):
    marks, script = _marks(tmp_path)
    root = repo(tmp_path, {".gitattributes": "*.py filter=spy\n", "app/core.py": "a = 1\n"})
    git(root, "config", "extensions.worktreeConfig", "true")
    git(root, "config", "--worktree", "filter.spy.clean", str(script))
    (root / "app" / "core.py").write_text("a = 2\n", encoding="utf-8")
    _exercise(root)
    assert list(marks.iterdir()) == []


def test_a_forged_signature_does_not_run_the_configured_gpg_program(tmp_path):
    marks, script = _marks(tmp_path)
    root = repo(tmp_path, {"app/core.py": "a = 1\n"})
    out = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%T %H"], capture_output=True, text=True, check=True).stdout.split()
    raw = ("tree %s\nparent %s\nauthor a <a@example.invalid> 1 +0000\ncommitter a <a@example.invalid> 1 +0000\n"
           "gpgsig -----BEGIN PGP SIGNATURE-----\n \n abc\n -----END PGP SIGNATURE-----\n\nsigned\n" % (out[0], out[1]))
    sha = subprocess.run(["git", "-C", str(root), "hash-object", "-t", "commit", "-w", "--stdin"], input=raw, capture_output=True, text=True,
                         check=True).stdout.strip()
    git(root, "update-ref", "HEAD", sha)
    git(root, "config", "log.showSignature", "true")
    git(root, "config", "gpg.program", str(script))
    (root / "app" / "core.py").write_text("a = 2\n", encoding="utf-8")
    _exercise(root)
    assert list(marks.iterdir()) == [] and git_hotspots.hotspots(root)["commits_read"] == 2


def test_git_is_not_run_when_the_config_cannot_be_listed(tmp_path):
    root = repo(tmp_path, {"app/core.py": "a = 1\n"})
    (root / ".git" / "config").write_text("[core\n broken\n", encoding="utf-8")
    del _repo.REFUSED[:]
    assert _repo.git(root, ["rev-parse", "HEAD"]) is None and _repo.REFUSED
    assert any("configuration" in n for n in write_map.draft(root)["_limitations"])
