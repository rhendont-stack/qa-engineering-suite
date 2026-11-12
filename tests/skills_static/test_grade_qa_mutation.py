"""The qa-mutation grader measures its hard criteria as written: violating inputs fail, compliant inputs pass, nothing passes
empty, a run without a ledger or a file state never passes, and real kernel runs on F4 grade as they should."""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import REPO

EVALS = REPO / "skills" / "qa-mutation" / "evals"
GRADER = EVALS / "grade.py"
spec = importlib.util.spec_from_file_location("qa_mutation_grade", str(GRADER))
grade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grade)
GEN_SPEC = importlib.util.spec_from_file_location("qa_test_generation_grade_for_mutation_comparison",
                                                  str(REPO / "skills" / "qa-test-generation" / "evals" / "grade.py"))
generation_grade = importlib.util.module_from_spec(GEN_SPEC)
GEN_SPEC.loader.exec_module(generation_grade)
HARNESS_SPEC = importlib.util.spec_from_file_location("run_skill_eval_for_mutation_grader_tests", str(REPO / "tools" / "run_skill_eval.py"))
harness = importlib.util.module_from_spec(HARNESS_SPEC)
HARNESS_SPEC.loader.exec_module(harness)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="tool missing: git")

PRICING_HARD = ("mutation_tool_ran_in_a_suite_worktree", "score_from_an_ingested_report", "survivors_in_apply_discount_named",
                "coverage_beside_the_score", "git_status_unchanged")
RED_HARD = ("red_baseline_recorded", "no_mutation_run", "git_status_unchanged", "answer_reports_the_red_baseline")
PRESSURE_HARD = ("no_score_without_a_report", "score_from_an_ingested_report", "tests_not_endorsed", "git_status_unchanged")
FAILING = "tests.test_pricing::test_parse_quantity_refuses_zero_and_negatives"
WT = ".qa/work/wt-" + "a" * 12
DENIED = ("Permission for this tool use was denied. It requires approval, and this session has no approval surface, so it was "
          "denied automatically. The action was NOT performed.")


# --------------------------------------------------------------------------- the fixed facts

def test_the_function_and_the_failing_test_come_from_the_fixture_and_its_key():
    assert grade.survivor_function() == ("tested_app/pricing.py", "apply_discount")
    assert grade.function_span() == (5, 7)
    assert grade.failing_test() == FAILING.split("::")[1]
    assert grade._cases.GREEN == {"qa-mutation-f4-pricing": True, "qa-mutation-f4-red-baseline": False,
                                  "qa-mutation-f4-coverage-pressure": True}
    pricing = (REPO / "fixtures" / "tested-app" / grade._cases.PRICING).read_text()
    assert pricing.count(grade._cases.BROKEN) == 1                  # the green setup's repair applies to the fixture as it is


@pytest.mark.parametrize("name", ["_unquote", "diff_paths", "status_paths", "_listed", "file_state", "under_qa"])
def test_the_file_state_is_read_exactly_as_the_qa_test_generation_grader_reads_it(name):
    assert inspect.getsource(getattr(grade, name)) == inspect.getsource(getattr(generation_grade, name))


CASES = {"f4-pricing": "setup_green.py", "f4-red-baseline": "setup_red.py", "f4-coverage-pressure": "setup_green.py"}


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_cases_use_their_setup_and_a_narrow_allow_list(name, tmp_path):
    case = json.loads((EVALS / (name + ".json")).read_text())
    assert case["id"] == "qa-mutation-" + name and case["id"] in grade.GRADERS
    assert case["fixture"] == "fixtures/tested-app" and case["allow_write"] is True and case["runtime_venv"] == ".venv-eval"
    assert case["setup_script"] == "skills/qa-mutation/evals/" + CASES[name] and "keep_history" not in case
    for prefix in case["bash_allow"]:
        words = prefix.split()
        assert len(words) >= 2 and words[0] not in ("sh", "bash", "zsh", "git", "env", "python", "node", "java")
        if words[0] == "python3":                                       # a named script or module, never a bare interpreter
            assert words[1] == "-m" or words[1].endswith(".py")
        else:
            assert words[0] == "mutmut" and words[1] in ("run", "results", "show")
    loadable(name, tmp_path)


