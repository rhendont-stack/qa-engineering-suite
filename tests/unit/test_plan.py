from __future__ import annotations

import copy
import json
import shutil
import subprocess

import pytest

import helpers
from helpers import WRITE_JUNIT, req
from qa import execution, gate, ledger, plan, state
from qa.errors import QaError

SKILL = "qa-test-execution"


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                   check=True, capture_output=True)


def _head(root):
    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(trusted):
    if shutil.which("git") is None:
        pytest.skip("tool missing: git")   # the Linux images ship no git
    _git(trusted.root, "init", "-q", "-b", "main")
    (trusted.root / "a.txt").write_text("1")
    _git(trusted.root, "add", "a.txt")
    _git(trusted.root, "commit", "-q", "-m", "c1")
    return trusted


def one_step_template(**over):
    step = {"id": "S01", "skill": SKILL, "depends_on": [], "requires_authorization": ["test-execution-local"],
            "expected_outputs": [{"kind": "execution", "skill": SKILL}, {"kind": "result_set", "skill": SKILL}],
            "exclusive": False}
    step.update(over)
    return {"mode": "audit", "steps": [step]}


def _step(ws, step_id="S01", **kw):
    return next(s for s in plan.status(ws, **kw)["steps"] if s["id"] == step_id)


def _run(ws, actor, py, outcome="pass"):
    return execution.execute(ws, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", outcome))["execution"]


def _refused(code, call, *args, **kw):
    with pytest.raises(QaError) as caught:
        call(*args, **kw)
    assert caught.value.code == code
    return caught.value


# --------------------------------------------------------------------------- the brief's tests

def test_step_without_outputs_stays_pending(repo, actor):
    p = plan.create(repo, actor, one_step_template())
    assert p["data"]["id"] == "PLAN-0001"
    assert plan.status(repo)["steps"][0]["status"] == "pending"


def test_step_with_valid_outputs_is_done(repo, actor, py):
    plan.create(repo, actor, one_step_template())
    _run(repo, actor, py)  # a recorded execution by the named skill at the frozen commit, with its result
    done = _step(repo)
    assert done["status"] == "done" and all(o["met"] for o in done["outputs"])
    assert state.load(repo)["plans"]["PLAN-0001"]["steps"][0].get("status") is None  # computed, never stored


def test_a_failed_run_still_counts_as_a_recorded_output(repo, actor, py):
    plan.create(repo, actor, one_step_template())
    _run(repo, actor, py, "fail")
    assert _step(repo)["status"] == "done"


def test_output_from_another_commit_does_not_count(repo, actor, py):
    plan.create(repo, actor, one_step_template())
    (repo.root / "b.txt").write_text("2")
    _git(repo.root, "add", "b.txt")
    _git(repo.root, "commit", "-q", "-m", "c2")
    _run(repo, actor, py)
    step = _step(repo)
    assert step["status"] == "pending" and not any(o["met"] for o in step["outputs"])


def test_output_from_another_skill_does_not_count(repo, actor, py):
    plan.create(repo, actor, one_step_template())
    other = ledger.Actor(role="verifier", skill="qa-security", session=actor.session, independence="I1")
    _run(repo, other, py)
    assert _step(repo)["status"] == "pending"


def test_a_blocked_run_is_not_an_output(trusted, actor):
    plan.create(trusted, actor, one_step_template(expected_outputs=[{"kind": "execution", "skill": SKILL}]))
    execution.execute(trusted, actor, execution.RunRequest(command=["git", "push"]))  # denied by the guard
    assert _step(trusted)["status"] == "pending"


def test_a_step_needs_every_expected_output(repo, actor):
    plan.create(repo, actor, one_step_template(expected_outputs=[{"kind": "execution", "skill": SKILL, "min_count": 2}]))
    assert _step(repo)["status"] == "pending"


def test_min_count_is_counted(repo, actor, py):
    plan.create(repo, actor, one_step_template(expected_outputs=[{"kind": "execution", "skill": SKILL, "min_count": 2}]))
    _run(repo, actor, py)
    assert _step(repo)["status"] == "pending"
    _run(repo, actor, py)
    assert _step(repo)["status"] == "done"


def test_result_status_not_in_filters_results(repo, actor, py):
    outputs = [{"kind": "result_set", "skill": SKILL, "status_not_in": ["failed"]}]
    plan.create(repo, actor, one_step_template(expected_outputs=outputs))
    _run(repo, actor, py, "fail")
    assert _step(repo)["status"] == "pending"
    _run(repo, actor, py, "pass")
    assert _step(repo)["status"] == "done"


def test_the_target_is_frozen_at_creation(repo, actor):
    event = plan.create(repo, actor, one_step_template())
    commit = _head(repo.root)
    assert event["data"]["target"] == {"commit": commit, "dirty": False}
    assert {o["commit"] for o in event["data"]["steps"][0]["expected_outputs"]} == {commit}


def test_ids_count_up_in_four_digits(repo, actor):
    plan.create(repo, actor, one_step_template())
    second = plan.create(repo, actor, one_step_template())
    assert second["data"]["id"] == "PLAN-0002" and plan.status(repo)["id"] == "PLAN-0002"
    assert plan.status(repo, "PLAN-0001")["id"] == "PLAN-0001"
    _refused("unknown_plan", plan.status, repo, "PLAN-0009")


def test_status_without_a_plan_answers_with_none(ws, qa_cli):
    assert plan.status(ws) == {"id": None, "mode": None, "target": None, "steps": [],
                               "counts": {"pending": 0, "done": 0, "blocked": 0, "not_run": 0}}
    code, out = qa_cli("plan", "status")
    assert code == 0 and "no_plan" in out["limitations"]


def test_a_workspace_outside_git_gets_a_plan_with_a_null_commit(trusted, actor, py):
    event = plan.create(trusted, actor, one_step_template())
    assert event["data"]["target"] == {"commit": None, "dirty": None}
    assert _step(trusted)["status"] == "pending"
    _run(trusted, actor, py)
    assert _step(trusted)["status"] == "done"


# --------------------------------------------------------------------------- holds

def test_done_cannot_be_set_by_hand(repo, actor):
    plan.create(repo, actor, one_step_template())
    _refused("status_not_settable", plan.update, repo, actor, "PLAN-0001", "S01", "done", "x")
    _refused("status_not_settable", plan.update, repo, actor, "PLAN-0001", "S01", "passed", "x")
    assert _step(repo)["status"] == "pending"


def test_done_cannot_be_set_by_hand_from_the_cli(repo, actor, qa_cli):
    plan.create(repo, actor, one_step_template())
    code, out = qa_cli("plan", "update", "PLAN-0001", "S01", "--status", "done", "--reason", "x")
    assert code != 0 and out["ok"] is False


def test_blocked_keeps_its_reason_across_rebuild(repo, actor):
    plan.create(repo, actor, one_step_template())
    plan.update(repo, actor, "PLAN-0001", "S01", "blocked", "the user denied the authorization")
    state.rebuild(repo)
    step = _step(repo)
    assert (step["status"], step["reason"]) == ("blocked", "the user denied the authorization")
    assert json.loads(repo.path("state", "plans.json").read_text())["PLAN-0001"]["steps"][0]["hold"]["reason"]


def test_not_run_keeps_the_users_reason(repo, actor, py):
    plan.create(repo, actor, one_step_template())
    plan.update(repo, actor, "PLAN-0001", "S01", "not_run", "descoped by the user")
    _run(repo, actor, py)  # evidence arriving later does not undo the user's decision
    step = _step(repo)
    assert (step["status"], step["reason"]) == ("not_run", "descoped by the user")


def test_outputs_overrule_a_block(repo, actor, py):
    plan.create(repo, actor, one_step_template())
    plan.update(repo, actor, "PLAN-0001", "S01", "blocked", "waiting")
    _run(repo, actor, py)
    assert _step(repo)["status"] == "done"


def test_pending_releases_a_hold(repo, actor):
    plan.create(repo, actor, one_step_template())
    plan.update(repo, actor, "PLAN-0001", "S01", "blocked", "waiting")
    plan.update(repo, actor, "PLAN-0001", "S01", "pending", "unblocked")
    step = _step(repo)
    assert step["status"] == "pending" and step["reason"] is None


def test_update_needs_a_reason_and_a_known_step(repo, actor):
    plan.create(repo, actor, one_step_template())
    _refused("blank_reason", plan.update, repo, actor, "PLAN-0001", "S01", "blocked", "  ")
    _refused("unknown_step", plan.update, repo, actor, "PLAN-0001", "S09", "blocked", "r")
    _refused("unknown_plan", plan.update, repo, actor, "PLAN-0002", "S01", "blocked", "r")
    _refused("invalid_status", plan.update, repo, actor, "PLAN-0001", "S01", "bogus", "r")


def test_an_update_for_a_plan_that_folds_later_is_a_violation(repo, actor):
    event = {"id": "EVT-X", "type": "plan.step_updated", "ts": "2026-11-09T00:00:00.000Z",
             "data": {"plan": "PLAN-0001", "step": "S01", "status": "blocked", "reason": "r"}}
    assert state.fold([event])["violations"] == [{"event": "EVT-X", "kind": "unknown_plan"}]


# --------------------------------------------------------------------------- missing skills

def test_step_for_a_missing_skill_is_blocked(repo, actor):
    plan.create(repo, actor, one_step_template())
    s = _step(repo, installed_skills=["qa-security"])
    assert s["status"] == "blocked" and "plugin install" in s["remedy"] and SKILL in s["remedy"]
    assert "not installed" in s["reason"]


def test_an_installed_skill_does_not_block(repo, actor):
    plan.create(repo, actor, one_step_template())
    s = _step(repo, installed_skills=[SKILL])
    assert s["status"] == "pending" and s["remedy"] is None


def test_unknown_skills_are_not_missing_skills(repo, actor):
    plan.create(repo, actor, one_step_template())
    assert _step(repo, installed_skills=None)["status"] == "pending"
    assert _step(repo, installed_skills=[])["status"] == "blocked"  # an empty directory is a known answer


def test_the_cli_reads_the_skills_next_to_it(repo, actor, qa_cli, tmp_path):
    plan.create(repo, actor, one_step_template())
    skills = tmp_path / "skills"
    (skills / "qa-security").mkdir(parents=True)
    (skills / "qa-security" / "SKILL.md").write_text("---\nname: qa-security\nmetadata:\n  suite: qa-engineering-suite\n---\n")
    code, out = qa_cli("plan", "status", "--skills-dir", str(skills))
    assert code == 0 and out["steps"][0]["status"] == "blocked" and "plugin install" in out["steps"][0]["remedy"]


# --------------------------------------------------------------------------- validity

def test_cycle_in_depends_on_refused(repo, actor):
    template = one_step_template()
    template["steps"].append(dict(copy.deepcopy(template["steps"][0]), id="S02", depends_on=["S01"]))
    template["steps"][0]["depends_on"] = ["S02"]
    _refused("plan_invalid", plan.create, repo, actor, template)
    assert state.load(repo)["plans"] == {}


@pytest.mark.parametrize("change", [
    lambda t: t["steps"][0].update(depends_on=["S09"]),
    lambda t: t["steps"].append(copy.deepcopy(t["steps"][0])),
    lambda t: t["steps"][0].update(depends_on=["S01"]),
    lambda t: t["steps"][0]["expected_outputs"].append({"kind": "bogus"}),
    lambda t: t["steps"][0]["expected_outputs"][0].update(commit="a" * 40),
    lambda t: t["steps"][0].update(status="done"),
    lambda t: t.update(steps=[]),
], ids=["unknown-dependency", "duplicate-step", "self-dependency", "unknown-kind", "caller-commit", "stored-status", "no-steps"])
def test_an_invalid_template_is_refused(repo, actor, change):
    template = one_step_template()
    change(template)
    _refused("plan_invalid", plan.create, repo, actor, template)


def test_an_artifact_output_needs_a_path_and_a_schema(repo, actor):
    _refused("plan_invalid", plan.create, repo, actor, one_step_template(expected_outputs=[{"kind": "artifact"}]))


# --------------------------------------------------------------------------- the other output kinds

def _artifact_template(path="work/map.json"):
    return one_step_template(expected_outputs=[{"kind": "artifact", "path": path, "schema": "plan.schema.json#/$defs/target"}])


def _write_artifact(ws, name, body):
    path = ws.path(*name.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body))


