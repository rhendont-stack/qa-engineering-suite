"""L0 tests for tools/check_packaging.py (run as ``python -I``): the marketplace, its two bundles and the guard entry."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

from helpers import REPO

TOOL = REPO / "tools" / "check_packaging.py"
MARKETPLACE = ".claude-plugin/marketplace.json"
GUARD_MANIFEST = "plugins/qa-command-guard/.claude-plugin/plugin.json"


def check(root):
    r = subprocess.run([sys.executable, "-I", str(TOOL), str(root)], capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


def kinds(out):
    return {line.split(": ")[1] for line in out.splitlines() if line.count(": ") >= 2}


@pytest.fixture
def root(tmp_path):
    """A repository root with the shipped marketplace, catalog, agents and guard manifest, and a stub for every skill."""
    for name in (MARKETPLACE, GUARD_MANIFEST, "catalog.json"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / name, tmp_path / name)
    shutil.copytree(REPO / "agents", tmp_path / "agents")
    for name in json.loads((tmp_path / "catalog.json").read_text())["skills"]:
        write_skill(tmp_path, name)
    return tmp_path


def write_skill(root, name, version="0.1.0"):
    skill = root / "skills" / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("---\nname: %s\ndescription: Use when a test needs it.\nmetadata:\n  suite: qa-engineering-suite\n"
                                    "  suite-version: \"%s\"\n---\n\n# %s\n" % (name, version, name))


def edit(root, relative, change):
    path = root / relative
    doc = json.loads(path.read_text())
    change(doc)
    path.write_text(json.dumps(doc, indent=2))


def entry(doc, name):
    return next(e for e in doc["plugins"] if e["name"] == name)


def test_packaging_accepts_the_shipped_manifest():
    code, out = check(REPO)
    assert code == 0 and out.strip().endswith("0 problem(s)"), out
    doc = json.loads((REPO / MARKETPLACE).read_text())
    assert [e["name"] for e in doc["plugins"]] == ["qa-engineering-suite", "qa-core", "qa-specialists", "qa-command-guard"]


def test_packaging_accepts_a_copy_of_the_shipped_manifest(root):
    assert check(root)[0] == 0


def test_packaging_bundles_partition_the_catalog(root):
    # a 17th skill that no bundle lists
    catalog = json.loads((root / "catalog.json").read_text())
    catalog["skills"].append("qa-extra")
    (root / "catalog.json").write_text(json.dumps(catalog))
    write_skill(root, "qa-extra")
    code, out = check(root)
    assert code == 1 and "bundle_gap" in kinds(out) and "qa-extra" in out
    # a skill in both bundles
    edit(root, MARKETPLACE, lambda d: entry(d, "qa-core")["skills"].append("./skills/qa-security"))
    assert "bundle_overlap" in kinds(check(root)[1])
    # a skill the catalog does not list
    edit(root, MARKETPLACE, lambda d: entry(d, "qa-specialists")["skills"].append("./skills/qa-unlisted"))
    assert "unknown_skill" in kinds(check(root)[1])
    # a listed path with no SKILL.md
    edit(root, MARKETPLACE, lambda d: entry(d, "qa-specialists")["skills"].append("./skills/qa-nothing-here"))
    shutil.rmtree(root / "skills" / "qa-mutation")
    assert "missing_path" in kinds(check(root)[1])


def test_packaging_refuses_root_plugin_manifest_and_reserved_names(root):
    (root / ".claude-plugin" / "plugin.json").write_text("{}")
    (root / "hooks").mkdir()
    (root / "hooks" / "hooks.json").write_text("{}")
    (root / "bin").mkdir()
    (root / "CLAUDE.md").write_text("x\n")
    code, out = check(root)
    assert code == 1 and out.count("forbidden_root_file") == 4
    edit(root, MARKETPLACE, lambda d: entry(d, "qa-core").update(name="claude-qa-core"))
    out = check(root)[1]
    assert "reserved_name" in kinds(out) and "wrong_entries" in kinds(out)
    edit(root, MARKETPLACE, lambda d: d.update(name="anthropic-marketplace"))
    assert out != check(root)[1] and "reserved_name" in kinds(check(root)[1])


def test_packaging_versions_agree(root):
    edit(root, MARKETPLACE, lambda d: entry(d, "qa-core").update(version="0.1.0-dev.2"))
    code, out = check(root)
    assert code == 1 and "version_mismatch" in kinds(out)
    edit(root, MARKETPLACE, lambda d: [e.update(version="0.2.0") for e in d["plugins"]])
    assert "version_mismatch" in kinds(check(root)[1])  # agrees with itself, not with the skills' suite-version
    edit(root, MARKETPLACE, lambda d: [e.update(version="0.1.0") for e in d["plugins"]])
    edit(root, GUARD_MANIFEST, lambda d: d.update(version="0.1.0"))
    assert check(root)[0] == 0  # the plain suite version is allowed too


def test_packaging_checks_metadata_and_the_guard_manifest(root):
    edit(root, MARKETPLACE, lambda d: d.update(owner={"name": "someone"}))
    assert "bad_metadata" in kinds(check(root)[1])
    edit(root, MARKETPLACE, lambda d: d.update(owner={"name": "rhendont-stack", "url": "https://github.com/rhendont-stack"}))
    edit(root, MARKETPLACE, lambda d: entry(d, "qa-core").update(license="MIT"))
    assert "bad_metadata" in kinds(check(root)[1])
    edit(root, MARKETPLACE, lambda d: entry(d, "qa-core").update(license="Apache-2.0"))
    edit(root, GUARD_MANIFEST, lambda d: d.update(name="other"))
    assert "wrong_entries" in kinds(check(root)[1])
    (root / GUARD_MANIFEST).unlink()
    assert "missing_manifest" in kinds(check(root)[1])
    (root / GUARD_MANIFEST).write_text("{not json")
    assert "bad_json" in kinds(check(root)[1])


def test_packaging_bundles_use_strict_false_and_list_agents_only_in_core(root):
    edit(root, MARKETPLACE, lambda d: entry(d, "qa-specialists").pop("strict"))
    assert "wrong_entries" in kinds(check(root)[1])
    edit(root, MARKETPLACE, lambda d: entry(d, "qa-specialists").update(strict=False))
    edit(root, MARKETPLACE, lambda d: entry(d, "qa-core")["agents"].pop())
    assert "wrong_entries" in kinds(check(root)[1])
    edit(root, MARKETPLACE, lambda d: entry(d, "qa-core").update(agents=["./agents/qa-auditor.md", "./agents/qa-evaluator.md", "./agents/gone.md"]))
    assert "missing_path" in kinds(check(root)[1])


def test_packaging_reports_a_missing_or_broken_marketplace(tmp_path):
    code, out = check(tmp_path)
    assert code == 1 and "missing_manifest" in kinds(out)
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / MARKETPLACE).write_text("[")
    assert "bad_json" in kinds(check(tmp_path)[1])
    assert subprocess.run([sys.executable, "-I", str(TOOL), "a", "b"], capture_output=True).returncode == 2
