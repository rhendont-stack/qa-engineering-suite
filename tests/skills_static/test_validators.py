"""L0 tests for tools/vendor_kernel.py, tools/validate_skills.py and tools/check_catalog.py (each run as ``python -I``)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

from helpers import REPO

TOOLS = REPO / "tools"
SECTIONS = ["Mission", "Boundaries", "Inputs", "Tools", "Safety rules", "Procedure", "Outputs", "Acceptance criteria",
            "When things go wrong", "Red flags", "References", "Example"]
FRONTMATTER = ("---\nname: qa-demo\ndescription: Demonstrates the layout. Use when a test needs a valid skill.\n"
               "license: Apache-2.0\ncompatibility: Requires python3 3.9+.\nmetadata:\n  suite: qa-engineering-suite\n"
               "  suite-version: \"0.1.0\"\n  role: verifier\n  consumes: \"codebase-map, test-plan\"\n  produces: \"execution\"\n---\n")


def tool(name, *args, cwd=None):
    r = subprocess.run([sys.executable, "-I", str(TOOLS / name), *map(str, args)], capture_output=True, text=True, cwd=cwd, timeout=120)
    return r.returncode, r.stdout + r.stderr


def skill_text(sections=SECTIONS, front=FRONTMATTER):
    return front + "\n# QA demo\n\n" + "".join("## %s\n\nSee [the notes](references/notes.md).\n\n" % s for s in sections)


@pytest.fixture
def tmp_repo(tmp_path):
    """A repository root holding a copy of ``shared/`` and one skill."""
    shutil.copytree(REPO / "shared", tmp_path / "shared", ignore=shutil.ignore_patterns("__pycache__"))
    skill = tmp_path / "skills" / "qa-demo"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text(skill_text())
    (skill / "references" / "notes.md").write_text("Notes.\n")
    return tmp_path


@pytest.fixture
def tmp_skill(tmp_repo):
    return tmp_repo / "skills" / "qa-demo"


# --------------------------------------------------------------------------- vendoring

def test_vendor_check_reports_one_changed_byte(tmp_repo):
    assert tool("vendor_kernel.py", "--check", "--root", tmp_repo)[0] == 1  # nothing vendored yet
    code, out = tool("vendor_kernel.py", "--root", tmp_repo)
    assert code == 0, out
    assert tool("vendor_kernel.py", "--check", "--root", tmp_repo)[0] == 0
    changed = tmp_repo / "skills" / "qa-demo" / "scripts" / "_kernel" / "qa" / "ids.py"
    data = bytearray(changed.read_bytes()); data[10] ^= 1; changed.write_bytes(bytes(data))
    code, out = tool("vendor_kernel.py", "--check", "--root", tmp_repo)
    assert code == 1 and out.count("differs:") == 1 and "_kernel/qa/ids.py" in out and "1 difference" in out


def test_vendor_never_copies_bytecode_and_check_flags_an_extra_file(tmp_repo):
    (tmp_repo / "shared" / "kernel" / "qa" / "__pycache__").mkdir()
    (tmp_repo / "shared" / "kernel" / "qa" / "__pycache__" / "x.pyc").write_bytes(b"x")
    assert tool("vendor_kernel.py", "--root", tmp_repo)[0] == 0
    kernel = tmp_repo / "skills" / "qa-demo" / "scripts" / "_kernel"
    assert not list(kernel.rglob("__pycache__")) and (kernel / "MANIFEST.sha256").is_file()
    (kernel / "qa" / "stray.py").write_text("x = 1\n")
    code, out = tool("vendor_kernel.py", "--check", "--root", tmp_repo)
    assert code == 1 and "extra: _kernel/qa/stray.py" in out
    assert tool("vendor_kernel.py", "--root", tmp_repo)[0] == 0 and tool("vendor_kernel.py", "--check", "--root", tmp_repo)[0] == 0


@pytest.mark.parametrize("name", ["vendor_kernel.py", "validate_skills.py", "check_catalog.py"])
def test_tools_pass_on_a_repository_with_no_skills(tmp_path, name):
    shutil.copytree(REPO / "shared", tmp_path / "shared", ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "catalog.json").write_text('{"version": 1, "skills": [], "responsibilities": []}\n')
    args = ["--check", "--root", tmp_path] if name == "vendor_kernel.py" else [tmp_path]
    code, out = tool(name, *args)
    assert code == 0 and "0 skill" in out, out


def test_vendored_launcher_runs_doctor(tmp_repo, tmp_path_factory):
    assert tool("vendor_kernel.py", "--root", tmp_repo)[0] == 0
    launcher = tmp_repo / "skills" / "qa-demo" / "scripts" / "qa.py"
    workspace = tmp_path_factory.mktemp("work")
    run = lambda *a: subprocess.run([sys.executable, str(launcher), "--workspace", str(workspace), *a], capture_output=True, text=True, timeout=120)
    assert run("init").returncode == 0
    r = run("doctor")
    out = json.loads(r.stdout)
    assert out["command"] == "doctor", r.stdout + r.stderr
    kernel = out["kernel"]
    assert kernel["status"] == "ok" and not kernel["source_checkout"] and kernel["manifest_sha256"]


# --------------------------------------------------------------------------- skill validation

def test_a_valid_skill_passes(tmp_skill, tmp_repo):
    code, out = tool("validate_skills.py", tmp_repo)
    assert code == 0 and "1 skill(s) checked, 0 problem(s)" in out, out


def _break(tmp_skill, defect):
    md = tmp_skill / "SKILL.md"
    if defect == "flow_list":
        md.write_text(md.read_text().replace('consumes: "codebase-map, test-plan"', "consumes: [codebase-map, test-plan]"))
    elif defect == "long_description":
        md.write_text(md.read_text().replace("Demonstrates the layout.", "x" * 300))
    elif defect == "unknown_field":
        md.write_text(md.read_text().replace("license:", "tags: a\nlicense:"))
    elif defect == "wrong_name":
        md.write_text(md.read_text().replace("name: qa-demo", "name: qa-other"))
    elif defect == "missing_section":
        md.write_text(skill_text([s for s in SECTIONS if s != "Red flags"]))
    elif defect == "section_out_of_order":
        swapped = list(SECTIONS); swapped[3], swapped[4] = swapped[4], swapped[3]
        md.write_text(skill_text(swapped))
    elif defect == "over_500_lines":
        md.write_text(md.read_text() + "filler\n" * 500)
    elif defect == "broken_link":
        (tmp_skill / "references" / "notes.md").unlink()
    elif defect == "empty_file":
        (tmp_skill / "references" / "notes.md").write_text("")
    elif defect == "section_sign":
        (tmp_skill / "references" / "notes.md").write_text("See the design, § 6.4.\n")


@pytest.mark.parametrize("defect", ["flow_list", "long_description", "unknown_field", "wrong_name", "missing_section",
                                    "section_out_of_order", "over_500_lines", "broken_link", "empty_file", "section_sign"])
def test_validator_rejects(tmp_skill, tmp_repo, defect):
    _break(tmp_skill, defect)
    code, out = tool("validate_skills.py", tmp_repo)
    kind = {"flow_list": "flow_style"}.get(defect, defect)
    assert code == 1 and (": %s:" % kind) in out, out
    assert "1 problem(s)" in out, out  # exactly the one defect, nothing collateral


def test_validator_ignores_the_vendored_kernel_but_not_a_link_out_of_the_skill(tmp_skill, tmp_repo):
    assert tool("vendor_kernel.py", "--root", tmp_repo)[0] == 0   # kernel sources cite section numbers
    assert tool("validate_skills.py", tmp_repo)[0] == 0
    (tmp_skill / "SKILL.md").write_text((tmp_skill / "SKILL.md").read_text() + "[x](../../README.md)\n")
    (tmp_repo / "README.md").write_text("r\n")
    assert ": link_escapes_skill:" in tool("validate_skills.py", tmp_repo)[1]


# --------------------------------------------------------------------------- catalog

def _catalog(root, skills, entries):
    (root / "catalog.json").write_text(json.dumps({"version": 1, "skills": skills,
                                                   "responsibilities": [{"key": k, "owner": o} for k, o in entries]}))


def test_catalog_refuses_two_owners_for_one_key(tmp_path):
    for name in ("qa-a", "qa-b"):
        d = tmp_path / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(FRONTMATTER.replace("qa-demo", name))
    _catalog(tmp_path, ["qa-a", "qa-b"], [("test-suite-changes", "qa-a")])
    assert tool("check_catalog.py", tmp_path)[0] == 0
    _catalog(tmp_path, ["qa-a", "qa-b"], [("test-suite-changes", "qa-a"), ("test-suite-changes", "qa-b")])
    code, out = tool("check_catalog.py", tmp_path)
    assert code == 1 and "several_owners" in out and "qa-a, qa-b" in out


def test_catalog_refuses_a_skill_that_is_listed_but_not_built_and_the_reverse(tmp_path):
    d = tmp_path / "skills" / "qa-demo"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(FRONTMATTER)
    _catalog(tmp_path, ["qa-ghost"], [("k", "qa-ghost")])
    code, out = tool("check_catalog.py", tmp_path)
    assert code == 1 and "skill_not_built" in out and "skill_not_listed" in out


def test_check_repo_holds_a_vendored_policy_to_the_same_schema(tmp_repo):
    assert tool("vendor_kernel.py", "--root", tmp_repo)[0] == 0
    assert tool("check_repo.py", tmp_repo)[0] == 0
    (tmp_repo / "skills" / "qa-demo" / "scripts" / "_kernel" / "policies" / "redaction.json").write_text("{}\n")
    code, out = tool("check_repo.py", tmp_repo)
    assert code == 1 and "_kernel/policies/redaction.json: policy_invalid" in out


# --------------------------------------------------------------------------- symlinks under a skill

def _snapshot(path):
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("mode", ["check", "write"])
@pytest.mark.parametrize("link", ["scripts", "_kernel", "qa.py"])
def test_vendoring_refuses_a_symlink_before_touching_anything(tmp_repo, tmp_path_factory, link, mode):
    assert tool("vendor_kernel.py", "--root", tmp_repo)[0] == 0
    scripts = tmp_repo / "skills" / "qa-demo" / "scripts"
    outside = tmp_path_factory.mktemp("outside")
    if link == "scripts":
        shutil.copytree(scripts, outside / "scripts")
        shutil.rmtree(scripts)
        scripts.symlink_to(outside / "scripts")
        guarded = outside
    elif link == "_kernel":
        shutil.copytree(scripts / "_kernel", outside / "_kernel")
        shutil.rmtree(scripts / "_kernel")
        (scripts / "_kernel").symlink_to(outside / "_kernel")
        guarded = outside
    else:
        shutil.copy(scripts / "qa.py", outside / "qa.py")
        (scripts / "qa.py").unlink()
        (scripts / "qa.py").symlink_to(outside / "qa.py")   # identical bytes: only the link itself is wrong
        guarded = outside
    # drift to prove that nothing is repaired through the link, and that the kernel in the skill is not half replaced
    victim = guarded / ("scripts/_kernel" if link == "scripts" else "_kernel" if link == "_kernel" else "qa.py")
    victim = victim / "qa" / "ids.py" if victim.is_dir() else victim
    if link != "qa.py":
        victim.write_bytes(victim.read_bytes() + b"# drift\n")
    before = _snapshot(guarded)
    code, out = tool("vendor_kernel.py", *(["--check"] if mode == "check" else []), "--root", tmp_repo)
    assert code == 1 and "symlink" in out, out
    assert _snapshot(guarded) == before
    assert (tmp_repo / "skills" / "qa-demo" / "scripts").is_symlink() == (link == "scripts")
    assert (scripts / "_kernel").is_symlink() == (link == "_kernel")
    assert (scripts / "qa.py").is_symlink() == (link == "qa.py")


def test_every_skill_python_file_outside_the_vendored_kernel_starts_with_the_annotations_import():
    """Python 3.9 is the floor: annotations stay strings, so a newer annotation form cannot break an older interpreter."""
    missing = []
    for path in sorted((REPO / "skills").glob("*/**/*.py")):
        parts = path.relative_to(REPO).parts
        if "_kernel" in parts or "__pycache__" in parts or path.name == "qa.py" or path.name.startswith("_") and path.stat().st_size == 0:
            continue
        if "from __future__ import annotations" not in path.read_text(encoding="utf-8"):
            missing.append(str(path.relative_to(REPO)))
    assert missing == []
