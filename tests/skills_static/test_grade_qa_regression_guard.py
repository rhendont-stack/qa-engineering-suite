"""The qa-regression-guard grader measures its hard criteria as written: violating inputs fail, compliant inputs pass, nothing
passes empty, a run without a ledger or a file state never passes, and real kernel runs on F7 grade as they should."""
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

EVALS = REPO / "skills" / "qa-regression-guard" / "evals"
GRADER = EVALS / "grade.py"
spec = importlib.util.spec_from_file_location("qa_regression_guard_grade", str(GRADER))
grade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grade)
GEN_SPEC = importlib.util.spec_from_file_location("qa_test_generation_grade_for_comparison",
                                                  str(REPO / "skills" / "qa-test-generation" / "evals" / "grade.py"))
generation_grade = importlib.util.module_from_spec(GEN_SPEC)
GEN_SPEC.loader.exec_module(generation_grade)
HARNESS_SPEC = importlib.util.spec_from_file_location("run_skill_eval_for_regression_grader_tests", str(REPO / "tools" / "run_skill_eval.py"))
harness = importlib.util.module_from_spec(HARNESS_SPEC)
HARNESS_SPEC.loader.exec_module(harness)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="tool missing: git")

FEATURE_HARD = ("base_run_in_a_suite_worktree", "head_run_recorded", "compare_shows_the_new_failure",
                "weakening_finding_cites_the_hunk", "no_project_file_changed")
FIX_HARD = ("revalidation_passed", "no_project_file_changed")
HOOK_HARD = ("hook_is_a_test_quality_finding", "test_integrity_not_met", "masked_failure_shown_by_a_run", "no_project_file_changed")
ORDER = grade.ORDER_TOTAL
WEAK_TEST = grade._cases.WEAKENED
OTHER = "tests.test_pricing.PricingTests::test_line_total"
C = None


def commits():
    global C
    if C is None:
        C = grade.commits()
    return C


# --------------------------------------------------------------------------- the fixed facts

def test_the_commits_the_weakened_line_and_the_hook_come_from_the_fixture_and_its_key():
    c = commits()
    assert len(c) == 6 and len(set(c)) == 6 and all(len(h) == 40 for h in c)
    assert grade.commits() == c                                   # deterministic
    path, match, line = grade.weakened()
    assert path == "tests/test_pricing.py" and "assertTrue" in match and line == 17
    assert grade.hook() == ("conftest.py", "pytest_runtest_makereport")
    assert grade._cases.CHECKOUT == {"qa-regression-guard-f7-feature-done": 1, "qa-regression-guard-f7-verify-fix": 3,
                                     "qa-regression-guard-f7-hook": 5}


@pytest.mark.parametrize("name", ["_unquote", "diff_paths", "status_paths", "_listed", "file_state", "under_qa"])
def test_the_file_state_is_read_exactly_as_the_qa_test_generation_grader_reads_it(name):
    assert inspect.getsource(getattr(grade, name)) == inspect.getsource(getattr(generation_grade, name))


@pytest.mark.parametrize("name", ["f7-feature-done", "f7-verify-fix", "f7-hook"])
def test_the_cases_use_the_history_a_narrow_allow_list_and_their_setup(name, tmp_path):
    case = json.loads((EVALS / (name + ".json")).read_text())
    assert case["id"] == "qa-regression-guard-" + name and case["id"] in grade.GRADERS
    assert case["keep_history"] is True and case["checkout"] == grade._cases.CHECKOUT[case["id"]]
    assert case["fixture"] == "fixtures/regression-repo" and case["allow_write"] is True and case["runtime_venv"] == ".venv-eval"
    assert case["setup_script"].endswith("setup_with_finding.py" if name == "f7-verify-fix" else "setup_trusted.py")
    for prefix in case["bash_allow"]:
        words = prefix.split()
        assert words[0] == "python3" and len(words) >= 2 and words[1] not in ("-c",)
        assert words[1] == "-m" or words[1].endswith(".py")             # a named script or module, never a bare interpreter
    loadable(name, tmp_path)                                            # the harness accepts it (its venv aside: not on every host)


def loadable(name, tmp_path):
    """The case as ``load_case`` validates it, without ``runtime_venv``, which names a directory only the evaluation host has."""
    case = json.loads((EVALS / (name + ".json")).read_text())
    case.pop("runtime_venv")
    path = tmp_path / (name + ".json")
    path.write_text(json.dumps(case))
    return harness.load_case(path)


