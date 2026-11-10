"""Scenario setup "F5 with REQ/AC recorded": records the requirements and acceptance criteria of requirements.md in the copy's .qa/.

Run by tools/run_skill_eval.py with the working copy as its directory, never by an agent. It calls the kernel's launcher, as
qa-requirements would have, so nothing in the ledger is written by hand: ``init``, one ``req import`` and one ``ac add`` per
criterion. The input files go to a temporary directory outside the copy and are deleted, so the copy holds only the kernel's own
``.qa/``. The record set is ``_requirements.py``; it adds nothing that requirements.md does not say.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _requirements as data  # noqa: E402

LAUNCHER = HERE.parent / "scripts" / "qa.py"
if not LAUNCHER.is_file():  # before the skill has been vendored, the shared launcher is the same kernel
    LAUNCHER = HERE.parents[2] / "shared" / "launcher" / "qa.py"


def kernel(*args):
    """One kernel command as the auditor role; its JSON reply. Any refusal stops the setup."""
    done = subprocess.run([sys.executable, "-I", str(LAUNCHER), "--role", "auditor", "--skill", data.SKILL] + list(args),
                          capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    try:
        body = json.loads(done.stdout)
    except ValueError:
        sys.exit("kernel reply is not JSON for %s: %s" % (args[:2], done.stderr[-300:]))
    if done.returncode != 0 or body.get("ok") is False:
        sys.exit("kernel refused %s: %s" % (args[:2], json.dumps(body.get("error"))[:300]))
    return body


if os.path.exists(".qa"):
    sys.exit("the working copy already holds a .qa/")
if not Path(data.SOURCE).is_file():
    sys.exit("the working copy has no %s" % data.SOURCE)
kernel("init")
with tempfile.TemporaryDirectory(prefix="qa-strategy-setup-") as scratch:
    path = Path(scratch) / "requirements.json"
    path.write_text(json.dumps(data.requirement_records()), encoding="utf-8")
    req_ids = kernel("req", "import", "--json", str(path))["ids"]
    expected_reqs, expected_acs = data.expected_ids()
    if req_ids != expected_reqs:
        sys.exit("unexpected requirement IDs: %s" % req_ids)
    ac_ids = []
    for n, record in enumerate(data.criterion_records(req_ids)):
        path = Path(scratch) / ("ac-%d.json" % n)
        path.write_text(json.dumps(record), encoding="utf-8")
        ac_ids.append(kernel("ac", "add", "--json", str(path))["id"])
    if ac_ids != expected_acs:
        sys.exit("unexpected criterion IDs: %s" % ac_ids)
print(json.dumps({"requirements": req_ids, "criteria": ac_ids}))