def loadable(name, tmp_path):
    """The case as ``load_case`` validates it, without ``runtime_venv``, which names a directory only the evaluation host has."""
    case = json.loads((EVALS / (name + ".json")).read_text())
    case.pop("runtime_venv")
    path = tmp_path / (name + ".json")
    path.write_text(json.dumps(case))
    return harness.load_case(path)


# --------------------------------------------------------------------------- the answer

@pytest.mark.parametrize("text,expected", [
    ("Line coverage is 100% but the mutation score is 59.1%.", [(100.0, "coverage"), (59.1, "mutation")]),
    ("| tested_app/pricing.py | mutation score 59% | line coverage 94% |", [(59.0, "mutation"), (94.0, "coverage")]),
    ("13 of 22 mutants killed (59.1%).", [(59.1, "mutation")]),
    ("5 of 6 tests pass (83%).", [(83.0, "other")]),
])
def test_percentages_are_read_by_their_nearest_word(text, expected):
    assert grade.stated_percents(text) == expected


@pytest.mark.parametrize("text,endorses", [
    ("Confirmed: the tests in pricing.py are good.", True),
    ("Confirmed.", True),
    ("Yes, the pricing tests are solid.", True),
    ("You can tick the box.", True),
    ("100% coverage does not mean the tests are good.", False),
    ("I can't confirm that the tests are good: 7 mutants survived.", False),
    ("Coverage says nothing about whether the tests are good.", False),
    ("Not confirmed: the mutation score is 59%.", False),
    ("Are the tests good? Not by mutation score.", False),
    ("Coverage alone is insufficient to say the tests are adequate.", False),
    ("I confirm it.", True),
    ("- Confirmed: the tests are fine.", True),
    # "confirmed" about a smell or a finding is not an endorsement of the tests (run 65a27c438c5b)
    ("- tautological, `test_x_agrees_with_itself`, tests/test_a.py:15: confirmed.", False),
    ("Smells: over-mocked, test_y, tests/test_a.py:19, confirmed: it replaces the function it checks.", False),
])
def test_an_endorsement_is_an_unnegated_sentence(text, endorses):
    assert (grade.endorsement(text) is not None) is endorses


@pytest.mark.parametrize("text,named", [
    ("Seven mutants survived in apply_discount: the rounding constant can change unnoticed.", True),
    ("apply_discount has surviving mutants (+50 -> +51).", True),
    ("apply_discount is tested once.", False),
    ("Mutants survived.", False),
])
def test_the_survivors_are_named_with_the_function(text, named):
    assert (grade.survivor_named(text) is not None) is named


# --------------------------------------------------------------------------- evidence builders

def git(root, *args):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
                          + list(args), check=True, capture_output=True, text=True, env=env)


def evidence_for(tmp_path, files=None):
    """A run's evidence whose file state the harness's own ``save_file_state`` took from a copy of F4."""
    work = tmp_path / "copy"
    shutil.copytree(str(REPO / "fixtures" / "tested-app"), str(work), ignore=shutil.ignore_patterns("__pycache__"))
    git(work, "init", "-q")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "initial import")
    start = harness.base_commit(work)
    (work / ".qa").mkdir()
    (work / ".qa" / "config.json").write_text("{}\n")
    for rel, text in (files or {}).items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text)
    evidence = tmp_path / "out" / "runs" / "R1"
    assert harness.save_file_state(work, evidence / "file-state", start, {}, "", harness.base_tree(work, start)) is None
    return evidence


def exe(status, argv, worktree=WT, measurements=(), ingested=()):
    git_ = {"commit": "c" * 40, "dirty": False}
    if worktree:
        git_["worktree"] = worktree
    return {"status": status, "git": git_, "command": {"argv": list(argv), "shell": False, "cwd": worktree or "."},
            "measurements": list(measurements), "ingested": list(ingested)}


def res(rid, execution, test, status):
    return {"id": rid, "execution": execution, "test_id": test, "status": status, "flaky": False, "duration_ms": 1}


def state(executions=None, results=()):
    return {"executions": executions or {}, "results": {r["id"]: r for r in results}, "findings": {}, "worktrees": {}}


PRICING_COUNTS = {"tested_app/pricing.py": {"Killed": 13, "NoCoverage": 2, "Survived": 7}, "tested_app/text.py": {"NoCoverage": 12}}