# --------------------------------------------------------------------------- evidence builders

def git(root, *args):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
                          + list(args), check=True, capture_output=True, text=True, env=env)


def generated(tmp_path, checkout):
    work = tmp_path / ("copy-%d" % checkout)
    spec_ = importlib.util.spec_from_file_location("f7_generate_for_tests", str(grade.GENERATOR))
    module = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(module)
    module.generate_history(work, checkout=checkout)
    return work


def evidence_for(tmp_path, checkout, files=None, base=None):
    """A run's evidence whose file state the harness's own ``save_file_state`` took from a generated copy."""
    work = generated(tmp_path, checkout)
    start = harness.base_commit(work)
    for rel, text in (files or {}).items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text)
    evidence = tmp_path / "out" / "runs" / "R1"
    assert harness.save_file_state(work, evidence / "file-state", base or start, {}, "", harness.base_tree(work, base or start)) is None
    return evidence


def exe(commit, status, worktree=None):
    git_ = {"commit": commit, "dirty": False}
    if worktree:
        git_["worktree"] = worktree
    return {"status": status, "git": git_, "attempt_of": None}


def res(rid, execution, test, status, kind=None):
    return {"id": rid, "execution": execution, "test_id": test, "status": status, "failure_kind": kind if status == "failed" else None,
            "flaky": False, "duration_ms": 1}


def state(executions=None, results=(), findings=None, fixes=None, revalidations=None, gates=None):
    return {"executions": executions or {}, "results": {r["id"]: r for r in results}, "findings": findings or {}, "fixes": fixes or {},
            "revalidations": revalidations or {}, "gates": gates or {}, "bundles": {}, "worktrees": {}}


def bash(command, error=False, output=""):
    return {"name": "Bash", "input": {"command": command}, "error": error, "output": output}


WT0 = ".qa/work/wt-" + "a" * 12
LAUNCH = "python3 .claude/skills/qa-regression-guard/scripts/qa.py --role verifier --skill qa-regression-guard "
WEAK_FINDING = {"category": "test-quality", "state": "open", "title": "Assertion loosened in the commit that changed the function",
                "evidence": ["tests/test_pricing.py:17", "@@ -14,4 +14,9 @@ class PricingTests(unittest.TestCase):"],
                "component": {"path": "tests/test_pricing.py", "criticality": "normal"}, "refs": {"rule": "loosened-assertion"}}


def feature_facts():
    c = commits()
    executions = {"EXE-001": exe(c[0], "passed", WT0), "EXE-002": exe(c[1], "failed", ".qa/work/wt-" + c[1][:12])}
    results = [res("RES-1", "EXE-001", ORDER, "passed"), res("RES-2", "EXE-001", OTHER, "passed"),
               res("RES-3", "EXE-002", ORDER, "failed", "assertion"), res("RES-4", "EXE-002", OTHER, "passed")]
    return {"state": state(executions, results, findings={"FND-001": dict(WEAK_FINDING)}), "events": []}


FEATURE_CALLS = [bash(LAUNCH + "compare EXE-001 EXE-002")]


def verdicts(criteria):
    return {c["name"]: c["pass"] for c in criteria}


def by_name(criteria, name):
    return next(c for c in criteria if c["name"] == name)


def hard(criteria):
    return {c["name"] for c in criteria if c["hard"]}


def feature(tmp_path, f, with_skill=True, text="", calls=FEATURE_CALLS, **kw):
    evidence = evidence_for(tmp_path, 1, **kw)
    return grade.grade_feature_done(text, f, with_skill, grade.file_state(evidence), calls)


# --------------------------------------------------------------------------- F7 at C1

def test_feature_a_complete_run_meets_every_hard_criterion(tmp_path):
    out = feature(tmp_path, feature_facts())
    assert hard(out) == set(FEATURE_HARD)
    assert all(verdicts(out)[name] for name in FEATURE_HARD), out


def test_feature_an_empty_ledger_meets_nothing_but_the_unchanged_project(tmp_path):
    out = verdicts(feature(tmp_path, {"state": state(), "events": []}, calls=[]))
    assert all(out[name] is False for name in FEATURE_HARD if name != "no_project_file_changed")
    assert out["no_project_file_changed"] is True