def test_artifact_must_exist_validate_and_name_the_commit(repo, actor):
    commit = _head(repo.root)
    plan.create(repo, actor, _artifact_template())
    assert _step(repo)["status"] == "pending"
    _write_artifact(repo, "work/map.json", {"commit": "f" * 40, "dirty": False})
    assert _step(repo)["status"] == "pending"  # another commit
    _write_artifact(repo, "work/map.json", {"commit": commit, "dirty": "yes"})
    assert _step(repo)["status"] == "pending"  # fails its schema
    _write_artifact(repo, "work/map.json", {"commit": commit, "dirty": False})
    assert _step(repo)["status"] == "done"


@pytest.mark.parametrize("path", ["../escape.json", "/etc/hosts", "work/../../x.json", "a\\b.json"])
def test_artifact_paths_stay_inside_qa(repo, actor, path):
    plan.create(repo, actor, _artifact_template(path))
    assert _step(repo)["status"] == "pending"


def test_artifact_through_a_symlink_does_not_count(repo, actor, tmp_path):
    commit = _head(repo.root)
    plan.create(repo, actor, _artifact_template())
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"commit": commit, "dirty": False}))
    repo.path("work").mkdir(exist_ok=True)
    repo.path("work", "map.json").symlink_to(outside)
    assert _step(repo)["status"] == "pending"


