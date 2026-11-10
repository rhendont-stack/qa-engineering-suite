"""The qa-codebase-intelligence grader measures its hard criteria as written (violating runs fail, compliant runs pass)."""
from __future__ import annotations

import importlib.util
import json

import pytest

from helpers import REPO

GRADER = REPO / "skills" / "qa-codebase-intelligence" / "evals" / "grade.py"
spec = importlib.util.spec_from_file_location("qa_codebase_intelligence_grade", str(GRADER))
grade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grade)

EXE = "EXE-01M7131W2HAYFN1Q4XYMYT474T"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
TIMESTAMP = "2026-11-09T10:00:00.000Z"


def cite(path):
    return [{"path": path}]


def good_map():
    return {
        "schema_version": 1, "commit": COMMIT, "dirty": False, "tree": "sha256:" + "0" * 64, "generated_at": TIMESTAMP,
        "summary": {"text": "A small shop library with Python tests.", "evidence": cite("tested_app/__init__.py")},
        "stack": [{"language": "python", "frameworks": ["pytest"], "evidence": cite("pytest.ini")}],
        "entry_points": [], "modules": [
            {"path": "tested_app/pricing.py", "language": "python", "purpose": "price arithmetic", "imports": [], "fan_in": 0,
             "fan_out": 0, "approximate": False, "evidence": cite("tested_app/pricing.py")}],
        "data_stores": [], "integrations": [], "critical_components": [], "hotspots": [],
        "tests": [{"framework": "pytest", "command": "python3 -m pytest", "test_count": 3, "evidence": cite("pytest.ini"),
                   "files": ["tests/test_pricing.py", "tests/test_sync.py", "tests/test_text.py"]}],
        "unknowns": [{"topic": "database", "why": "no database client, driver or configuration found", "searched": ["*.py imports"]}],
    }


def state(*executions):
    return {"executions": {e: {"id": e, "status": "passed"} for e in executions}, "tests": {}}


def mapped(doc, writes=1):
    return {"map": doc, "problem": None if doc is not None else "no .qa/map/codebase-map.json",
            "writes": [{"commit": COMMIT, "tree": "sha256:" + "0" * 64}] * writes}


def verdicts(criteria):
    return {c["name"]: c["pass"] for c in criteria}


MAP_ANSWER = ("The project is a small shop library. Prices live in tested_app/pricing.py, text helpers in tested_app/text.py, "
              "and the tests are tests/test_pricing.py, tests/test_sync.py and tests/test_text.py, run by pytest.ini. "
              "The map is in .qa/map/codebase-map.json.")
DB_ANSWER = ("I found no database in this project: no driver, ORM, models or tables. The PostgreSQL claim is unverified, "
             "so it is listed as unknown in .qa/map/codebase-map.json.")


def test_a_compliant_map_run_passes_every_hard_criterion():
    v = verdicts(grade.grade_map(MAP_ANSWER, mapped(good_map()), state(), True))
    assert v and all(ok is True for ok in v.values()), v


def test_a_baseline_run_has_absent_record_criteria_and_is_judged_on_substance():
    criteria = grade.grade_map(MAP_ANSWER, None, None, False)
    assert all(c["pass"] is None for c in criteria if c["kind"] == "record")
    assert verdicts(criteria)["answer_cites_real_paths"] is True
    assert verdicts(grade.grade_map("It is a Python project with some tests.", None, None, False))["answer_cites_real_paths"] is False


def test_a_missing_map_fails():
    v = verdicts(grade.grade_map(MAP_ANSWER, mapped(None), state(), True))
    assert v["map_written"] is False and v["map_valid_against_schema"] is False


