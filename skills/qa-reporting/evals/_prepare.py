"""Shared by the setup scripts of the qa-reporting evaluation cases (run by tools/run_skill_eval.py, never by an agent).

Each setup script runs with the working copy as its directory and builds a real ``.qa/`` state by calling the kernel's launcher,
as the skill that would have produced it does. Nothing here writes a ledger or evidence file by hand. The only deliberate
damage is ``tamper``, which a scenario asks for by name.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE.parent / "scripts" / "qa.py"
if not LAUNCHER.is_file():  # before the skill has been vendored, the shared launcher is the same kernel
    LAUNCHER = HERE.parents[1] / "shared" / "launcher" / "qa.py"
SKILL = "qa-test-execution"  # the skill whose work is being reproduced: reporting evaluates what another role recorded
APPROVAL = "the maintainer approves running this project's tests on this machine"


def kernel(*args: str) -> Dict[str, Any]:
    """One kernel command as the verifier role; its JSON reply. An exit other than a run's own status words stops the setup."""
    done = subprocess.run([sys.executable, "-I", str(LAUNCHER), "--role", "verifier", "--skill", SKILL] + list(args),
                          capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    try:
        body = json.loads(done.stdout)
    except ValueError:
        sys.exit("kernel reply is not JSON for %s: %s" % (args[:2], done.stderr[-300:]))
    if body.get("ok") is False and "error" in body:
        sys.exit("kernel refused %s: %s" % (args[:2], json.dumps(body["error"])[:300]))
    print(json.dumps({"command": " ".join(args[:2]), "status": body.get("status"), "reason": body.get("reason"), "execution": body.get("execution")}))
    return body


def trusted_workspace() -> None:
    kernel("init")
    kernel("config", "set", "project.trust", "trusted", "--approval", APPROVAL)


def run_suite(*pytest_args: str, scope: str = "full") -> str:
    """A recorded pytest run of the working copy, with a JUnit report in the evidence directory. Returns its execution ID."""
    body = kernel("run", "--scope", scope, "--report", "junit:{evidence}/junit.xml", "--", sys.executable, "-B", "-m", "pytest", "-q",
                  "--junitxml={evidence}/junit.xml", *pytest_args)
    return body["execution"]


def rerun(execution: str, test: str, times: int) -> List[str]:
    """``times`` isolated re-runs of one test, each its own execution chained to ``execution``."""
    return [kernel("run", "--attempt-of", execution, "--report", "junit:{evidence}/junit.xml", "--", sys.executable, "-B", "-m", "pytest",
                   "-q", test, "--junitxml={evidence}/junit.xml")["execution"] for _ in range(times)]


def tamper(execution: str, name: str = "stdout.log") -> Path:
    """Damage one recorded log, as a person editing the evidence after the fact would. The ledger keeps the old hash."""
    path = Path(".qa") / "evidence" / execution / name
    if not path.is_file():
        sys.exit("no %s to damage in %s" % (name, execution))
    with open(str(path), "ab") as handle:
        handle.write(b"\n10 passed in 0.02s\n")
    print("damaged %s" % path)
    return path


def require_clean_start() -> None:
    if os.path.exists(".qa"):
        sys.exit("the working copy already holds a .qa/")