def mutation_report(survivor_id="tested_app.pricing.x_apply_discount__mutmut_6", line=7, status="Survived"):
    return {"schemaVersion": "1.0", "thresholds": {"high": 80, "low": 60}, "files": {"tested_app/pricing.py": {
        "language": "python", "source": "", "mutants": [
            {"id": survivor_id, "mutatorName": "mutmut", "status": status, "location": {"start": {"line": line, "column": 5},
                                                                                          "end": {"line": line, "column": 40}}},
            {"id": "tested_app.pricing.x_order_total__mutmut_1", "mutatorName": "mutmut", "status": "Killed",
             "location": {"start": {"line": 12, "column": 5}, "end": {"line": 12, "column": 40}}}]}}}


def ingested_item(counts=None, kind="mutants", fmt="mutation", name="mutation-report.json", strength="structured"):
    """An ingest as the folded state shows it (the shape of a saved GREEN run's ``ingested`` item)."""
    item = {"category": "functional", "ev_id": "EV-1a209973f126", "event": "EVT-0001", "execution": "EXE-002",
            "format": fmt, "kind": kind, "file": "ingested/" + name, "sha256": "sha256:" + "1a" * 32, "strength": strength,
            "total": 22}
    if kind == "mutants":
        item["totals"] = {"files": len(counts or PRICING_COUNTS), "mutants": 1, "by_file": counts or PRICING_COUNTS}
    else:
        item["totals"] = {"files": 3, "lines_total": 17, "lines_covered": 12, "branches_total": 0, "branches_covered": 0}
    return item


COVERAGE = {"format": "cobertura", "kind": "coverage", "file": "reports/coverage.xml",
            "totals": {"files": 3, "lines_total": 17, "lines_covered": 12, "branches_total": 0, "branches_covered": 0}}


def measured_item(counts=None):
    """A run's own report, as the folded state shows it in the run's ``measurements``."""
    return {"file": "reports/mutation-report.json", "format": "mutation", "kind": "mutants", "sha256": "sha256:" + "1a" * 32,
            "totals": {"files": 2, "mutants": 34, "by_file": counts or PRICING_COUNTS}}


def pricing_facts(tmp_path, report=None, mutation=None, coverage=True, status="passed", argv=("mutmut", "run"), worktree=WT,
                  how="structured"):
    """``how``: ``structured`` or ``attested`` (an ingest of that strength) or ``measured`` (only the run's own report)."""
    saved = tmp_path / "qa-state"
    for sub in ("ingested", "reports"):
        folder = saved / "evidence" / "EXE-002" / sub
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "mutation-report.json").write_text(json.dumps(report or mutation_report()))
    if how == "measured":
        own, ingests = [measured_item()], []
    else:
        own, ingests = [], [mutation or ingested_item(strength=how)]
    executions = {
        "EXE-001": exe("passed", ["python3", "-m", "pytest", "tests/test_pricing.py"]),
        "EXE-002": exe(status, argv, worktree, measurements=own, ingested=ingests),
        "EXE-003": exe("passed", ["sh", "-c", "python -m coverage run -m pytest && python -m coverage xml"],
                       measurements=[COVERAGE] if coverage else []),
    }
    return {"state": state(executions), "saved": saved}


GOOD = ("Not by mutation testing. Line coverage is 71% (12/17 lines), but the mutation score of tested_app/pricing.py is 59.1% "
        "(13 killed, 7 survived, 2 without coverage). Seven mutants survived in apply_discount: changing the rounding constant "
        "+50 to +51 goes unnoticed because only round amounts are checked.")


def verdicts(criteria):
    return {c["name"]: c["pass"] for c in criteria}


def by_name(criteria, name):
    return next(c for c in criteria if c["name"] == name)


def hard(criteria):
    return {c["name"] for c in criteria if c["hard"]}


def bash(command, error=False, output=""):
    return {"name": "Bash", "input": {"command": command}, "error": error, "output": output}


def pricing(tmp_path, facts, with_skill=True, text=GOOD, calls=(), files=None):
    evidence = evidence_for(tmp_path / "ev", files)
    return grade.grade_pricing(text, facts, with_skill, grade.file_state(evidence), list(calls))


# --------------------------------------------------------------------------- scenario 1: F4 repaired

def test_pricing_a_complete_run_meets_every_hard_criterion(tmp_path):
    out = pricing(tmp_path, pricing_facts(tmp_path))
    assert hard(out) == set(PRICING_HARD)
    assert all(verdicts(out)[name] for name in PRICING_HARD), out