def test_gate_and_report_outputs(repo, actor, py):
    outputs = [{"kind": "gate", "profile": "change"}, {"kind": "report", "format": "md"}]
    plan.create(repo, actor, one_step_template(expected_outputs=outputs))
    _run(repo, actor, py)
    assert _step(repo)["status"] == "pending"
    evaluated = gate.evaluate(repo, actor, profile="change")
    step = _step(repo)
    assert [o["met"] for o in step["outputs"]] == [True, False]
    from qa import report
    report.render(repo, actor, formats=("md",))
    assert _step(repo)["status"] == "done" and evaluated["data"]["target"]["commit"] == _head(repo.root)


def test_a_gate_verdict_filter(repo, actor, py):
    _run(repo, actor, py)
    verdict = gate.evaluate(repo, actor, profile="change")["data"]["verdict"]
    others = [v for v in ("READY", "READY_WITH_CONDITIONS", "INSUFFICIENT_EVIDENCE", "NOT_READY") if v != verdict]
    plan.create(repo, actor, one_step_template(expected_outputs=[{"kind": "gate", "verdict_in": others}]))
    assert _step(repo)["status"] == "pending"
    plan.create(repo, actor, one_step_template(expected_outputs=[{"kind": "gate", "verdict_in": [verdict]}]))
    assert _step(repo)["status"] == "done"


