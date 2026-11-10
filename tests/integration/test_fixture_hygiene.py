"""No fixture hints at what is wrong with it, and none carries an answer key or a skill.

A fixture is read by the agent under evaluation. What the harness places in its copy, and every commit message a generator writes,
must hold none of the words that name the thing being looked for.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import ANSWER_KEYS, FIXTURES, REPO, answer_key

SEED_WORDS = ("seed", "seeded", "flaky", "bug", "defect", "off-by-one", "mutant", "answer", "trap", "deliberate", "intentional", "hidden",
              "planted")
WORD = re.compile(r"(?<![A-Za-z0-9])(?:%s)(?![A-Za-z0-9])" % "|".join(re.escape(word) for word in SEED_WORDS), re.IGNORECASE)
KINDS = ("tested-app", "blocked-env", "injection-bait", "seeded-defects", "regression-repo")


def harness():
    spec = importlib.util.spec_from_file_location("run_skill_eval_for_hygiene", str(REPO / "tools" / "run_skill_eval.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def files_under(*roots):
    """Every file on disk under each root, whether or not git lists it."""
    for root in roots:
        for here, _directories, names in os.walk(str(root)):
            for name in names:
                yield Path(here, name)


def placed_in_a_copy(fixture):
    """The files the harness copies from a fixture that holds no generator."""
    skip = harness()._ignore
    for here, directories, names in os.walk(str(fixture)):
        skipped = set(skip(here, directories + names))
        directories[:] = [name for name in directories if name not in skipped]
        for name in names:
            if name not in skipped:
                yield Path(here, name)


def hits(label, text):
    return ["%s: %r" % (label, match.group(0)) for match in WORD.finditer(text)]


def readable(path):
    return None if path.suffix == ".jar" else path.read_bytes().decode("utf-8", errors="replace")


def generated(name, tmp_path):
    """What a generator makes, as ``(relative path, text)`` pairs, plus every commit message and diff it wrote."""
    directory = tmp_path / name
    done = subprocess.run([sys.executable, str(FIXTURES / name / "generate.py"), str(directory)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    found = []
    for path in files_under(directory):
        relative = path.relative_to(directory)
        if ".git" not in relative.parts:
            found.append((relative.as_posix(), readable(path)))
    if (directory / ".git").exists():
        log = subprocess.run(["git", "-C", str(directory), "log", "--all", "-p", "--name-only", "--format=%an %ae %cn %ce%n%B"],
                             capture_output=True, text=True, timeout=60)
        found.append(("the history of " + name, log.stdout))
        found.append(("the branches of " + name, subprocess.run(["git", "-C", str(directory), "for-each-ref"], capture_output=True,
                                                                text=True, timeout=60).stdout))
    return found


def test_fixtures_carry_no_seed_words(repo_root, tmp_path):
    found, no_git = [], False
    for fixture in sorted(path for path in FIXTURES.iterdir() if path.is_dir()):
        if (fixture / "generate.py").is_file():
            if shutil.which("git") is None and fixture.name == "regression-repo":
                no_git = True
                continue
            for label, text in generated(fixture.name, tmp_path):
                found += hits(fixture.name + "/" + label, label) + hits(fixture.name + "/" + label, text)
            continue
        for path in placed_in_a_copy(fixture):
            label = path.relative_to(FIXTURES).as_posix()
            found += hits(label, path.relative_to(fixture).as_posix())      # the fixture's own directory name is not placed in the copy
            text = readable(path)
            found += hits(label, text) if text is not None else []
    assert found == []
    if no_git:
        pytest.skip("tool missing: git (the generated history was not scanned)")


def test_the_scan_sees_a_seed_word_and_leaves_ordinary_ones_alone():
    assert hits("x", "A flaky test hid a Bug.") == ["x: 'flaky'", "x: 'Bug'"]
    assert hits("x", "off-by-one and OFF-BY-ONE and an answer.") == ["x: 'off-by-one'", "x: 'OFF-BY-ONE'", "x: 'answer'"]
    assert hits("x", "debugging seeds, hiddenly; a bugle, defective, answered") == []
    assert hits("x", "seed_data and the-trap and planted_items") == ["x: 'seed'", "x: 'trap'", "x: 'planted'"]
    assert len(SEED_WORDS) == 13


def test_fixtures_hold_no_answer_key_or_skill_md(repo_root):
    for path in files_under(FIXTURES):
        relative = path.relative_to(FIXTURES)
        assert path.name.lower() != "skill.md", relative
        assert "answer" not in path.name.lower() and "expected" not in path.name.lower(), relative
        assert "answer-keys" not in path.read_bytes().decode("utf-8", errors="replace"), relative
    for kind in KINDS:
        assert (FIXTURES / kind).is_dir()
        assert answer_key(kind)["fixture"] == kind
    assert sorted(p.name for p in ANSWER_KEYS.iterdir() if p.name[:-5] in KINDS) == sorted(kind + ".json" for kind in KINDS)
    keys = [p for p in files_under(repo_root / "fixtures", repo_root / "skills") if "answer-key" in p.as_posix()]
    assert keys == []


def test_the_fixtures_that_hold_a_generator_are_the_two_the_harness_generates():
    generators = sorted(path.parent.name for path in FIXTURES.glob("*/generate.py"))
    assert generators == ["injection-bait", "regression-repo"]
