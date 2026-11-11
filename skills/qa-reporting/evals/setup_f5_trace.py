"""Scenario setup "F5 traced": requirements, criteria, a test case, a recorded run and two traceability gaps (``_trace_records.py``).

Run by tools/run_skill_eval.py with the working copy (the seeded-defects fixture, one fresh commit) as its directory, never by an
agent. Everything goes through the kernel's launcher, each command under the role and skill that would have recorded it:
``init``; the user's approval to run the tests; ``req import`` and ``ac add`` (qa-requirements); ``tc import``
(qa-test-strategy); one full pytest run with a JUnit report (qa-test-execution); and the ``automated_by`` link of the test case
to the existing test that run passed (qa-test-generation). The project's tests run with the repository's ``.venv-eval``
interpreter, the evaluated agents' runtime, since the harness's own interpreter has no pytest.

Before it ends the setup computes the matrix in memory with the kernel's own function, read-only, and refuses (exit 1, so the run
never starts and nothing is spent) unless the gaps are exactly ``EXPECTED_GAPS``. It writes no ``.qa/trace/matrix.json``: only
``trace matrix`` writes that file, and whether the agent runs it is what the case measures. Input files go to a temporary
directory outside the copy and are deleted.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
import _trace_records as data  # noqa: E402

LAUNCHER = HERE.parent / "scripts" / "qa.py"
KERNEL = HERE.parent / "scripts" / "_kernel"
if not LAUNCHER.is_file():  # before the skill has been vendored, the shared launcher is the same kernel
    LAUNCHER = REPO / "shared" / "launcher" / "qa.py"
if not (KERNEL / "qa").is_dir():
    KERNEL = REPO / "shared" / "kernel"
PYTHON = REPO / ".venv-eval" / "bin" / "python"


def kernel(role, skill, *args, allow_failed_run=False):
    """One kernel command; its JSON reply. Any refusal stops the setup."""
    done = subprocess.run([sys.executable, "-I", str(LAUNCHER), "--role", role, "--skill", skill] + list(args),
                          capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    try:
        body = json.loads(done.stdout)
    except ValueError:
        sys.exit("kernel reply is not JSON for %s: %s" % (args[:2], done.stderr[-300:]))
    if "error" in body or (body.get("ok") is False and not allow_failed_run):
        sys.exit("kernel refused %s: %s" % (args[:2], json.dumps(body.get("error") or body.get("status"))[:300]))
    return body


def write(scratch, name, record):
    path = Path(scratch) / name
    path.write_text(json.dumps(record), encoding="utf-8")
    return str(path)


def main():
    if os.path.exists(".qa"):
        sys.exit("the working copy already holds a .qa/")
    if not Path(data.SOURCE).is_file() or not Path("shop").is_dir() or not Path("tests/test_smoke.py").is_file():
        sys.exit("the working copy is not the seeded-defects fixture")
    if not PYTHON.is_file():
        sys.exit("no interpreter at %s to run the fixture's tests" % PYTHON)
    kernel("auditor", data.REQUIREMENTS_SKILL, "init")
    kernel("user", "user", "config", "set", "project.trust", "trusted", "--approval", data.TRUST_APPROVAL)
    with tempfile.TemporaryDirectory(prefix="qa-reporting-setup-") as scratch:
        req_ids = kernel("auditor", data.REQUIREMENTS_SKILL, "req", "import", "--json",
                         write(scratch, "requirements.json", data.requirement_records()))["ids"]
        ac_ids = [kernel("auditor", data.REQUIREMENTS_SKILL, "ac", "add", "--json", write(scratch, "ac-%d.json" % n, record))["id"]
                  for n, record in enumerate(data.criterion_records(req_ids))]
        tc_ids = kernel("auditor", data.STRATEGY_SKILL, "tc", "import", "--json", write(scratch, "testcases.json", data.testcase_records()))["ids"]
    if req_ids != ["REQ-001", "REQ-002"] or ac_ids != ["AC-001.1", "AC-002.1"] or tc_ids != ["TC-001"]:
        sys.exit("unexpected IDs: %s %s %s" % (req_ids, ac_ids, tc_ids))
    run = kernel("verifier", data.EXECUTION_SKILL, "run", "--scope", "full", "--report", "junit:{evidence}/junit.xml", "--", str(PYTHON),
                 "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml={evidence}/junit.xml", allow_failed_run=True)
    if run.get("status") != "passed":
        sys.exit("the fixture's suite did not pass: %s" % json.dumps({k: run.get(k) for k in ("status", "reason", "execution")}))
    for testcase, test_id in data.AUTOMATED:
        kernel("builder", data.GENERATION_SKILL, "trace", "link", testcase, "automated_by", test_id)
    st = kernel("user", "user", "state", "show")["state"]
    commit = (st["executions"][run["execution"]].get("git") or {}).get("commit")
    sys.path.insert(0, str(KERNEL))
    from qa import trace  # noqa: E402  (the kernel's pure matrix function; it writes nothing)
    gaps = [(g["kind"], g["id"]) for g in trace.matrix(st, commit)["gaps"]]
    if gaps != list(data.EXPECTED_GAPS):
        sys.exit("unexpected traceability gaps: %s" % gaps)
    if Path(".qa/trace/matrix.json").exists():
        sys.exit("the setup must not leave a matrix file")
    print(json.dumps({"requirements": req_ids, "criteria": ac_ids, "testcases": tc_ids, "execution": run["execution"],
                      "commit": commit, "gaps": gaps}))


main()