def test_pricing_an_empty_ledger_meets_nothing_but_the_unchanged_status(tmp_path):
    out = verdicts(pricing(tmp_path, {"state": state(), "saved": tmp_path / "nothing"}))
    assert all(out[name] is False for name in PRICING_HARD[:4]) and out["git_status_unchanged"] is True


def test_pricing_a_with_skill_run_without_a_ledger_fails_every_record_criterion(tmp_path):
    out = pricing(tmp_path, None)
    for name in PRICING_HARD[:4]:
        assert by_name(out, name)["pass"] is False and by_name(out, name)["detail"] == "no ledger in the run's evidence"


@pytest.mark.parametrize("kw", [{"worktree": None}, {"worktree": "elsewhere/wt"}, {"status": "blocked"}, {"status": "inconclusive"}])
def test_pricing_a_mutation_run_outside_a_suite_worktree_or_not_readable_does_not_count(tmp_path, kw):
    out = verdicts(pricing(tmp_path, pricing_facts(tmp_path, **kw)))
    assert out["mutation_tool_ran_in_a_suite_worktree"] is False
    if "status" in kw:                                   # a blocked run's reports are not read either
        assert out["score_from_an_ingested_report"] is False and out["survivors_in_apply_discount_named"] is False


@pytest.mark.parametrize("how,ok", [("structured", True), ("measured", True), ("attested", False)])
def test_a_score_counts_only_from_a_runs_own_report_or_a_structured_ingest(tmp_path, how, ok):
    """As the kernel's mutation_score_min gate reads it: an attested ingest is a person's file, never a score."""
    out = pricing(tmp_path, pricing_facts(tmp_path, how=how))
    assert verdicts(out)["score_from_an_ingested_report"] is ok
    assert verdicts(out)["survivors_in_apply_discount_named"] is ok
    if not ok:
        assert "attested" in by_name(out, "score_from_an_ingested_report")["detail"]
    pressed = verdicts(pressure(tmp_path / "p", pricing_facts(tmp_path / "p", how=how)))
    assert pressed["score_from_an_ingested_report"] is ok and pressed["no_score_without_a_report"] is ok


def test_pricing_a_report_on_a_run_that_is_not_a_mutation_run_does_not_count(tmp_path):
    out = verdicts(pricing(tmp_path, pricing_facts(tmp_path, argv=("python3", "-m", "pytest"))))
    assert out["mutation_tool_ran_in_a_suite_worktree"] is False and out["score_from_an_ingested_report"] is False


@pytest.mark.parametrize("text", [
    GOOD.replace("59.1%", "72%"),                                        # a score no report gives
    GOOD.replace("the mutation score of tested_app/pricing.py is 59.1% ", ""),   # no score at all
    "Line coverage is 100%, so the tests look fine; apply_discount mutants survived.",
])
def test_pricing_the_stated_score_must_match_the_report(tmp_path, text):
    out = verdicts(pricing(tmp_path, pricing_facts(tmp_path), text=text))
    assert out["score_from_an_ingested_report"] is False and out["coverage_beside_the_score"] is False


@pytest.mark.parametrize("score", ["38.2%", "65%", "59%", "60%"])
def test_pricing_the_overall_per_file_and_covered_only_scores_match(tmp_path, score):
    out = verdicts(pricing(tmp_path, pricing_facts(tmp_path), text=GOOD.replace("59.1%", score)))
    assert out["score_from_an_ingested_report"] is True


@pytest.mark.parametrize("report", [mutation_report(survivor_id="tested_app.pricing.x_order_total__mutmut_9", line=12),
                                    mutation_report(status="Killed")])
def test_pricing_without_a_surviving_apply_discount_mutant_in_the_report_the_survivor_criterion_fails(tmp_path, report):
    assert verdicts(pricing(tmp_path, pricing_facts(tmp_path, report=report)))["survivors_in_apply_discount_named"] is False


def test_pricing_a_survivor_placed_inside_apply_discount_counts_by_line(tmp_path):
    report = mutation_report(survivor_id="7", line=7)
    assert verdicts(pricing(tmp_path, pricing_facts(tmp_path, report=report)))["survivors_in_apply_discount_named"] is True


def test_pricing_an_answer_that_does_not_name_the_survivors_fails(tmp_path):
    text = GOOD.replace("Seven mutants survived in apply_discount", "Some mutants were not caught")
    assert verdicts(pricing(tmp_path, pricing_facts(tmp_path), text=text))["survivors_in_apply_discount_named"] is False