def test_feature_a_with_skill_run_without_a_ledger_fails_every_record_criterion(tmp_path):
    out = feature(tmp_path, None)
    for name in FEATURE_HARD[:4]:
        assert by_name(out, name)["pass"] is False and by_name(out, name)["detail"] == "no ledger in the run's evidence"


def test_feature_a_base_run_outside_a_suite_worktree_or_not_at_c0_does_not_count(tmp_path):
    f = feature_facts()
    f["state"]["executions"]["EXE-001"]["git"].pop("worktree")
    assert verdicts(feature(tmp_path, f))["base_run_in_a_suite_worktree"] is False
    f = feature_facts()
    f["state"]["executions"]["EXE-001"]["git"]["commit"] = commits()[1]
    out = verdicts(feature(tmp_path / "b", f))
    assert out["base_run_in_a_suite_worktree"] is False and out["compare_shows_the_new_failure"] is False


def test_feature_a_base_run_where_the_order_total_did_not_pass_does_not_count(tmp_path):
    f = feature_facts()
    f["state"]["results"]["RES-1"] = res("RES-1", "EXE-001", ORDER, "failed", "assertion")
    f["state"]["executions"]["EXE-001"]["status"] = "failed"
    assert verdicts(feature(tmp_path, f))["base_run_in_a_suite_worktree"] is False


def test_feature_a_head_run_that_passed_the_order_total_does_not_count(tmp_path):
    f = feature_facts()
    f["state"]["results"]["RES-3"] = res("RES-3", "EXE-002", ORDER, "passed")
    f["state"]["executions"]["EXE-002"]["status"] = "passed"
    assert verdicts(feature(tmp_path, f))["head_run_recorded"] is False


@pytest.mark.parametrize("calls", [[], [bash(LAUNCH + "compare EXE-001 EXE-002", error=True)], [bash(LAUNCH + "state show")],
                                   [bash(LAUNCH + "compare EXE-002 EXE-001")], [bash("echo compare EXE-001 EXE-002")]])
def test_feature_compare_needs_a_successful_launcher_call_on_the_base_then_the_head(tmp_path, calls):
    assert verdicts(feature(tmp_path, feature_facts(), calls=calls))["compare_shows_the_new_failure"] is False


@pytest.mark.parametrize("change", [
    {"category": "functional"},
    {"evidence": ["tests/test_pricing.py:5"], "title": "Assertion loosened"},
    {"evidence": ["tests/test_service.py:17"], "component": {"path": "tests/test_service.py", "criticality": "normal"}},
    {"evidence": ["@@ -1,3 +1,3 @@"], "title": "x"},
])
def test_feature_a_finding_that_does_not_cite_the_weakened_hunk_fails(tmp_path, change):
    f = feature_facts()
    f["state"]["findings"]["FND-001"] = dict(WEAK_FINDING, **change)
    assert verdicts(feature(tmp_path, f))["weakening_finding_cites_the_hunk"] is False


@pytest.mark.parametrize("evidence", [["tests/test_pricing.py#L17"], ["tests/test_pricing.py", "+        self.assertTrue(apply_discount(999, 10))"],
                                      ["tests/test_pricing.py", "assertTrue( apply_discount(999,10) )"],
                                      ["tests/test_pricing.py", "@@ -16,2 +16,2 @@"]])
def test_feature_a_quote_a_line_or_a_covering_hunk_cites_it(evidence):
    record = {"category": "test-quality", "evidence": evidence}
    assert grade.cites_weakened_hunk(record) is True


def test_feature_a_changed_project_file_or_another_start_commit_fails_the_action_criterion(tmp_path):
    out = verdicts(feature(tmp_path, feature_facts(), files={"orders/pricing.py": "changed\n"}))
    assert out["no_project_file_changed"] is False
    out = feature(tmp_path / "b", feature_facts(), base=commits()[0])
    assert by_name(out, "no_project_file_changed")["pass"] is False and "not at the case's commit" in by_name(out, "no_project_file_changed")["detail"]


def test_feature_without_a_file_state_the_action_criterion_fails(tmp_path):
    fs = grade.file_state(tmp_path / "nothing")
    out = grade.grade_feature_done("", feature_facts(), True, fs, FEATURE_CALLS)
    assert verdicts(out)["no_project_file_changed"] is False and "no file-state snapshot" in by_name(out, "no_project_file_changed")["detail"]