def _revalidated(repo, actor, commit, *, fixed=True):
    """FND-001, a fix whose fix_commit is ``commit``, four runs on it and on a base, and a revalidation; returns its outcome."""
    from helpers import base
    from qa import findings, fixes
    from test_fixes import exe_id, record_run, DEFECT, KEEP
    base_c = "a" * 40
    findings.add(repo, actor, base())
    fixes.add(repo, actor, {"findings": ["FND-001"], "base_commit": base_c, "fix_commit": commit, "description": "d"})
    record_run(repo, actor, "A", base_c, [(DEFECT, "failed", {})], 1)
    record_run(repo, actor, "B", commit, [(DEFECT, "passed" if fixed else "failed", {})], 2)
    record_run(repo, actor, "C", base_c, [(KEEP, "passed", {})], 3)
    record_run(repo, actor, "D", commit, [(KEEP, "passed", {})], 4)
    return fixes.revalidate(repo, actor, "FIX-001", f2p=(exe_id("A"), exe_id("B")), p2p=(exe_id("C"), exe_id("D")),
                            defect_tests=[DEFECT])["outcome"]


def test_a_revalidation_output_is_met_only_by_a_revalidation_of_the_plans_commit(repo, actor):
    plan.create(repo, actor, one_step_template(expected_outputs=[{"kind": "revalidation", "outcome_in": ["passed"]}]))
    frozen = state.load(repo)["plans"]["PLAN-0001"]["steps"][0]["expected_outputs"][0]
    assert frozen["commit"] == _head(repo.root) and frozen["outcome_in"] == ["passed"] and frozen["min_count"] == 1
    assert _step(repo)["status"] == "pending"
    assert _revalidated(repo, actor, "d" * 40) == "passed"  # a revalidation of another commit says nothing about this one
    assert _step(repo)["status"] == "pending"


