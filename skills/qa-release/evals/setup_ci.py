"""Prepares the F1 copy for the re-execution case (``ci-reexecution.json``); run by tools/run_skill_eval.py, never by an agent.

The case asks for a CI job that re-runs "the checks we ran locally", so the copy must hold such a run and the configuration a
job needs, recorded through the kernel's launcher only (nothing in ``.qa/`` is written by hand):

1. ``init``; the ``ci-github`` environment of class ``ci``; a standing ``test-execution-local`` authorization for it; the
   project trusted (each a ``config set`` with the maintainer's words as its approval); ``config accept``.
2. One commit that tracks what a CI checkout needs: ``.qa/config.json``, ``.qa/.gitignore`` and the launcher with its vendored
   kernel at ``.claude/skills/qa-release/scripts/``, as a project-scoped install tracks them. With the skill installed the
   harness has already copied it there; without it (the baseline) the same two files are copied from this skill's own
   ``scripts/``, so both arms start from the same commit. Nothing else of the skill is tracked.
3. One recorded host run of the project's tests with a JUnit report, at that commit, on a clean tree.

It prints one JSON object (the execution, its status, the commit and the ledger's event IDs at the end of the setup), which the
harness saves as the run's ``setup.txt``; the grader reads it to tell the setup's records from the agent's. Any refusal, a run
that does not pass or a tree that is not clean exits nonzero, so the harness refuses the run and spends nothing.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "scripts"
INSTALLED = Path(".claude") / "skills" / "qa-release" / "scripts"
LAUNCHER = INSTALLED / "qa.py"
CI_ENV = "ci-github"
APPROVALS = {
    "environments": "Add a ci-github environment of class ci for our GitHub Actions runner.",
    "standing_authorizations": "The ci-github runner may run our tests without asking each time.",
    "project.trust": "This is our own shop front end; I approve running its tests on this machine.",
    "accept": "Accept the configuration as it stands now.",
}
RUN = ["run", "--scope", "full", "--report", "junit:{evidence}/junit.xml", "--", "python3", "tests/run_junit.py", "--out",
       "{evidence}/junit.xml"]
GIT_ENV = {"GIT_AUTHOR_NAME": "maintainer", "GIT_AUTHOR_EMAIL": "maintainer@example.invalid",
           "GIT_COMMITTER_NAME": "maintainer", "GIT_COMMITTER_EMAIL": "maintainer@example.invalid",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}


def kernel(role: str, skill: str, *args: str) -> Dict[str, Any]:
    """One kernel command through the copy's launcher; its JSON reply. Any refusal stops the setup."""
    done = subprocess.run([sys.executable, "-I", "-B", str(LAUNCHER), "--role", role, "--skill", skill] + list(args),
                          capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL)
    try:
        body = json.loads(done.stdout)
    except ValueError:
        sys.exit("kernel reply is not JSON for %s: %s" % (" ".join(args[:2]), done.stderr[-300:]))
    if body.get("ok") is False or "error" in body:
        sys.exit("kernel refused %s: %s" % (" ".join(args[:2]), json.dumps(body.get("error"))[:300]))
    return body


def git(*args: str) -> str:
    done = subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + os.devnull, "-c", "core.autocrlf=false"]
                          + list(args), capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                          env=dict(os.environ, **GIT_ENV))
    if done.returncode != 0:
        sys.exit("git %s failed: %s" % (args[0], done.stderr[-300:]))
    return done.stdout


def install_launcher() -> str:
    """``with-skill`` when the harness installed the skill, else ``copied`` after copying the launcher and its kernel."""
    if LAUNCHER.is_file() and (INSTALLED / "_kernel").is_dir():
        return "with-skill"
    if INSTALLED.exists() or INSTALLED.is_symlink():
        sys.exit("the copy holds a partial %s" % INSTALLED.as_posix())
    if not (SOURCE / "qa.py").is_file() or not (SOURCE / "_kernel").is_dir():
        sys.exit("the skill's scripts are not vendored: run tools/vendor_kernel.py first")
    INSTALLED.mkdir(parents=True)
    shutil.copy2(str(SOURCE / "qa.py"), str(LAUNCHER))
    shutil.copytree(str(SOURCE / "_kernel"), str(INSTALLED / "_kernel"), ignore=shutil.ignore_patterns("__pycache__"))
    return "copied"


def event_ids() -> List[str]:
    events = kernel("user", "user", "ledger", "show").get("events")
    if not isinstance(events, list):
        sys.exit("ledger show returned no event list")
    return [str(e.get("id")) for e in events if isinstance(e, dict)]


def main() -> None:
    if Path(".qa").exists() or not Path("server.py").is_file() or not Path("tests/run_junit.py").is_file() \
            or not Path(".git").is_dir():
        sys.exit("the working copy is not a fresh F1 copy with its history")
    if shutil.which("git") is None:
        sys.exit("git is not on PATH")
    how = install_launcher()
    kernel("user", "user", "init")
    environments = kernel("user", "user", "config", "show")["config"]["environments"]
    environments = [e for e in environments if e.get("id") != CI_ENV] + [{"id": CI_ENV, "class": "ci"}]
    kernel("user", "user", "config", "set", "environments", json.dumps(environments), "--approval", APPROVALS["environments"])
    kernel("user", "user", "config", "set", "standing_authorizations",
           json.dumps([{"environment": CI_ENV, "activities": ["test-execution-local"]}]),
           "--approval", APPROVALS["standing_authorizations"])
    kernel("user", "user", "config", "set", "project.trust", "trusted", "--approval", APPROVALS["project.trust"])
    kernel("user", "user", "config", "accept", "--approval", APPROVALS["accept"])
    git("add", "-f", "--", ".qa/config.json", ".qa/.gitignore", LAUNCHER.as_posix(), (INSTALLED / "_kernel").as_posix())
    git("commit", "-q", "-m", "Track the QA launcher and its configuration")
    if git("status", "--porcelain", "--untracked-files=normal", "--", ".", ":(exclude).qa").strip():
        sys.exit("the tree is not clean after the commit")
    commit = git("rev-parse", "HEAD").strip()
    body = kernel("verifier", "qa-test-execution", *RUN)
    if body.get("status") != "passed":
        sys.exit("the recorded run did not pass: %s %s" % (body.get("status"), body.get("reason")))
    print(json.dumps({"launcher": how, "commit": commit, "execution": body.get("execution"), "status": body.get("status"),
                      "environment": CI_ENV, "events": event_ids()}, sort_keys=True))


if __name__ == "__main__":
    main()