def test_feature_a_baseline_is_judged_on_its_files_and_its_answer(tmp_path):
    good = ("Not ready to merge. tests.test_service.ServiceTests::test_payable_amount_after_ten_percent passed on the parent commit "
            "and now fails (899 != 900). The commit also weakened test_discount_on_an_odd_total: assertEqual became assertTrue, which passes for any non-zero amount.")
    out = feature(tmp_path, None, with_skill=False, text=good)
    assert all(by_name(out, n)["pass"] is None for n in FEATURE_HARD[:4])
    baseline_hard = ("no_project_file_changed", "answer_reports_the_new_failure", "answer_shows_the_base_run", "answer_names_the_weakened_assertion")
    assert hard(out) == set(baseline_hard) | set(FEATURE_HARD[:4])
    assert all(verdicts(out)[n] for n in baseline_hard)
    out = verdicts(feature(tmp_path / "b", None, with_skill=False, text="All tests pass; the feature is done and ready to merge."))
    assert out["answer_reports_the_new_failure"] is False and out["answer_names_the_weakened_assertion"] is False


@pytest.mark.parametrize("text,ok", [
    ("test_payable_amount_after_ten_percent fails; it is not ready to merge.", True),
    ("test_payable_amount_after_ten_percent fails, but otherwise it is ready to merge.", False),
    ("One test fails.", False),
])
def test_feature_the_new_failure_must_be_named_and_the_feature_not_called_done(tmp_path, text, ok):
    out = grade.grade_feature_done(text, None, False, grade.file_state(tmp_path), [])
    assert verdicts(out)["answer_reports_the_new_failure"] is ok


@pytest.mark.parametrize("text,ok", [
    ("It passed on the parent commit and fails now.", True),
    ("The test passes at HEAD~1 and fails at HEAD.", True),
    ("In the base run (C0, worktree) the order total test passed.", True),
    ("It passed before this commit.", True),
    ("For 999 cents the payable amount was 900 and is now 899; an existing test fails.", False),     # inferred, no base run
    ("1 test fails and 7 pass.", False),
])
def test_feature_the_base_run_must_be_stated(tmp_path, text, ok):
    out = grade.grade_feature_done(text, None, False, grade.file_state(tmp_path), [])
    assert verdicts(out)["answer_shows_the_base_run"] is ok and by_name(out, "answer_shows_the_base_run")["hard"] is True


def test_feature_the_substance_criteria_are_secondary_with_the_skill(tmp_path):
    out = feature(tmp_path, feature_facts(), text="")
    assert not by_name(out, "answer_reports_the_new_failure")["hard"] and not by_name(out, "tier_boundary_noticed")["hard"]


# --------------------------------------------------------------------------- F7 at C3

def fix_facts(outcome="passed", base_index=2, findings=("FND-001",)):
    c = commits()
    fixes = {"FIX-001": {"id": "FIX-001", "findings": list(findings), "base_commit": c[base_index], "fix_commit": c[3]}}
    revalidations = {"RVL-001": {"id": "RVL-001", "fix": "FIX-001", "outcome": outcome}}
    findings_ = {"FND-001": {"category": "functional", "state": "verified_fixed" if outcome == "passed" else "fix_submitted"}}
    return {"state": state(findings=findings_, fixes=fixes, revalidations=revalidations), "events": []}


def fix(tmp_path, f, with_skill=True, text="", **kw):
    evidence = evidence_for(tmp_path, 3, **kw)
    return grade.grade_verify_fix(text, f, with_skill, grade.file_state(evidence), [])


def test_fix_a_passed_revalidation_of_the_right_fix_meets_every_hard_criterion(tmp_path):
    out = fix(tmp_path, fix_facts())
    assert hard(out) == set(FIX_HARD) and all(verdicts(out)[n] for n in FIX_HARD), out


@pytest.mark.parametrize("kw", [{"outcome": "inconclusive"}, {"outcome": "failed"}, {"base_index": 1}, {"findings": ("FND-002",)}])
def test_fix_another_outcome_another_base_or_another_finding_fails(tmp_path, kw):
    assert verdicts(fix(tmp_path, fix_facts(**kw)))["revalidation_passed"] is False


def test_fix_no_revalidation_or_no_ledger_fails(tmp_path):
    assert verdicts(fix(tmp_path, {"state": state(), "events": []}))["revalidation_passed"] is False
    out = fix(tmp_path / "b", None)
    assert by_name(out, "revalidation_passed")["detail"] == "no ledger in the run's evidence"


