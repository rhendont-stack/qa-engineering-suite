"""Records the prepared state of ``_cases.py`` in the working copy's ``.qa/``, through the kernel's launcher only.

Shared by ``setup_trusted.py`` and ``setup_with_finding.py``, which the harness runs with the working copy as their directory,
never an agent. Nothing in the ledger is written by hand: ``init``; the user's approval to run the project's tests
(``config set project.trust trusted``, quoted as the user's words); and, with ``finding``, one ``finding add`` of the failing
order total, as the verifier that saw the test fail would have recorded it. The input file goes to a temporary directory outside
the copy and is deleted.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _cases as data  # noqa: E402

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


def prepare(finding):
    if os.path.exists(".qa"):
        sys.exit("the working copy already holds a .qa/")
    if not Path("orders/pricing.py").is_file() or not Path(".git").is_dir():
        sys.exit("the working copy is not the regression-repo fixture with its history")
    kernel("user", "user", "init")
    kernel("user", "user", "config", "set", "project.trust", "trusted", "--approval", data.TRUST_APPROVAL)
    recorded = None
    if finding:
        with tempfile.TemporaryDirectory(prefix="qa-regression-setup-") as scratch:
            path = Path(scratch) / "finding.json"
            path.write_text(json.dumps(data.PREPARED_FINDING), encoding="utf-8")
            recorded = kernel("verifier", data.FINDING_SKILL, "finding", "add", "--json", str(path))["id"]
        if recorded != data.FINDING_ID:
            sys.exit("unexpected finding ID: %s" % recorded)
    print(json.dumps({"trusted": True, "finding": recorded}))
