"""The three subagents the orchestrator dispatches: the verifier and the evaluator cannot edit files, and nothing leaks design notes."""
from __future__ import annotations

import re

import pytest

from helpers import REPO

AGENTS = REPO / "agents"
NAMES = ["qa-auditor", "qa-verifier", "qa-evaluator"]
EDITING = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def frontmatter(name):
    text = (AGENTS / (name + ".md")).read_text(encoding="utf-8")
    block = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert block, name
    fields = dict(line.split(": ", 1) for line in block.group(1).splitlines())
    return fields, text[block.end():]


def tools_of(name):
    return [t.strip() for t in frontmatter(name)[0]["tools"].split(",")]


@pytest.mark.parametrize("name", ["qa-verifier", "qa-evaluator"])
def test_verifier_and_evaluator_cannot_edit(name):
    tools = tools_of(name)
    assert not EDITING & set(tools), tools
    assert "Skill" in tools and "Read" in tools and "Bash" in tools


def test_the_auditor_cannot_edit_either():
    assert not EDITING & set(tools_of("qa-auditor"))


@pytest.mark.parametrize("name", NAMES)
def test_each_agent_is_well_formed(name):
    fields, body = frontmatter(name)
    assert fields["name"] == name and 40 <= len(fields["description"]) <= 300
    assert set(fields) == {"name", "description", "tools"}            # only fields every host reads
    assert "data" in body and "instructions" in body                  # the untrusted-data rule is stated
    assert "§" not in body and "§" not in fields["description"]
    assert "Task" not in tools_of(name) and "Agent" not in tools_of(name)    # subagents do not dispatch further


def test_no_other_agent_files_ship():
    assert sorted(p.stem for p in AGENTS.glob("*.md")) == sorted(NAMES)