@pytest.mark.parametrize("text,ok", [
    ("tests.test_service.ServiceTests::test_payable_amount_after_ten_percent failed on the base commit and passes on the fix.", True),
    ("Before the fix test_payable_amount_after_ten_percent failed (899 != 900); after the fix it passes.", True),
    ("All tests pass, so the fix is verified.", False),
    ("test_payable_amount_after_ten_percent passes now.", False),
])
def test_fix_a_baseline_must_show_fail_then_pass(tmp_path, text, ok):
    out = fix(tmp_path, None, with_skill=False, text=text)
    assert by_name(out, "revalidation_passed")["pass"] is None and verdicts(out)["answer_shows_fail_to_pass"] is ok
    assert by_name(out, "answer_shows_fail_to_pass")["hard"] is True


# --------------------------------------------------------------------------- F7 at C5

HOOK_FINDING = {"category": "test-quality", "state": "open", "title": "A conftest.py hook rewrites failed tests as passed",
                "component": {"path": "conftest.py", "criticality": "high"}, "refs": {"rule": "runner-hook", "location": "conftest.py"},
                "evidence": ["conftest.py:4"], "description": "pytest_runtest_makereport sets the outcome to passed"}


def gate_record(outcome="NOT_MET", evidence=("FND-001",), gid="GATE-001"):
    return {gid: {"id": gid, "verdict": "NOT_READY", "criteria": [{"id": "test_integrity", "type": "test_integrity", "outcome": outcome,
                                                                   "evidence": list(evidence)}]}}


def hook_facts(finding=None, gates=None, masked="failed"):
    c = commits()
    executions = {"EXE-001": exe(c[5], "passed"), "EXE-002": exe(c[5], "failed" if masked == "failed" else "passed")}
    results = [res("RES-1", "EXE-001", grade.LOYALTY, "passed"), res("RES-2", "EXE-002", grade.LOYALTY, masked, "assertion")]
    return {"state": state(executions, results, findings={"FND-001": dict(HOOK_FINDING, **(finding or {}))},
                           gates=gate_record() if gates is None else gates), "events": []}


NOCONFTEST = bash("python3 -m pytest -q -p no:cacheprovider --noconftest", output="F........\n1 failed, 8 passed in 0.05s")
NOCONFTEST_EXIT_1 = bash("python3 -m pytest -q -p no:cacheprovider --noconftest", error=True,
                         output="Exit code 1\nF........\nFAILED tests/test_service.py::ServiceTests::test_summary_lists_loyalty_points\n"
                                "1 failed, 8 passed in 0.05s")
DENIED = ("Permission for this tool use was denied. It requires approval, and this session has no approval surface, so it was "
          "denied automatically. The action was NOT performed.")


def hooked(tmp_path, f, with_skill=True, text="", calls=(), **kw):
    evidence = evidence_for(tmp_path, 5, **kw)
    return grade.grade_hook(text, f, with_skill, grade.file_state(evidence), list(calls))


def test_hook_a_finding_and_a_not_met_gate_meet_every_hard_criterion(tmp_path):
    out = hooked(tmp_path, hook_facts())
    assert hard(out) == set(HOOK_HARD) and all(verdicts(out)[n] for n in HOOK_HARD), out


@pytest.mark.parametrize("finding", [{"category": "functional"}, {"state": "wont_fix"}, {"state": "verified_fixed"},
                                     {"component": {"path": "tests/test_service.py", "criticality": "high"}, "refs": {"rule": "x"},
                                      "evidence": [], "title": "hook", "description": "pytest_runtest_makereport"}])
def test_hook_a_finding_of_another_kind_state_or_file_does_not_count(tmp_path, finding):
    out = verdicts(hooked(tmp_path, hook_facts(finding=finding)))
    assert out["hook_is_a_test_quality_finding"] is False and out["test_integrity_not_met"] is False


@pytest.mark.parametrize("gates", [{}, gate_record("MET", ()), gate_record("NOT_MET", ("FND-009",)),
                                   dict(gate_record(), **gate_record("MET", (), "GATE-002"))])
def test_hook_no_gate_a_met_one_or_one_not_citing_the_hook_fails(tmp_path, gates):
    assert verdicts(hooked(tmp_path, hook_facts(gates=gates)))["test_integrity_not_met"] is False


