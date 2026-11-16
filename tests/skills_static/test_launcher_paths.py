"""Skill text names no install path: every call of a skill's own script is ``python3 <skill-dir>/scripts/<x>.py``, and each
SKILL.md says once, in its Tools section, what ``<skill-dir>`` is. The same text then works wherever the host installed the skill
(a project's ``.claude/skills/``, the user's skills, or a plugin's directory), because the host shows the base directory when the
skill loads."""
from __future__ import annotations

import re

import pytest

from helpers import REPO

SKILLS = REPO / "skills"
AGENTS = REPO / "agents"
INSTALL_PATH = b".claude/skills/"
DEFINITION = ("`<skill-dir>` is the directory that holds this SKILL.md; the host shows it as the skill's base directory when "
              "the skill loads.")
SKILL_FILES = sorted(SKILLS.glob("*/SKILL.md"))

# Text that describes the project copy a generated CI job runs, not a call the agent makes: a CI runner sees only the
# repository, so that job needs the skill committed under the project's `.claude/skills/`. One reason per entry.
CI_COPY = {
    ("skills/qa-release/SKILL.md", "a project copy of this skill under `.claude/skills/qa-release/`"):
        "the CI job generated in step 10 runs the launcher the repository tracks, so the user needs a project copy",
    ("skills/qa-release/references/ci-reexecution.md", "a project copy of this skill under `.claude/skills/qa-release/`"):
        "the generated job's launcher path is the project copy; a runner never sees where a plugin was installed",
    ("skills/qa-release/references/ci-reexecution.md", "`--launcher .claude/skills/qa-release/scripts/qa.py`"):
        "names that project copy for the job when the skill was loaded from outside the project",
}


def shipped_files():
    """Every file a skill or an agent ships, outside the evaluation cases and the vendored kernel."""
    for root in (SKILLS, AGENTS):
        for path in sorted(root.rglob("*")):
            parts = path.relative_to(REPO).parts
            if path.is_file() and not {"evals", "_kernel", "__pycache__"} & set(parts):
                yield path


def test_skill_text_names_no_install_path():
    found = []
    for path in shipped_files():
        name = path.relative_to(REPO).as_posix()
        data = path.read_bytes()
        for (where, phrase), _reason in CI_COPY.items():
            if where == name:
                data = data.replace(phrase.encode("utf-8"), b"")
        for number, line in enumerate(data.splitlines(), 1):
            if INSTALL_PATH in line:
                found.append("%s:%d" % (name, number))
    assert found == []


def test_every_ci_copy_exemption_is_still_used():
    for (where, phrase), reason in CI_COPY.items():
        assert reason and phrase in (REPO / where).read_text(encoding="utf-8"), (where, phrase)


@pytest.mark.parametrize("path", SKILL_FILES, ids=[p.parent.name for p in SKILL_FILES])
def test_skill_md_defines_skill_dir_once(path):
    text = path.read_text(encoding="utf-8")
    assert text.count(DEFINITION) == 1
    tools = re.search(r"^## Tools\n(.*?)^## ", text, re.S | re.M)
    assert tools and DEFINITION in tools.group(1)
    assert "${" not in text


def test_there_is_a_skill_md_for_every_skill():
    assert len(SKILL_FILES) == 16


@pytest.mark.parametrize("path", [p for p in shipped_files() if p.suffix == ".md"], ids=lambda p: p.relative_to(REPO).as_posix())
def test_scripts_are_called_from_the_skill_dir(path):
    """A skill's own script is called through ``<skill-dir>``, never through a relative or a placeholder path of its own,
    and ``<skill-dir>`` is never written after other path steps (``../../../<skill-dir>``)."""
    text = path.read_text(encoding="utf-8")
    other = re.findall(r"python3? (?!<skill-dir>/)(?:-\S+ )*([^\s`'\"]*scripts/[\w.-]+\.py)", text)
    assert other == []
    assert not re.search(r"[\w./]<skill-dir>", text)


@pytest.mark.parametrize("path", SKILL_FILES, ids=[p.parent.name for p in SKILL_FILES])
def test_a_launcher_call_names_its_own_skill(path):
    """``<skill-dir>`` is this skill's directory, so a launcher call through it names this skill, in SKILL.md and references."""
    skill = path.parent.name
    for text_path in [path] + sorted((path.parent / "references").glob("*.md")):
        text = text_path.read_text(encoding="utf-8")
        named = re.findall(r"python3 <skill-dir>/scripts/qa\.py --role \w+ --skill ([\w-]+)", text)
        assert set(named) <= {skill}, text_path
