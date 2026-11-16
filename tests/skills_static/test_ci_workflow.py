"""The CI workflow's packaging checks, read as text (standard library only)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
SKILLS = sorted(p.name for p in (REPO / "skills").iterdir() if p.is_dir() and not p.name.startswith("."))
VALIDATOR_STEP = "Skills pass the reference validator"


def _job(name: str) -> str:
    match = re.search(r"^  %s:\n(.*?)(?=^  \S|\Z)" % name, WORKFLOW, re.M | re.S)
    assert match, name
    return match.group(1)


def _run_block(step: str) -> str:
    """The ``run: |`` script of the l0 step named ``step``, as the runner's shell gets it."""
    match = re.search(r"^      - name: %s\n(?:        #.*\n)*        run: \|\n((?:          .*\n|\n)+)" % re.escape(step), _job("l0"), re.M)
    assert match, step
    return "".join(line[10:] if line.strip() else "\n" for line in match.group(1).splitlines(True))


def test_ci_l0_validates_packaging_with_pinned_tools():
    assert re.findall(r"^  ([a-z0-9]+):$", WORKFLOW.split("\njobs:\n", 1)[1], re.M) == ["l0", "l1"]
    l0 = _job("l0")
    assert "timeout-minutes: 20" in l0
    names = re.findall(r"^      - name: (.*)$", l0, re.M)
    expected = [
        "Repository has no empty files, bad JSON or unclosed schemas",
        "Vendored kernels equal the shared source",
        "Skills follow the specification and the template",
        "Every responsibility has exactly one owner",
        "Packaging follows the bundle rules",
        "Eval cases match their prompt tables",
        "Install the pinned reference validator",
        "Skills pass the reference validator",
    ]
    assert names == expected
    assert "run: python -I tools/check_packaging.py" in l0
    # The eval cases are checked, never written, right after the packaging step.
    build = "      - name: Eval cases match their prompt tables\n        run: python -I tools/build_evals.py --check\n"
    assert "        run: python -I tools/check_packaging.py\n" + build in l0
    install = re.search(r'python -m pip install "skills-ref @ git\+https://github\.com/agentskills/agentskills@([0-9a-f]+)#subdirectory=skills-ref"', l0)
    assert install and re.fullmatch(r"[0-9a-f]{40}", install.group(1))
    assert len(SKILLS) == 16
    # One loop names every skill once, in order, and validates each as skills/<name>.
    block = _run_block(VALIDATOR_STEP)
    assert re.findall(r"^\s+(qa-[a-z-]+)(?: \\)?$", block, re.M) == SKILLS
    assert re.findall(r'skills-ref validate ("[^"\n]*")', block) == ['"skills/$skill"']
    # The step that installs the validator runs on a Python the package supports (>= 3.11).
    assert 'python-version: "3.15"' in l0
    # The CLI's install script is needed to get a working binary, so CI does not install it.
    assert "claude plugin validate runs on the Mac" in l0
    assert "npm install" not in l0 and "claude plugin validate --strict" not in l0


def test_ci_has_no_floating_references():
    assert "@latest" not in WORKFLOW
    for ref in re.findall(r"^\s*- uses: (\S+)", WORKFLOW, re.M):
        assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", ref), ref
    assert "secrets." not in WORKFLOW


# A stand-in for skills-ref: logs each call and fails for the skills named in FAKE_FAILING.
FAKE_SKILLS_REF = """#!/bin/sh
echo "$*" >> "$FAKE_LOG"
for bad in $FAKE_FAILING; do
  if [ "$2" = "skills/$bad" ]; then echo "$2: invalid"; exit 1; fi
done
exit 0
"""


@pytest.mark.parametrize("failing", [[], ["qa-api-testing"], ["qa-api-testing", "qa-release", "qa-test-strategy"]],
                         ids=["none", "first", "three"])
def test_the_reference_validator_step_validates_every_skill_then_fails_on_any(tmp_path, failing):
    """A failing skill does not stop the step: every skill is validated, every failure is named, and the step fails at the
    end. Run as the runner runs a ``run`` step on Linux, ``bash -e``."""
    bash = shutil.which("bash")
    assert bash, "the step's shell is bash"
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "skills-ref").write_text(FAKE_SKILLS_REF)
    (tools / "skills-ref").chmod(0o755)
    (tmp_path / "step.sh").write_text(_run_block(VALIDATOR_STEP))
    env = {"PATH": "%s%s%s" % (tools, os.pathsep, os.environ.get("PATH", "/usr/bin:/bin")), "FAKE_LOG": str(tmp_path / "calls"),
           "FAKE_FAILING": " ".join(failing)}
    done = subprocess.run([bash, "-e", str(tmp_path / "step.sh")], cwd=str(tmp_path), env=env, capture_output=True, text=True,
                          timeout=60)
    assert (tmp_path / "calls").read_text().splitlines() == ["validate skills/%s" % s for s in SKILLS]
    out = done.stdout + done.stderr
    assert done.returncode == (1 if failing else 0), out
    for skill in failing:
        assert "skills/%s: invalid" % skill in out
    if failing:
        assert ("The reference validator failed for:" + "".join(" " + s for s in failing)) in out, out