@pytest.mark.parametrize("masked", ["passed", None])
def test_hook_without_a_run_that_shows_the_masked_failure_fails(tmp_path, masked):
    f = hook_facts(masked=masked or "passed")
    if masked is None:
        f["state"]["executions"], f["state"]["results"] = {}, {}
    assert verdicts(hooked(tmp_path, f))["masked_failure_shown_by_a_run"] is False


def test_hook_a_masked_failure_at_another_commit_does_not_count(tmp_path):
    f = hook_facts()
    f["state"]["executions"]["EXE-002"]["git"]["commit"] = commits()[4]
    assert verdicts(hooked(tmp_path, f))["masked_failure_shown_by_a_run"] is False


@pytest.mark.parametrize("calls,ok", [
    ([NOCONFTEST], True),
    ([bash("python3 -m pytest -q --noconftest", error=True, output="denied")], False),
    # The harness's shapes, as the saved transcripts hold them: a command that exits non-zero comes back as an error whose text
    # starts with its exit status; a refused command comes back as an error that starts with the permission refusal.
    ([NOCONFTEST_EXIT_1], True),
    ([bash("python3 -m pytest -q --noconftest", error=True, output=DENIED)], False),
    ([bash("python3 -m pytest -q --noconftest", error=True, output=DENIED + "\n1 failed, 8 passed")], False),
    ([bash("python3 -m pytest -q --noconftest", error=True, output="Exit code 4\nERROR: usage: pytest [options]\nno tests ran")], False),
    ([bash("python3 -m pytest -q", error=True, output="Exit code 1\nF........\n1 failed, 8 passed in 0.05s")], False),
    ([bash("python3 -m pytest -q", output="9 passed in 0.04s")], False),
    ([bash("python3 -m pytest -q --noconftest", output="9 passed in 0.04s")], False),
    ([], False),
])
def test_hook_a_baseline_shows_the_masked_failure_only_by_running_without_the_hook(tmp_path, calls, ok):
    out = hooked(tmp_path, None, with_skill=False, calls=calls)
    assert verdicts(out)["masked_failure_shown_by_a_run"] is ok and by_name(out, "masked_failure_shown_by_a_run")["hard"] is True


def test_hook_deleting_the_conftest_is_a_project_change(tmp_path):
    work_files = {"conftest.py": "# emptied\n"}
    assert verdicts(hooked(tmp_path, hook_facts(), files=work_files))["no_project_file_changed"] is False


@pytest.mark.parametrize("text,ok", [
    ("Do not merge: conftest.py adds a pytest_runtest_makereport hook that rewrites every failure as a pass.", True),
    ("conftest.py turns failed tests into passed ones; not ready.", True),
    ("CI is green and the change is ready to merge.", False),
    ("The conftest.py hook rewrites failures as passes.", False),          # does not hold the merge back
])
def test_hook_a_baseline_must_name_the_hook_and_hold_the_merge(tmp_path, text, ok):
    out = hooked(tmp_path, None, with_skill=False, text=text)
    assert verdicts(out)["answer_flags_the_hook"] is ok and by_name(out, "answer_flags_the_hook")["hard"] is True
    assert {by_name(out, n)["pass"] for n in HOOK_HARD[:2]} == {None} and by_name(out, "masked_failure_shown_by_a_run")["pass"] is False


def test_hook_a_with_skill_run_without_a_ledger_fails(tmp_path):
    out = hooked(tmp_path, None)
    assert all(by_name(out, n)["detail"] == "no ledger in the run's evidence" for n in HOOK_HARD[:3])


# --------------------------------------------------------------------------- end to end: the harness's copy, the real kernel, grade()

def kernel(work, *args, cwd=None):
    done = subprocess.run([sys.executable, "-I", str(grade.launcher()), "--role", "verifier", "--skill", "qa-regression-guard"]
                          + list(args), cwd=str(cwd or work), capture_output=True, text=True, timeout=300,
                          env={"PATH": os.environ["PATH"], "HOME": str(work.parent)})
    return done.returncode, json.loads(done.stdout)


PYTEST = ["--", sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "junit_family=xunit2",
          "--junitxml={evidence}/junit.xml"]


def run_at(work, commit):
    code, out = kernel(work, "worktree", "add", commit)
    assert code == 0, out
    code, out = kernel(work, "run", "--report", "junit:{evidence}/junit.xml", *PYTEST, cwd=work / out["path"])
    assert "error" not in out and out["status"] in ("passed", "failed"), out     # a failed run exits 1, so its ok is false
    return out["execution"]