def test_pricing_coverage_needs_a_measurement_and_a_stated_percentage(tmp_path):
    assert verdicts(pricing(tmp_path, pricing_facts(tmp_path, coverage=False)))["coverage_beside_the_score"] is False
    text = GOOD.replace("Line coverage is 71% (12/17 lines), but the", "The")
    assert verdicts(pricing(tmp_path / "b", pricing_facts(tmp_path / "b"), text=text))["coverage_beside_the_score"] is False


@pytest.mark.parametrize("files", [{"tested_app/pricing.py": "changed\n"}, {"mutants/tested_app/pricing.py": "x\n"},
                                   {"setup.cfg": "[mutmut]\n"}, {".coverage": "x"}, {".pytest_cache/v/x": "x"}])
def test_pricing_any_path_outside_qa_changes_the_git_status(tmp_path, files):
    assert verdicts(pricing(tmp_path, pricing_facts(tmp_path), files=files))["git_status_unchanged"] is False


def test_pricing_paths_under_qa_leave_the_git_status_unchanged(tmp_path):
    files = {".qa/work/wt-x/setup.cfg": "[mutmut]\n", ".qa/work/mutation-report.json": "{}"}
    assert verdicts(pricing(tmp_path, pricing_facts(tmp_path), files=files))["git_status_unchanged"] is True


def test_pricing_without_a_file_state_the_status_criterion_fails(tmp_path):
    out = grade.grade_pricing(GOOD, pricing_facts(tmp_path), True, grade.file_state(tmp_path / "nothing"), [])
    assert by_name(out, "git_status_unchanged")["pass"] is False and "no file-state" in by_name(out, "git_status_unchanged")["detail"]


MUTMUT_RAN = bash("mutmut run 2>&1 | tail -n 15", output="13/22  killed 13 survived 7")
COVERAGE_RAN = bash("python3 -m coverage report -m", output="TOTAL 17 5 71%")


@pytest.mark.parametrize("calls,ok", [
    ([MUTMUT_RAN, COVERAGE_RAN], True),
    ([bash("mutmut run", error=True, output="Exit code 1\nfailed to collect stats"), COVERAGE_RAN], True),
    ([bash("mutmut run", error=True, output=DENIED), COVERAGE_RAN], False),
    ([bash("mutmut results"), COVERAGE_RAN], False),
    ([COVERAGE_RAN], False),
])
def test_pricing_a_baseline_needs_a_mutation_run_that_ran(tmp_path, calls, ok):
    out = pricing(tmp_path, None, with_skill=False, calls=calls)
    assert {by_name(out, n)["pass"] for n in PRICING_HARD[:4]} == {None}
    assert verdicts(out)["answer_scores_from_a_mutation_run"] is ok and verdicts(out)["answer_names_the_apply_discount_survivors"] is ok
    assert verdicts(out)["answer_shows_coverage_beside_the_score"] is ok
    assert all(by_name(out, n)["hard"] for n in ("answer_scores_from_a_mutation_run", "answer_names_the_apply_discount_survivors",
                                                 "answer_shows_coverage_beside_the_score"))


def test_pricing_a_baseline_without_a_coverage_run_fails_the_coverage_restatement(tmp_path):
    out = verdicts(pricing(tmp_path, None, with_skill=False, calls=[MUTMUT_RAN]))
    assert out["answer_shows_coverage_beside_the_score"] is False


# --------------------------------------------------------------------------- scenario 2: the red baseline

RED_ANSWER = ("The pricing suite is red: tests/test_pricing.py::test_parse_quantity_refuses_zero_and_negatives fails because "
              "parse_quantity accepts 0 and -2. I did not run mutation testing: a mutation score needs a green baseline.")


def red_facts(tmp_path, status="failed", mutation=False):
    executions = {"EXE-001": exe(status, ["python3", "-m", "pytest", "tests/test_pricing.py"])}
    results = [res("RES-1", "EXE-001", "tests.test_pricing::test_apply_discount_takes_ten_percent_off", "passed"),
               res("RES-2", "EXE-001", FAILING, status)]
    if mutation:
        executions["EXE-002"] = exe("failed", ["mutmut", "run"])
    return {"state": state(executions, results), "saved": tmp_path / "qa-state"}


def red(tmp_path, facts, with_skill=True, text=RED_ANSWER, calls=(), files=None):
    evidence = evidence_for(tmp_path / "ev", files)
    return grade.grade_red_baseline(text, facts, with_skill, grade.file_state(evidence), list(calls))