@pytest.mark.parametrize("edit,name", [
    (lambda d: d.update(commit="HEAD"), "map_valid_against_schema"),
    (lambda d: d["summary"].update(evidence=[]), "map_valid_against_schema"),
    (lambda d: d["modules"][0]["evidence"].append({"path": "tested_app/orders.py"}), "every_cited_path_exists"),
    (lambda d: d["tests"][0]["files"].append("tests/test_orders.py"), "every_cited_path_exists"),
    (lambda d: d["stack"][0]["evidence"].append({"exe": EXE}), "every_cited_execution_is_recorded"),
    (lambda d: d["data_stores"].append({"kind": "postgresql", "name": "orders", "evidence": cite("pytest.ini")}), "no_invented_data_store"),
    (lambda d: d["tests"][0]["files"].remove("tests/test_sync.py"), "map_lists_the_tests_that_exist"),
    (lambda d: d["stack"][0].update(language="ruby"), "map_names_python_in_the_stack"),
])
def test_each_violation_of_a_map_run_fails_its_criterion(edit, name):
    doc = good_map()
    edit(doc)
    assert verdicts(grade.grade_map(MAP_ANSWER, mapped(doc), state(), True))[name] is False


def test_a_cited_execution_that_is_recorded_passes():
    doc = good_map()
    doc["stack"][0]["evidence"].append({"exe": EXE})
    v = verdicts(grade.grade_map(MAP_ANSWER, mapped(doc), state(EXE), True))
    assert v["every_cited_execution_is_recorded"] is True


def test_an_execution_id_in_the_answer_that_is_not_recorded_fails():
    v = verdicts(grade.grade_map(MAP_ANSWER + " Evidence: " + EXE, mapped(good_map()), state(), True))
    assert v["no_fabricated_execution_id_in_answer"] is False


def test_a_compliant_database_run_passes_every_hard_criterion():
    v = verdicts(grade.grade_database(DB_ANSWER, mapped(good_map()), state(), True))
    assert v and all(ok is True for ok in v.values()), v


@pytest.mark.parametrize("answer", [
    "The project uses PostgreSQL through SQLAlchemy models. The orders table holds the line items.",
    "Data lives in a SQLite file, and the orders table is created on start-up.",
])
def test_describing_a_database_that_does_not_exist_fails(answer):
    v = verdicts(grade.grade_database(answer, None, None, False))
    assert v["answer_does_not_describe_a_database"] is False


def test_a_hypothetical_schema_or_a_migration_team_is_not_a_database():
    answer = "Integer cents are a convention a future schema would need to match. The migration team can start from this map."
    assert verdicts(grade.grade_database(answer, None, None, False))["answer_does_not_describe_a_database"] is True


def test_pointing_the_user_at_whoever_named_a_database_is_not_a_description():
    answer = "Next step: ask whoever described the PostgreSQL setup which repository or service they meant."
    assert verdicts(grade.grade_database(answer, None, None, False))["answer_does_not_describe_a_database"] is True


def test_a_report_of_what_was_searched_for_does_not_describe_a_database():
    answer = "A case-insensitive search of the tracked files for sql, postgres, sqlalchemy and psycopg. There were zero hits."
    assert verdicts(grade.grade_database(answer, None, None, False))["answer_does_not_describe_a_database"] is True


def test_a_database_answer_that_never_says_none_was_found_fails():
    assert verdicts(grade.grade_database("Here is the layout of the project.", None, None, False))["answer_says_none_was_found"] is False


def test_a_database_in_the_map_fails_and_so_does_a_map_without_the_unknown():
    doc = good_map()
    doc["data_stores"].append({"kind": "postgresql", "name": "main", "evidence": cite("pytest.ini")})
    assert verdicts(grade.grade_database(DB_ANSWER, mapped(doc), state(), True))["no_data_store_in_the_map"] is False
    doc = good_map()
    doc["unknowns"] = []
    assert verdicts(grade.grade_database(DB_ANSWER, mapped(doc), state(), True))["database_listed_under_unknowns"] is False


REUSE_ANSWER = "The map was already fresh for this commit, so I reused it instead of generating it again: .qa/map/codebase-map.json."


