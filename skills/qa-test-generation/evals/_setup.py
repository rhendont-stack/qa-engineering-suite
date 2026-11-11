"""Records the prepared state of ``_records.py`` in the working copy's ``.qa/``, through the kernel's launcher only.

Shared by ``setup_with_grant.py`` and ``setup_no_grant.py``, which the harness runs with the working copy as their directory,
never an agent. Nothing in the ledger is written by hand: ``init``; the user's approval to run the project's tests
(``config set project.trust trusted``, quoted as the user's words); one ``req import``, one ``ac add`` per criterion and one
``tc import``, as qa-requirements and qa-test-strategy would have recorded them; and, with ``grant``, one ``authorize`` of
``write-tests`` on ``tests/**``, signed by the copy's own authority key like any grant the kernel records. The input files go to
a temporary directory outside the copy and are deleted.
"""
import datetime
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _records as data  # noqa: E402

LAUNCHER = HERE.parent / "scripts" / "qa.py"
if not LAUNCHER.is_file():  # before the skill has been vendored, the shared launcher is the same kernel
    LAUNCHER = HERE.parents[2] / "shared" / "launcher" / "qa.py"


def kernel(role, skill, *args):
    """One kernel command; its JSON reply. Any refusal stops the setup."""
    done = subprocess.run([sys.executable, "-I", str(LAUNCHER), "--role", role, "--skill", skill] + list(args),
                          capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    try:
        body = json.loads(done.stdout)
    except ValueError:
        sys.exit("kernel reply is not JSON for %s: %s" % (args[:2], done.stderr[-300:]))
    if done.returncode != 0 or body.get("ok") is False:
        sys.exit("kernel refused %s: %s" % (args[:2], json.dumps(body.get("error"))[:300]))
    return body


def prepare(grant):
    if os.path.exists(".qa"):
        sys.exit("the working copy already holds a .qa/")
    if not Path(data.SOURCE).is_file() or not Path("shop").is_dir():
        sys.exit("the working copy is not the seeded-defects fixture")
    req_skill, strategy = data.REQUIREMENTS_SKILL, data.STRATEGY_SKILL
    kernel("auditor", req_skill, "init")
    kernel("user", "user", "config", "set", "project.trust", "trusted", "--approval", data.TRUST_APPROVAL)
    expected_reqs, expected_acs, expected_tcs = data.expected_ids()
    with tempfile.TemporaryDirectory(prefix="qa-generation-setup-") as scratch:
        path = Path(scratch) / "requirements.json"
        path.write_text(json.dumps(data.requirement_records()), encoding="utf-8")
        req_ids = kernel("auditor", req_skill, "req", "import", "--json", str(path))["ids"]
        if req_ids != expected_reqs:
            sys.exit("unexpected requirement IDs: %s" % req_ids)
        ac_ids = []
        for n, record in enumerate(data.criterion_records(req_ids)):
            path = Path(scratch) / ("ac-%d.json" % n)
            path.write_text(json.dumps(record), encoding="utf-8")
            ac_ids.append(kernel("auditor", req_skill, "ac", "add", "--json", str(path))["id"])
        if ac_ids != expected_acs:
            sys.exit("unexpected criterion IDs: %s" % ac_ids)
        path = Path(scratch) / "testcases.json"
        path.write_text(json.dumps(data.testcase_records()), encoding="utf-8")
        tc_ids = kernel("auditor", strategy, "tc", "import", "--json", str(path))["ids"]
        if tc_ids != expected_tcs:
            sys.exit("unexpected test case IDs: %s" % tc_ids)
    granted = None
    if grant:
        ends = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=12)
        stamp = ends.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (ends.microsecond // 1000)
        granted = kernel("user", "user", "authorize", "--environment", "local", "--activity", "write-tests",
                         "--limit", "write-tests.paths=" + json.dumps(data.GRANT_PATHS), "--approval", data.GRANT_APPROVAL,
                         "--expires", stamp)["id"]
    print(json.dumps({"requirements": req_ids, "criteria": ac_ids, "testcases": tc_ids, "grant": granted}))