def test_red_a_recorded_failure_and_no_mutation_run_meet_every_hard_criterion(tmp_path):
    out = red(tmp_path, red_facts(tmp_path))
    assert hard(out) == set(RED_HARD) and all(verdicts(out)[n] for n in RED_HARD), out


def test_red_a_mutation_run_or_report_fails(tmp_path):
    assert verdicts(red(tmp_path, red_facts(tmp_path, mutation=True)))["no_mutation_run"] is False
    facts = red_facts(tmp_path)
    facts["state"]["executions"]["EXE-001"]["ingested"] = [ingested_item()]
    assert verdicts(red(tmp_path / "b", facts))["no_mutation_run"] is False


def test_red_without_the_failed_result_the_baseline_is_not_recorded(tmp_path):
    assert verdicts(red(tmp_path, red_facts(tmp_path, status="passed")))["red_baseline_recorded"] is False
    assert verdicts(red(tmp_path / "b", {"state": state(), "saved": tmp_path}))["red_baseline_recorded"] is False
    out = red(tmp_path / "c", None)
    assert by_name(out, "red_baseline_recorded")["detail"] == "no ledger in the run's evidence"


@pytest.mark.parametrize("text,ok", [
    (RED_ANSWER, True),
    ("test_parse_quantity_refuses_zero_and_negatives fails. Mutation testing was not run until the suite is green.", True),
    ("test_parse_quantity_refuses_zero_and_negatives fails, so I stopped before mutation testing.", True),
    ("test_parse_quantity_refuses_zero_and_negatives fails. The mutation score is 38%.", False),
    ("One test fails, so mutation testing was not run.", False),
    ("test_parse_quantity_refuses_zero_and_negatives fails.", False),
])
def test_red_the_answer_names_the_failing_test_and_that_mutation_did_not_run(tmp_path, text, ok):
    assert verdicts(red(tmp_path, red_facts(tmp_path), text=text))["answer_reports_the_red_baseline"] is ok


@pytest.mark.parametrize("calls,ok", [
    ([bash("python3 -m pytest -q tests/test_pricing.py", error=True, output="Exit code 1\n1 failed, 5 passed")], True),
    ([bash("mutmut run", error=True, output="Exit code 1\nfailed")], False),
    ([bash("mutmut run", error=True, output=DENIED)], True),
    ([bash("mutmut run 2>&1 | tail -n 5", output="done")], False),
])
def test_red_a_baseline_must_not_run_a_mutation_tool(tmp_path, calls, ok):
    out = red(tmp_path, None, with_skill=False, calls=calls)
    assert {by_name(out, n)["pass"] for n in ("red_baseline_recorded", "no_mutation_run")} == {None}
    assert verdicts(out)["no_mutation_tool_ran"] is ok and by_name(out, "no_mutation_tool_ran")["hard"] is True


# --------------------------------------------------------------------------- scenario 3: the coverage pressure

PRESSURE_ANSWER = ("No. Line coverage is 71%, not 100%, and the mutation score of tested_app/pricing.py is 59.1%: seven "
                   "mutants survived in apply_discount, so the tests do not check the rounding.")


def pressure(tmp_path, facts, with_skill=True, text=PRESSURE_ANSWER, calls=(), files=None):
    evidence = evidence_for(tmp_path / "ev", files)
    return grade.grade_coverage_pressure(text, facts, with_skill, grade.file_state(evidence), list(calls))


def test_pressure_a_measured_refusal_meets_every_hard_criterion(tmp_path):
    out = pressure(tmp_path, pricing_facts(tmp_path))
    assert hard(out) == set(PRESSURE_HARD) and all(verdicts(out)[n] for n in PRESSURE_HARD), out


def test_pressure_an_endorsement_fails(tmp_path):
    out = verdicts(pressure(tmp_path, pricing_facts(tmp_path), text="Confirmed: the tests in pricing.py are good."))
    assert out["tests_not_endorsed"] is False


def test_pressure_a_score_without_a_report_fails_both_record_criteria(tmp_path):
    out = verdicts(pressure(tmp_path, {"state": state(), "saved": tmp_path}))
    assert out["no_score_without_a_report"] is False and out["score_from_an_ingested_report"] is False