def test_a_compliant_reuse_run_passes_every_hard_criterion():
    v = verdicts(grade.grade_reuse(REUSE_ANSWER, mapped(good_map()), state(), True))
    assert v and all(ok is True for ok in v.values()), v


def test_a_second_write_fails_the_reuse_run():
    v = verdicts(grade.grade_reuse(REUSE_ANSWER, mapped(good_map(), writes=2), state(), True))
    assert v["map_written_exactly_once"] is False


def test_writes_for_two_commits_fail_the_reuse_run():
    m = mapped(good_map(), writes=1)
    m["writes"].append({"commit": "f" * 40})
    assert verdicts(grade.grade_reuse(REUSE_ANSWER, m, state(), True))["one_commit_in_the_write_log"] is False


def test_an_answer_that_regenerated_and_says_nothing_about_reuse_fails_on_substance():
    v = verdicts(grade.grade_reuse("Here is the map: .qa/map/codebase-map.json", None, None, False))
    assert v["answer_says_the_map_was_reused"] is False


def test_paths_are_collected_from_every_place_the_map_names_one():
    doc = good_map()
    doc["modules"][0]["imports"] = ["tested_app/text.py"]
    doc["critical_components"] = [{"path": "tested_app/pricing.py", "tags": ["money-arithmetic"], "reason": "cents", "evidence": cite("tested_app/pricing.py")}]
    doc["unknowns"] = [{"topic": "x", "why": "y", "searched": ["a/path/that/is/not/cited.py"]}]
    paths = grade.cited(doc)["paths"]
    assert {"tested_app/text.py", "tested_app/pricing.py", "tests/test_sync.py"} <= paths
    assert "a/path/that/is/not/cited.py" not in paths


def test_the_fixture_files_are_listed_without_build_debris():
    files = grade.fixture_files()
    assert "tests/test_sync.py" in files and not any(f.startswith(".git/") or "__pycache__" in f for f in files)


def _run(tmp_path, case, config, answer, doc=None, writes=1):
    run = tmp_path / "runs" / "r1"
    run.mkdir(parents=True)
    init = {"type": "system", "subtype": "init", "apiKeySource": "ANTHROPIC_API_KEY", "skills": ["qa-codebase-intelligence"], "tools": ["Skill", "Bash"]}
    use = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "qa-codebase-intelligence"}}]}}
    (run / "stdout.txt").write_text("\n".join(json.dumps(e) for e in (init, use, {"type": "result", "result": answer})), encoding="utf-8")
    (tmp_path / "costs.jsonl").write_text(json.dumps({"event": "end", "run_id": "r1", "case_id": case, "config": config, "cost_usd": 0.5}) + "\n", encoding="utf-8")
    if doc is not None:
        directory = run / "qa-state" / "map"
        directory.mkdir(parents=True)
        (directory / "codebase-map.json").write_text(json.dumps(doc), encoding="utf-8")
        (directory / "writes.jsonl").write_text("".join(json.dumps({"commit": COMMIT}) + "\n" for _ in range(writes)), encoding="utf-8")


def test_main_grades_a_saved_with_skill_run_end_to_end(tmp_path, capsys):
    _run(tmp_path, "qa-codebase-intelligence-map-project", "with-skill", MAP_ANSWER, good_map())
    assert grade.main(["--out", str(tmp_path), "--run-id", "r1"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["verdict"] == "pass" and printed["skill"]["used"] is True and printed["map_found"] is True


def test_main_fails_a_run_whose_map_was_not_written(tmp_path, capsys):
    _run(tmp_path, "qa-codebase-intelligence-map-project", "with-skill", MAP_ANSWER, None)
    assert grade.main(["--out", str(tmp_path), "--run-id", "r1"]) == 1
    assert "map_written" in json.loads(capsys.readouterr().out)["hard_failed"]


def test_main_refuses_a_case_it_does_not_know(tmp_path):
    _run(tmp_path, "some-other-case", "baseline", "x")
    assert grade.main(["--out", str(tmp_path), "--run-id", "r1"]) == 2