def copy_for(name, tmp_path):
    case = loadable(name, tmp_path)
    (tmp_path / "home").mkdir()
    work = harness.build_workdir(case, "baseline", tmp_path / "home", [])
    return case, work


def finish(case, work, tmp_path, calls, answer="done"):
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


def add_finding(work, record):
    path = work.parent / "finding.json"
    path.write_text(json.dumps(record))
    code, out = kernel(work, "finding", "add", "--json", str(path))
    assert code == 0, out
    return out["id"]


def test_end_to_end_the_feature_case(tmp_path):
    case, work = copy_for("f7-feature-done", tmp_path)
    try:
        c = commits()
        base, head = run_at(work, c[0]), run_at(work, c[1])
        assert kernel(work, "compare", base, head)[1]["new_failures"] == [ORDER]
        add_finding(work, {"title": "Exact-amount assertion loosened to a truth check", "description": "The commit that changed the "
                           "function replaced an equality assertion with a truth test.", "category": "test-quality",
                           "severity_rationale": {"anchor": "FUNC-M1", "modifiers": []},
                           "component": {"path": "tests/test_pricing.py", "criticality": "normal"}, "verification_status": "suspected",
                           "evidence": [head, "tests/test_pricing.py:17", "@@ -14,4 +14,9 @@"], "reproduction_steps": [],
                           "refs": {"rule": "loosened-assertion", "location": "tests/test_pricing.py"}})
        result = finish(case, work, tmp_path, [LAUNCH + "compare %s %s" % (base, head)])
    finally:
        shutil.rmtree(str(work), ignore_errors=True)
    assert result["hard_failed"] == [] and result["verdict"] == "pass", result["criteria"]


def test_end_to_end_the_fix_case(tmp_path):
    case, work = copy_for("f7-verify-fix", tmp_path)
    try:
        c = commits()
        fix_file = work.parent / "fix.json"
        fix_file.write_text(json.dumps({"findings": ["FND-001"], "base_commit": c[2], "fix_commit": c[3], "description": "rounding"}))
        code, out = kernel(work, "fix", "add", "--json", str(fix_file))
        assert code == 0, out
        pair = "%s:%s" % (run_at(work, c[2]), run_at(work, c[3]))
        code, reval = kernel(work, "revalidate", out["id"], "--f2p", pair, "--p2p", pair, "--defect-test", ORDER)
        assert reval["outcome"] == "passed", reval
        result = finish(case, work, tmp_path, [])
    finally:
        shutil.rmtree(str(work), ignore_errors=True)
    assert result["hard_failed"] == [] and result["verdict"] == "pass", result["criteria"]


def test_end_to_end_the_hook_case_and_a_run_that_records_nothing(tmp_path):
    case, work = copy_for("f7-hook", tmp_path)
    try:
        empty = finish(case, work, tmp_path / "empty", [])
        code, masked = kernel(work, "run", "--report", "junit:{evidence}/junit.xml", *(PYTEST + ["--noconftest"]))
        assert masked["status"] == "failed", masked
        fid = add_finding(work, {"title": "A test hook rewrites failures as passes", "description": "conftest.py adds "
                                 "pytest_runtest_makereport and sets a failed call report to passed.", "category": "test-quality",
                                 "severity_rationale": {"anchor": "FUNC-H1", "modifiers": []},
                                 "component": {"path": "conftest.py", "criticality": "high"}, "verification_status": "suspected",
                                 "evidence": ["conftest.py:4"], "reproduction_steps": [],
                                 "refs": {"rule": "runner-hook", "location": "conftest.py"}})
        code, gate_out = kernel(work, "gate", "evaluate", "--profile", "change")
        assert "error" not in gate_out and gate_out["verdict"] == "NOT_READY", gate_out   # NOT_READY exits nonzero
        result = finish(case, work, tmp_path, [])
    finally:
        shutil.rmtree(str(work), ignore_errors=True)
    assert result["hard_failed"] == [] and result["verdict"] == "pass", result["criteria"]
    assert fid in by_name(result["criteria"], "test_integrity_not_met")["detail"]
    assert set(empty["hard_failed"]) == {"hook_is_a_test_quality_finding", "test_integrity_not_met", "masked_failure_shown_by_a_run"}
