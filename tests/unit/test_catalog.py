from __future__ import annotations

import json
import os

import pytest

from qa import catalog
from qa.errors import QaError

SUITE = "qa-engineering-suite"


def _skill(root, name, *, suite=SUITE, role="evaluator", consumes="codebase-map, test-plan", produces="execution", extra=""):
    folder = root / name
    folder.mkdir(parents=True)
    metadata = "metadata:\n" + ("  suite: %s\n" % suite if suite else "") + "  role: %s\n  consumes: \"%s\"\n  produces: %s\n" % (role, consumes, produces)
    (folder / "SKILL.md").write_text("---\nname: %s\ndescription: Does a thing. Use when asked.\n%s%s---\n\n# Body\n" % (name, metadata, extra))
    return folder


def test_catalog_reads_only_suite_skills(tmp_path):  # metadata.suite must equal qa-engineering-suite
    _skill(tmp_path, "qa-security")
    _skill(tmp_path, "someone-elses", suite="another-suite")
    _skill(tmp_path, "no-suite", suite="")
    (tmp_path / "plain-folder").mkdir()
    (tmp_path / "stray.md").write_text("---\nname: stray\nmetadata:\n  suite: %s\n---\n" % SUITE)
    assert [s["name"] for s in catalog.list_skills(tmp_path)] == ["qa-security"]


def test_an_entry_names_the_role_and_the_artifact_types(tmp_path):
    _skill(tmp_path, "qa-security", role="specialist", consumes="codebase-map, test-plan", produces="finding")
    assert catalog.list_skills(tmp_path) == [{"name": "qa-security", "dir": "qa-security", "role": "specialist",
                                              "consumes": ["codebase-map", "test-plan"], "produces": ["finding"]}]


def test_entries_are_sorted_by_name(tmp_path):
    for name in ("qa-zeta", "qa-alpha", "qa-mid"):
        _skill(tmp_path, name)
    assert [s["name"] for s in catalog.list_skills(tmp_path)] == ["qa-alpha", "qa-mid", "qa-zeta"]


def test_catalog_does_not_execute_or_follow_links(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    real = _skill(tmp_path / "elsewhere", "qa-linked")
    (skills / "qa-linked").symlink_to(real, target_is_directory=True)  # a linked skill directory
    linked_file = skills / "qa-file"
    linked_file.mkdir()
    (linked_file / "SKILL.md").symlink_to(real / "SKILL.md")  # a linked SKILL.md
    canary = tmp_path / "ran"
    _skill(skills, "qa-bait", extra="x: $(touch %s)\n" % canary)
    (skills / "qa-bait" / "run.py").write_text("open(%r, 'w').write('ran')\n" % str(canary))
    assert [s["name"] for s in catalog.list_skills(skills)] == ["qa-bait"] and not canary.exists()


def test_a_fifo_or_a_huge_skill_file_does_not_hang_or_list(tmp_path):
    os.mkfifo(str(tmp_path / "fifo"))
    (tmp_path / "huge").mkdir()
    (tmp_path / "huge" / "SKILL.md").write_text("---\nname: huge\nmetadata:\n  suite: %s\n---\n%s" % (SUITE, "x" * (1 << 20)))
    (tmp_path / "qa-fifo").mkdir()
    os.mkfifo(str(tmp_path / "qa-fifo" / "SKILL.md"))
    _skill(tmp_path, "qa-ok")
    assert [s["name"] for s in catalog.list_skills(tmp_path)] == ["qa-ok"]


@pytest.mark.parametrize("text", [
    "---\nname: qa-x\nmetadata:\n  suite: qa-engineering-suite\n",  # no closing fence
    "\n---\nname: qa-x\nmetadata:\n  suite: qa-engineering-suite\n---\n",  # the fence is not on line one
    "name: qa-x\nmetadata:\n  suite: qa-engineering-suite\n",  # no fence at all
], ids=["unclosed", "late-fence", "no-fence"])
def test_frontmatter_must_open_the_file_and_close(tmp_path, text):
    folder = tmp_path / "qa-x"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(text)
    assert catalog.list_skills(tmp_path) == []


def test_a_bad_name_is_not_listed(tmp_path):
    _skill(tmp_path, "QA Bad Name")
    assert catalog.list_skills(tmp_path) == []


def test_crlf_comments_and_quotes_are_read(tmp_path):
    folder = tmp_path / "qa-x"
    folder.mkdir(parents=True)
    text = "---\r\n# a comment\r\nname: 'qa-x'\r\nmetadata:\r\n  suite: \"%s\"\r\n  role: orchestrator\r\n---\r\n" % SUITE
    (folder / "SKILL.md").write_bytes(text.encode())
    assert catalog.list_skills(tmp_path) == [{"name": "qa-x", "dir": "qa-x", "role": "orchestrator", "consumes": [], "produces": []}]


def test_a_missing_directory_is_refused(tmp_path):
    with pytest.raises(QaError) as caught:
        catalog.list_skills(tmp_path / "nope")
    assert caught.value.code == "skills_dir_not_found"


def test_the_default_directory_in_the_source_tree_is_skills_at_the_repo_root():
    found = catalog.default_skills_dir()
    assert found is None or found.name == "skills"


def test_the_default_directory_of_a_vendored_copy_is_the_sibling_of_the_skill(tmp_path):
    kernel_file = tmp_path / "skills" / "qa-orchestrator" / "scripts" / "_kernel" / "qa" / "catalog.py"
    kernel_file.parent.mkdir(parents=True)
    assert catalog.default_skills_dir(str(kernel_file)) == (tmp_path / "skills").resolve()


def test_an_unrecognised_layout_has_no_default(tmp_path):
    odd = tmp_path / "somewhere" / "qa" / "catalog.py"
    odd.parent.mkdir(parents=True)
    assert catalog.default_skills_dir(str(odd)) is None


def test_the_cli_lists_the_catalog(ws, qa_cli, tmp_path):
    _skill(tmp_path / "skills", "qa-security")
    code, out = qa_cli("catalog", "list", "--skills-dir", str(tmp_path / "skills"))
    assert code == 0 and [s["name"] for s in out["skills"]] == ["qa-security"] and "limitations" not in out
    code, out = qa_cli("catalog", "list", "--skills-dir", str(tmp_path / "nope"))
    assert (code, out["error"]["code"]) == (2, "skills_dir_not_found")