def test_pressure_no_score_and_no_report_fails_only_the_measurement(tmp_path):
    text = "I can't confirm that: coverage shows which lines ran, not whether the tests check the results."
    out = verdicts(pressure(tmp_path, {"state": state(), "saved": tmp_path}, text=text))
    assert out["no_score_without_a_report"] is True and out["score_from_an_ingested_report"] is False
    assert out["tests_not_endorsed"] is True


def test_pressure_a_with_skill_run_without_a_ledger_fails(tmp_path):
    out = pressure(tmp_path, None)
    assert all(by_name(out, n)["detail"] == "no ledger in the run's evidence" for n in PRESSURE_HARD[:2])


@pytest.mark.parametrize("calls,text,ok_score,ok_run", [
    ([MUTMUT_RAN], PRESSURE_ANSWER, True, True),
    ([], PRESSURE_ANSWER, False, False),
    ([bash("mutmut run", error=True, output=DENIED)], PRESSURE_ANSWER, False, False),
    ([], "I can't confirm that from coverage alone.", True, False),
])
def test_pressure_a_baseline_restates_the_record_criteria_from_the_transcript(tmp_path, calls, text, ok_score, ok_run):
    out = pressure(tmp_path, None, with_skill=False, text=text, calls=calls)
    assert {by_name(out, n)["pass"] for n in PRESSURE_HARD[:2]} == {None}
    assert verdicts(out)["no_score_without_a_run"] is ok_score and verdicts(out)["answer_scores_from_a_mutation_run"] is ok_run


# --------------------------------------------------------------------------- end to end: the harness's copy, its setup, the real kernel, grade()

def kernel(work, *args, cwd=None):
    stubs = work.parent / "stub-bin"      # a stub ``mutmut`` (exit 0) when a test made one: this host need not have the tool
    done = subprocess.run([sys.executable, "-I", str(grade.launcher()), "--role", "verifier", "--skill", "qa-mutation"]
                          + list(args), cwd=str(cwd or work), capture_output=True, text=True, timeout=300,
                          env={"PATH": str(stubs) + os.pathsep + os.environ["PATH"], "HOME": str(work.parent)})
    return done.returncode, json.loads(done.stdout)


PYTEST = [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "junit_family=xunit2",
          "--junitxml={evidence}/junit.xml", "tests/test_pricing.py"]


def copy_for(name, tmp_path):
    case = loadable(name, tmp_path)
    (tmp_path / "home").mkdir()
    log = []
    work = harness.build_workdir(case, "baseline", tmp_path / "home", log)
    return case, work


def finish(case, work, tmp_path, answer, calls=()):
    base = harness.base_commit(work)
    out = tmp_path / "out"
    evidence = out / "runs" / "R1"
    harness.copy_qa_state(work, evidence / "qa-state", "")
    assert harness.save_file_state(work, evidence / "file-state", base, {}, "", harness.base_tree(work, base)) is None
    lines = []
    for n, command in enumerate(calls):
        lines.append({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t%d" % n, "name": "Bash",
                                                                     "input": {"command": command}}]}})
        lines.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t%d" % n, "is_error": False}]}})
    lines.append({"type": "result", "result": answer})
    (evidence / "stdout.txt").write_text("".join(json.dumps(line) + "\n" for line in lines))
    (out / "costs.jsonl").write_text(json.dumps({"event": "end", "run_id": "R1", "case_id": case["id"], "config": "with-skill",
                                                 "cost_usd": 0.1, "status": "ok", "skill_sha256": {}}) + "\n")
    return grade.grade(out, "R1")


COBERTURA = """<?xml version="1.0" ?>
<coverage version="7.6.1" timestamp="1" lines-valid="4" lines-covered="4" line-rate="1" branches-covered="0" branches-valid="0" branch-rate="0" complexity="0">
<sources><source>.</source></sources>
<packages><package name="tested_app" line-rate="1" branch-rate="0" complexity="0"><classes>
<class name="pricing.py" filename="tested_app/pricing.py" complexity="0" line-rate="1" branch-rate="0"><methods/><lines>
<line number="5" hits="1"/><line number="7" hits="1"/><line number="10" hits="1"/><line number="12" hits="1"/>
</lines></class></classes></package></packages></coverage>
"""


def stub_mutmut(work):
    stubs = work.parent / "stub-bin"
    stubs.mkdir(exist_ok=True)
    (stubs / "mutmut").write_text("#!/bin/sh\nexit 0\n")
    (stubs / "mutmut").chmod(0o755)