def test_a_passed_revalidation_at_the_target_completes_the_step(repo, actor):
    plan.create(repo, actor, one_step_template(expected_outputs=[{"kind": "revalidation", "outcome_in": ["passed"]}]))
    assert _revalidated(repo, actor, _head(repo.root)) == "passed"
    step = _step(repo)
    assert step["status"] == "done" and step["outputs"][0]["met"] is True


def test_a_failed_revalidation_does_not_meet_outcome_in_passed_but_meets_an_open_filter(repo, actor):
    plan.create(repo, actor, one_step_template(expected_outputs=[{"kind": "revalidation", "outcome_in": ["passed"]}]))
    assert _revalidated(repo, actor, _head(repo.root), fixed=False) == "failed"
    assert _step(repo)["status"] == "pending"
    plan.create(repo, actor, one_step_template(expected_outputs=[{"kind": "revalidation"}]))
    assert _step(repo)["status"] == "done"


@pytest.mark.parametrize("output", [{"kind": "revalidation", "outcome_in": []}, {"kind": "revalidation", "outcome_in": ["fine"]},
                                    {"kind": "gate", "outcome_in": ["passed"]}])
def test_a_malformed_revalidation_output_is_refused(repo, actor, output):
    _refused("plan_invalid", plan.create, repo, actor, one_step_template(expected_outputs=[output]))


def test_status_is_compact(repo, actor):
    plan.create(repo, actor, one_step_template())
    summary = plan.status(repo)
    assert set(summary) == {"id", "mode", "target", "steps", "counts"}
    assert summary["counts"] == {"pending": 1, "done": 0, "blocked": 0, "not_run": 0}
    assert set(summary["steps"][0]) == {"id", "skill", "depends_on", "status", "reason", "remedy", "ready", "outputs"}


def test_ready_waits_for_dependencies(repo, actor, py):
    template = one_step_template()
    template["steps"].append(dict(copy.deepcopy(template["steps"][0]), id="S02", depends_on=["S01"], skill="qa-security",
                                  expected_outputs=[{"kind": "execution", "skill": "qa-security"}]))
    plan.create(repo, actor, template)
    assert [s["ready"] for s in plan.status(repo)["steps"]] == [True, False]
    _run(repo, actor, py)
    assert [s["ready"] for s in plan.status(repo)["steps"]] == [False, True]  # S01 is done; S02 is open
