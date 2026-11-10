"""F7 ``regression-repo``: a six-commit history made at test time by the fixture's generator, never kept in this repository.

C0 a service with passing tests and ``openapi.yaml``; C1 a feature that breaks an existing function and weakens its test; C2 a renamed
response field; C3 a fix with a FAIL_TO_PASS test; C4 a "fix" that special-cases the test's input; C5 a ``conftest.py`` hook that
rewrites failures as passes. Runs go through the real CLI, in suite worktrees where a commit other than the checked-out one is needed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

from helpers import FIXTURES, answer_key, base

GENERATOR = FIXTURES / "regression-repo" / "generate.py"
PYTEST = ("-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "junit_family=xunit2", "--junitxml={evidence}/junit.xml")
WEAKENED = "tests.test_pricing.PricingTests::test_discount_on_an_odd_total"
ORDER_TOTAL = "tests.test_service.ServiceTests::test_payable_amount_after_ten_percent"
LOYALTY = "tests.test_service.ServiceTests::test_summary_lists_loyalty_points"


def git(root, *args):
    done = subprocess.run(["git", "-C", str(root)] + list(args), capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout


def commits(root):
    """The commit hashes, oldest first: C0 .. Cn."""
    return git(root, "rev-list", "--reverse", "HEAD").split()


def generate(directory, *extra):
    if shutil.which("git") is None:
        pytest.skip("tool missing: git")
    return subprocess.run([sys.executable, str(GENERATOR), str(directory)] + list(extra), capture_output=True, text=True, timeout=120)


def run_in(project, where, *extra):
    """A pytest run recorded by the kernel, with ``where`` (a directory) as the working directory."""
    return project.cli("run", "--report", "junit:{evidence}/junit.xml", "--", sys.executable, *PYTEST, *extra, cwd=where)


def at(project, commit):
    """The folder of a suite worktree at ``commit``."""
    code, out = project.cli("worktree", "add", commit)
    assert code == 0, out
    return project.root / out["path"]


def record_failing_order_total(project):
    """The one finding the fix is about: the payable amount of a discounted order is wrong. Nothing else is recorded."""
    finding = base(title="Payable amount is one cent short on a discounted order", category="functional",
                   description="An order of 999 cents with 10 percent off is billed 899 cents instead of 900.",
                   severity_rationale={"anchor": "FUNC-C3", "modifiers": []},
                   component={"path": "orders/pricing.py", "endpoint": "GET /orders/{id}", "criticality": "high"},
                   refs={"rule": "manual", "location": "orders/pricing.py"})
    path = project.root.parent / "finding.json"
    path.write_text(json.dumps(finding), encoding="utf-8")
    code, out = project.cli("finding", "add", "--json", str(path))
    assert code == 0, out
    return out["id"]


def test_f7_history_has_the_six_commits_and_nothing_else(tmp_path):
    done = generate(tmp_path / "repo")
    assert done.returncode == 0, done.stderr
    root = tmp_path / "repo"
    assert len(commits(root)) == 6 and len(git(root, "log", "--all", "--format=%H").split()) == 6
    subjects = git(root, "log", "--reverse", "--format=%s").splitlines()
    assert len(set(subjects)) == 6 and all(8 <= len(subject) <= 72 for subject in subjects)
    assert git(root, "for-each-ref", "--format=%(refname)").split() == ["refs/heads/main"]
    assert git(root, "tag").strip() == "" and git(root, "remote").strip() == "" and git(root, "status", "--porcelain").strip() == ""
    assert git(root, "rev-list", "--merges", "HEAD").strip() == ""
    authors = set(git(root, "log", "--format=%an <%ae>|%cn <%ce>").splitlines())
    assert len(authors) == 1                       # one fixed author and committer
    again = generate(tmp_path / "again")
    assert again.returncode == 0 and commits(tmp_path / "again") == commits(root)    # the same commits, bit for bit
    key = answer_key("regression-repo")
    assert [entry["index"] for entry in key["commits"]] == [0, 1, 2, 3, 4, 5] and key["fixture"] == "regression-repo"
    for issue in key["issues"]:
        text = git(root, "show", "%s:%s" % (commits(root)[issue["commit"]], issue["location"]["path"]))
        assert issue["location"]["match"] in text, issue["id"]
    assert (root / "openapi.yaml").is_file() and not (root / "generate.py").exists()


def test_f7_a_checkout_leaves_nothing_later(tmp_path):
    done = generate(tmp_path / "repo", "--checkout", "2")
    assert done.returncode == 0, done.stderr
    root = tmp_path / "repo"
    assert len(commits(root)) == 3 and len(git(root, "log", "--all", "--format=%H").split()) == 3
    objects = git(root, "cat-file", "--batch-all-objects", "--batch-check").splitlines()
    assert sum(1 for line in objects if line.split()[1] == "commit") == 3
    assert "amount_due_cents" in (root / "openapi.yaml").read_text(encoding="utf-8")
    assert git(root, "status", "--porcelain").strip() == "" and git(root, "symbolic-ref", "HEAD").strip() == "refs/heads/main"
    assert commits(tmp_path / "repo")[:3] == commits_of(tmp_path, 6)[:3]
    assert generate(tmp_path / "bad", "--checkout", "9").returncode != 0 and not (tmp_path / "bad").exists()


def commits_of(tmp_path, count):
    full = tmp_path / ("full-%d" % count)
    assert generate(full).returncode == 0
    return commits(full)


def test_f7_generator_refuses_the_repository_and_a_used_directory(tmp_path, repo_root):
    used = tmp_path / "used"
    used.mkdir()
    (used / "file.txt").write_text("here\n")
    done = generate(used)
    assert done.returncode != 0 and "new or empty" in done.stderr
    inside = repo_root / "fixtures" / "regression-repo" / "out"
    done = generate(inside)
    assert done.returncode != 0 and "refusing" in done.stderr and not inside.exists()


def test_f7_c1_is_a_base_pass_head_fail(qa_on_generated):
    project = qa_on_generated("regression-repo", checkout=1)
    first, second = commits(project.root)
    base_run = run_in(project, at(project, first))
    head_run = run_in(project, at(project, second))
    assert (base_run[0], base_run[1]["status"]) == (0, "passed") and (head_run[0], head_run[1]["status"]) == (1, "failed")
    results = project.results(head_run[1]["execution"])
    assert results[ORDER_TOTAL] == "failed" and results[WEAKENED] == "passed"      # the weakened test no longer notices
    assert {name: status for name, status in results.items() if status == "failed"} == {ORDER_TOTAL: "failed"}
    code, out = project.cli("compare", base_run[1]["execution"], head_run[1]["execution"])
    assert code == 0 and out["new_failures"] == [ORDER_TOTAL] and out["fixed"] == [] and out["comparable"]
    diff = git(project.root, "diff", first, second, "--", "tests/test_pricing.py")
    assert "-        self.assertEqual(apply_discount(999, 10), 900)" in diff
    assert "+        self.assertTrue(apply_discount(999, 10))" in diff


def test_f7_c3_revalidates(qa_on_generated):
    project = qa_on_generated("regression-repo", checkout=3)
    c0, c1, c2, c3 = commits(project.root)
    finding = record_failing_order_total(project)
    base_code, base_run = run_in(project, at(project, c2))
    head_code, head_run = run_in(project, at(project, c3))
    assert (base_code, base_run["status"], head_code, head_run["status"]) == (1, "failed", 0, "passed")
    path = project.root.parent / "fix.json"
    path.write_text(json.dumps({"findings": [finding], "base_commit": c2, "fix_commit": c3, "description": "Round the discount down again"}))
    code, fix = project.cli("fix", "add", "--json", str(path))
    assert code == 0 and fix["id"] == "FIX-001", fix
    pair = "%s:%s" % (base_run["execution"], head_run["execution"])
    code, out = project.cli("revalidate", fix["id"], "--f2p", pair, "--p2p", pair, "--defect-test", ORDER_TOTAL)
    assert code == 0 and out["outcome"] == "passed", out
    assert out["findings"] == {finding: "verified_fixed"}


def test_f7_c5_hook_rewrites_outcomes(qa_on_generated):
    project = qa_on_generated("regression-repo")
    assert len(commits(project.root)) == 6
    code, hooked = run_in(project, project.root)
    assert (code, hooked["status"]) == (0, "passed")                             # the report says everything passed ...
    assert project.results(hooked["execution"])[LOYALTY] == "passed"
    source = (project.root / "tests" / "test_service.py").read_text(encoding="utf-8")
    assert "assert False" in source                                               # ... while the test asserts False
    assert "pytest_runtest_makereport" in (project.root / "conftest.py").read_text(encoding="utf-8")
    code, plain = run_in(project, project.root, "--noconftest")
    assert (code, plain["status"]) == (1, "failed") and project.results(plain["execution"])[LOYALTY] == "failed"
