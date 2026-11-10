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


def git_text(directory, *args):
    done = subprocess.run(["git", "-C", str(directory)] + list(args), capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, "git %s failed: %s" % (" ".join(args), done.stderr)
    return done.stdout


def history_texts(directory):
    """``(label, text)`` for every commit of the repository in ``directory``: its author fields and message, its file names and the
    content of every file in its tree. A copy made with ``checkout: N`` holds the tree of commit N, so every tree counts."""
    found = []
    for number, commit in enumerate(git_text(directory, "rev-list", "--reverse", "--all").split()):
        label = "commit %d" % number
        found.append((label, git_text(directory, "log", "-1", "--format=%an %ae %cn %ce%n%B", commit)))
        for name in git_text(directory, "ls-tree", "-r", "--name-only", commit).splitlines():
            found.append((label + " " + name, name))
            if not name.endswith(".jar"):
                found.append((label + " " + name, git_text(directory, "show", "%s:%s" % (commit, name))))
    found.append(("refs", git_text(directory, "for-each-ref")))
    return found


def generated(name, tmp_path):
    """What a generator makes, as ``(label, text)`` pairs: the files it writes and, when it makes a history, every commit."""
    directory = tmp_path / name
    done = subprocess.run([sys.executable, str(FIXTURES / name / "generate.py"), str(directory)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    found = []
    for path in files_under(directory):
        relative = path.relative_to(directory)
        if ".git" not in relative.parts:
            found.append((relative.as_posix(), readable(path)))
    if (directory / ".git").exists():
        found += history_texts(directory)
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


def test_the_history_scan_reads_files_that_later_commits_replace(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("tool missing: git")
    repo = tmp_path / "repo"
    repo.mkdir()
    env = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    for number, text in enumerate(("a planted word\n", "a clean line\n")):
        (repo / "notes.txt").write_text(text)
        if number == 0:
            git_text(repo, "init", "-q")
        git_text(repo, "add", "-A")
        git_text(repo, *env, "commit", "-q", "-m", "change %d" % number)
    everything = "\n".join(text for _label, text in history_texts(repo))
    assert hits("x", everything) == ["x: 'planted'"]            # found in a version the tip tree no longer holds
    with pytest.raises(AssertionError, match="failed"):
        history_texts(tmp_path)                                    # not a repository: a failing git fails the scan, never an empty one


def test_fixtures_hold_no_answer_key_or_skill_md(repo_root):
    for path in files_under(FIXTURES):
        relative = path.relative_to(FIXTURES)
        assert not path.name.lower().startswith("skill.md"), relative       # nor a template that becomes one
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