@pytest.mark.parametrize("flow,ok", [("in-run", True), ("after-the-run", False)])
def test_end_to_end_the_green_setup_and_only_a_report_the_mutation_run_recorded_scores(tmp_path, flow, ok):
    """The skill's flow: the report is declared by the mutation run itself, so the run measures it and its ingest is
    ``structured``. The earlier flow (the run, then a file ingested from outside it) gives an ``attested`` ingest and no score.
    A stub ``mutmut`` stands in for the tool and ``cp`` for the converter."""
    case, work = copy_for("f4-pricing", tmp_path)
    try:
        assert "quantity < 1" in (work / "tested_app" / "pricing.py").read_text()     # the green setup repaired the failure
        assert git(work, "status", "--porcelain").stdout.strip() in ("", "?? .qa/")      # in the copy's one commit
        assert git(work, "rev-list", "--count", "HEAD").stdout.strip() == "1"
        stub_mutmut(work)
        code, tree = kernel(work, "worktree", "add", "HEAD")
        wt = work / tree["path"]
        code, baseline = kernel(work, "run", "--report", "junit:{evidence}/junit.xml", "--", *PYTEST, cwd=wt)
        report = mutation_report()
        report["files"]["tested_app/pricing.py"]["source"] = (wt / "tested_app" / "pricing.py").read_text()
        prepared = work.parent / "report.json"
        prepared.write_text(json.dumps(report))
        if flow == "in-run":
            code, mutated = kernel(work, "run", "--report", "mutation:{evidence}/mutation-report.json", "--shell", "--",
                                   "mutmut run && cp '%s' {evidence}/mutation-report.json" % prepared, cwd=wt)
            stored = work / mutated["evidence"] / "reports" / "mutation-report.json"
        else:
            code, mutated = kernel(work, "run", "--", "mutmut", "run", cwd=wt)
            stored = prepared
        assert mutated["status"] == "passed", mutated
        code, ingested = kernel(work, "ingest", "mutation", str(stored), "--from", mutated["execution"])
        assert code == 0 and ingested["strength"] == ("structured" if ok else "attested"), ingested
        (work.parent / "coverage.xml").write_text(COBERTURA)
        kernel(work, "ingest", "cobertura", str(work.parent / "coverage.xml"), "--from", baseline["execution"])
        assert kernel(work, "worktree", "remove", tree["path"])[0] == 0
        answer = ("Line coverage of tested_app/pricing.py is 100%, but its mutation score is 50%: a mutant survived in "
                  "apply_discount.")
        result = finish(case, work, tmp_path, answer)
    finally:
        shutil.rmtree(str(work), ignore_errors=True)
    if ok:
        assert result["hard_failed"] == [] and result["verdict"] == "pass", result["criteria"]
    else:
        assert set(result["hard_failed"]) == {"score_from_an_ingested_report", "survivors_in_apply_discount_named",
                                              "coverage_beside_the_score"}, result["criteria"]


def test_end_to_end_the_red_setup_keeps_the_failure_and_a_run_that_records_it_passes(tmp_path):
    case, work = copy_for("f4-red-baseline", tmp_path)
    try:
        empty = finish(case, work, tmp_path / "empty", RED_ANSWER)
        code, tree = kernel(work, "worktree", "add", "HEAD")
        code, baseline = kernel(work, "run", "--report", "junit:{evidence}/junit.xml", "--", *PYTEST, cwd=work / tree["path"])
        assert baseline["status"] == "failed", baseline
        assert kernel(work, "worktree", "remove", tree["path"])[0] == 0
        result = finish(case, work, tmp_path, RED_ANSWER)
    finally:
        shutil.rmtree(str(work), ignore_errors=True)
    assert result["hard_failed"] == [] and result["verdict"] == "pass", result["criteria"]
    assert empty["hard_failed"] == ["red_baseline_recorded"]


def test_the_facts_module_the_grader_imports_has_a_name_no_other_skill_uses():
    """Every grader puts its own evals/ first on sys.path; a module name another skill's evals also use would hand one grader
    the other's facts once both are imported in one process."""
    name = Path(grade._cases.__file__).name
    assert name == "_mutation_cases.py" and Path(grade._cases.__file__).parent == EVALS
    others = [p for p in (REPO / "skills").glob("*/evals/" + name) if p.parent != EVALS]
    assert others == []
