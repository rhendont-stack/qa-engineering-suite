"""Scenario "Spanish requirements file": writes requisitos.md into the working copy and folds it into the copy's one commit.

Run by tools/run_skill_eval.py with the copy as working directory, never by an agent. The case excludes the fixture's own
requirements.md, so the Spanish file is the only requirements source in the copy, and amending the harness's single commit keeps
the history the shape every other copy has (one commit, nothing untracked).
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _spanish  # noqa: E402

target = Path(_spanish.NAME)
if target.exists():
    sys.exit("the working copy already holds %s" % _spanish.NAME)
if not Path(".git").is_dir():
    sys.exit("the working copy has no git history to fold the file into")
target.write_text(_spanish.TEXT, encoding="utf-8")
env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="eval", GIT_AUTHOR_EMAIL="eval@example.invalid",
           GIT_COMMITTER_NAME="eval", GIT_COMMITTER_EMAIL="eval@example.invalid")
for args in (["add", _spanish.NAME], ["-c", "commit.gpgsign=false", "commit", "-q", "--amend", "--no-edit", "--reset-author"]):
    subprocess.run(["git"] + args, check=True, env=env, capture_output=True, timeout=60)
print("wrote %s (%d lines) into the initial commit" % (_spanish.NAME, len(_spanish.TEXT.splitlines())))
