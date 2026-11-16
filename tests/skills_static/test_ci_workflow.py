"""The CI workflow's packaging checks, read as text (standard library only)."""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def _job(name: str) -> str:
    match = re.search(r"^  %s:\n(.*?)(?=^  \S|\Z)" % name, WORKFLOW, re.M | re.S)
    assert match, name
    return match.group(1)


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
        "Install the pinned reference validator",
        "Skills pass the reference validator",
    ]
    assert names == expected
    assert "run: python -I tools/check_packaging.py" in l0
    install = re.search(r'python -m pip install "skills-ref @ git\+https://github\.com/agentskills/agentskills@([0-9a-f]+)#subdirectory=skills-ref"', l0)
    assert install and re.fullmatch(r"[0-9a-f]{40}", install.group(1))
    skills = sorted(p.name for p in (REPO / "skills").iterdir() if p.is_dir() and not p.name.startswith("."))
    assert len(skills) == 16
    assert re.findall(r"^\s+skills-ref validate (skills/\S+)$", l0, re.M) == ["skills/%s" % s for s in skills]
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
