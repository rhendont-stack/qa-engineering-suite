"""The deterministic scenarios of the spec's §16.3 that milestone 1 can run, each through the launcher (Task 19).

F4 and F9 are miniature fixtures built here; the full fixture projects arrive in milestone 2 (deviation 16).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

from helpers import CORPUS, WRITE_JUNIT

APPROVAL = "scenario test"
JUNIT = ("--report", "junit:{evidence}/junit.xml")


def junit_run(qa_cli, outcome, *more, **kw):
    """``run`` of a command that writes a one-case JUnit report ending ``outcome`` (pass or fail)."""
    return qa_cli("run", *JUNIT, *more, "--", sys.executable, "-c", WRITE_JUNIT, "{evidence}/junit.xml", outcome, **kw)


def git_project(tmp_path, qa_cli, name):
    """A trusted workspace that is also a git work tree with one commit, as Task 16's ``repo_ws`` is."""
    if shutil.which("git") is None:
        pytest.skip("tool missing: git")   # the Linux images ship no git; §16.1: a test that needs an absent tool is skipped
    root = tmp_path / name; root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / "a.txt").write_text("1")
    for args in (["add", "a.txt"], ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "c"]):
        subprocess.run(["git", "-C", str(root), *args], check=True)
    assert qa_cli("init", workspace=root)[0] == 0
    assert qa_cli("config", "set", "project.trust", "trusted", "--approval", APPROVAL, workspace=root)[0] == 0
    return root


# --------------------------------------------------------------------------- F4, F9

def test_f4_flaky_three_reruns_end_failed_flaky(trusted, qa_cli):
    code, first = junit_run(qa_cli, "fail")
    assert code == 1 and first["status"] == "failed"
    for outcome, want in (("pass", 0), ("fail", 1), ("pass", 0)):
        code, last = junit_run(qa_cli, outcome, "--attempt-of", first["execution"])
        assert code == want
    view = last["flaky"]["c::t"]
    assert (view["status"], view["flaky"], view["attempts"], view["passes"], view["ratio"]) == ("failed", True, 4, 2, 0.5)


def test_f9_zero_tests_inconclusive(trusted, qa_cli):
    code, out = qa_cli("run", *JUNIT, "--", sys.executable, "-c",
                       "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('<testsuite></testsuite>')", "{evidence}/junit.xml")
    assert code == 4 and (out["status"], out["reason"]) == ("inconclusive", "no_tests_collected")


def test_f9_junit_failures_with_exit_zero(trusted, qa_cli):
    code, out = qa_cli("run", *JUNIT, "--", sys.executable, "-c",
                       "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('<testsuite><testcase classname=\"c\" name=\"t\"><failure/></testcase></testsuite>')",
                       "{evidence}/junit.xml")
    assert code == 1 and out["status"] == "failed" and "exit_code_mismatch" in out["anomalies"]


def test_f9_missing_tool_blocks_and_gate_insufficient(tmp_path, qa_cli):
    root = git_project(tmp_path, qa_cli, "f9")
    code, ran = qa_cli("run", "--", "qa-no-such-tool", workspace=root)
    assert code == 3 and (ran["status"], ran["reason"]) == ("blocked", "tool_missing")
    code, release = qa_cli("--role", "evaluator", "--independence", "I2", "gate", "evaluate", "--profile", "release", workspace=root)
    assert code == 3 and release["verdict"] == "INSUFFICIENT_EVIDENCE"
    code, change = qa_cli("gate", "evaluate", "--profile", "change", workspace=root)
    suite = next(c for c in change["criteria"] if c["type"] == "suite_executed")
    assert code == 3 and suite["outcome"] == "NO_EVIDENCE" and ran["execution"] in suite["detail"]


# --------------------------------------------------------------------------- integrity

def test_one_log_byte_edit_is_modified(trusted, qa_cli):
    code, ran = qa_cli("run", "--", sys.executable, "-c", "print('a line the log keeps')")
    assert code == 0 and qa_cli("ledger", "verify")[0] == 0
    log = trusted.root / ran["evidence"] / "stdout.log"
    data = bytearray(log.read_bytes()); data[0] ^= 1; log.write_bytes(bytes(data))
    code, out = qa_cli("ledger", "verify")
    assert code == 1 and [p for p in out["problems"] if p["kind"] == "modified" and p["where"].endswith("stdout.log")]


def test_deleted_ledger_line_is_unchained(trusted, qa_cli):
    assert all(qa_cli("run", "--", sys.executable, "-c", "pass")[0] == 0 for _ in range(3))   # three events in the CLI's own session
    assert qa_cli("ledger", "verify")[0] == 0
    ledger = max((trusted.root / ".qa" / "ledger").glob("*.jsonl"), key=lambda f: f.stat().st_size)   # the CLI's session, not the fixture's
    lines = ledger.read_bytes().splitlines(True)
    assert len(lines) > 2
    ledger.write_bytes(b"".join(lines[:1] + lines[2:]))
    code, out = qa_cli("ledger", "verify")
    assert code == 1 and "unchained" in {p["kind"] for p in out["problems"]}


# --------------------------------------------------------------------------- reports

def test_same_state_renders_identical_bytes(trusted, qa_cli):
    assert junit_run(qa_cli, "pass")[0] == 0
    first, second = (qa_cli("report", "render")[1] for _ in range(2))
    assert first["id"] != second["id"]
    for name in ("report.md", "report.json"):
        a, b = ((trusted.root / ".qa" / "reports" / out["id"] / name).read_bytes() for out in (first, second))
        assert a and a == b, name


# --------------------------------------------------------------------------- gate

def test_gate_truth_table_via_cli(tmp_path, qa_cli):
    def verdict(name, *, outcome=None, independence="I1"):
        root = git_project(tmp_path, qa_cli, name)
        if outcome is not None:
            junit_run(qa_cli, outcome, workspace=root)
        code, out = qa_cli("--independence", independence, "gate", "evaluate", "--profile", "change", workspace=root)
        return out["verdict"], code
    assert verdict("ready", outcome="pass") == ("READY", 0)
    assert verdict("conditions", outcome="pass", independence="I0") == ("READY_WITH_CONDITIONS", 6)   # independence_min is advisory
    assert verdict("not-ready", outcome="fail") == ("NOT_READY", 1)
    assert verdict("insufficient") == ("INSUFFICIENT_EVIDENCE", 3)


# --------------------------------------------------------------------------- guard

def test_guard_corpus_via_cli(ws, qa_cli):
    cases = json.loads((CORPUS / "guard" / "cases.json").read_text(encoding="utf-8"))
    exit_codes = {"allow": 0, "deny": 3, "require_grant": 3}
    wrong = []
    for case in cases:
        words = [case["command"]] if isinstance(case["command"], str) else case["command"]   # a string row is one argument, shell or not
        code, out = qa_cli("guard", "check", *(["--shell"] if case["shell"] else []), "--activity", case["activity"], "--", *words)
        if (out.get("decision"), code) != (case["expect"], exit_codes[case["expect"]]):
            wrong.append((case["id"], case["expect"], out.get("decision"), code))
    assert wrong == [] and len(cases) > 100
