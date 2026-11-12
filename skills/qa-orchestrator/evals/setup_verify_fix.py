"""Scenario setup "F7 at the fix, the defect recorded" for the orchestrator: a workspace holding the one finding the fix is about.

Run by tools/run_skill_eval.py with the working copy (F7 generated with its history, stopped at C3) as its directory, never by an
agent. Nothing in the ledger is written by hand: ``init`` and one ``finding add`` of the failing order total, as the verifier
that saw the test fail would have recorded it. The finding is the one the qa-regression-guard fix case records (its
``_cases.py``), restated here so this case does not change when that one does. Trust is not recorded: the user gives it in the
prompt, and recording it is the orchestrator's job. The input file goes to a temporary directory outside the copy and is deleted.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE.parent / "scripts" / "qa.py"
if not LAUNCHER.is_file():  # before the skill has been vendored, the shared launcher is the same kernel
    LAUNCHER = HERE.parents[2] / "shared" / "launcher" / "qa.py"

ORDER_TOTAL = "tests.test_service.ServiceTests::test_payable_amount_after_ten_percent"
FINDING_SKILL = "qa-test-execution"
FINDING_ID = "FND-001"
FINDING = {
    "title": "Payable amount is one cent short on a discounted order",
    "description": "An order of 999 cents with 10 percent off is billed 899 cents instead of 900.",
    "category": "functional",
    "severity_rationale": {"anchor": "FUNC-C3", "modifiers": []},
    "component": {"path": "orders/pricing.py", "endpoint": "GET /orders/{id}", "criticality": "high"},
    "verification_status": "suspected",
    "evidence": [],
    "reproduction_steps": [],
    "refs": {"rule": "manual", "location": "orders/pricing.py"},
    "key_attributes": {"test": ORDER_TOTAL},
}


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


def main():
    if os.path.exists(".qa"):
        sys.exit("the working copy already holds a .qa/")
    if not Path("orders/pricing.py").is_file() or not Path(".git").is_dir():
        sys.exit("the working copy is not the regression-repo fixture with its history")
    kernel("user", "user", "init")
    with tempfile.TemporaryDirectory(prefix="qa-orchestrator-setup-") as scratch:
        path = Path(scratch) / "finding.json"
        path.write_text(json.dumps(FINDING), encoding="utf-8")
        recorded = kernel("verifier", FINDING_SKILL, "finding", "add", "--json", str(path))["id"]
    if recorded != FINDING_ID:
        sys.exit("unexpected finding ID: %s" % recorded)
    print(json.dumps({"trusted": False, "finding": recorded}))


main()
