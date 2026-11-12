"""Prepares the working copy for a qa-mutation case, through the kernel's launcher for everything under ``.qa/``.

Shared by ``setup_green.py`` and ``setup_red.py``, which the harness runs with the working copy as their directory, never an
agent. With ``repair``, ``parse_quantity`` is rewritten as ``_mutation_cases.REPAIRED`` and the copy's one commit is amended to
hold it, so the work tree is clean and HEAD is the repaired project. Then ``init`` and the user's approval to run the project's tests
(``config set project.trust trusted``, quoted as the user's words). Nothing else is recorded.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _mutation_cases as data  # noqa: E402

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


def git(*args):
    env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": os.environ.get("HOME", "/nonexistent"), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "eval", "GIT_AUTHOR_EMAIL": "eval@example.invalid",
           "GIT_COMMITTER_NAME": "eval", "GIT_COMMITTER_EMAIL": "eval@example.invalid"}
    done = subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"] + list(args), capture_output=True,
                          text=True, timeout=60, env=env, stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        sys.exit("git %s failed: %s" % (args[0], done.stderr[-300:]))
    return done.stdout


def prepare(repair):
    if os.path.exists(".qa"):
        sys.exit("the working copy already holds a .qa/")
    pricing = Path(data.PRICING)
    if not pricing.is_file() or not Path(".git").is_dir() or git("status", "--porcelain").strip():
        sys.exit("the working copy is not a clean copy of the tested-app fixture")
    if repair:
        text = pricing.read_text(encoding="utf-8")
        if text.count(data.BROKEN) != 1:
            sys.exit("parse_quantity is not the fixture's")
        pricing.write_text(text.replace(data.BROKEN, data.REPAIRED), encoding="utf-8")
        git("commit", "-q", "-a", "--amend", "--no-edit")
        if git("status", "--porcelain").strip() or git("rev-list", "--count", "HEAD").strip() != "1":
            sys.exit("the repair did not leave one commit and a clean tree")
    kernel("user", "user", "init")
    kernel("user", "user", "config", "set", "project.trust", "trusted", "--approval", data.TRUST_APPROVAL)
    print(json.dumps({"trusted": True, "repaired": bool(repair)}))
