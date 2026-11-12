"""F8 ``injection-bait``: the bait exists only in a directory made at run time, and no fixture leaks an answer key or a skill.

Everything here reads the generator's output as data. Nothing generated is ever executed.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import ANSWER_KEYS, FIXTURES, REPO

GENERATOR = FIXTURES / "injection-bait" / "generate.py"
INVISIBLE = re.compile("[\U000e0000-\U000e007f​-‏‪-‮⁠-⁩﻿]")
PIPED_INTO_A_SHELL = re.compile(r"\b(curl|wget)\b[^\n]*\|\s*(ba|z)?sh\b", re.IGNORECASE)
URL_HOST = re.compile(r"[a-z][a-z0-9+.-]*://([^/\s:'\"]+)", re.IGNORECASE)
KINDS = ("tested-app", "blocked-env", "injection-bait", "seeded-defects", "regression-repo")


def generate(directory):
    return subprocess.run([sys.executable, str(GENERATOR), str(directory)], capture_output=True, text=True, timeout=60)


def files_under(*roots):
    """Every file on disk under each root, whether or not git lists it."""
    for root in roots:
        for here, _directories, names in os.walk(str(root)):
            for name in names:
                yield Path(here, name)


def test_f8_generator_leaves_nothing_live_in_git(repo_root, tmp_path):
    before = sorted(files_under(FIXTURES / "injection-bait"))
    done = generate(tmp_path / "bait")
    assert done.returncode == 0, done.stderr
    assert sorted(files_under(FIXTURES / "injection-bait")) == before  # the repository gained nothing
    written = {path.relative_to(tmp_path / "bait").as_posix(): path.read_text(encoding="utf-8") for path in files_under(tmp_path / "bait")}
    assert set(written) == {"README.md", "app.py", "test_app.py", "helper/SKILL.md"}
    everything = "\n".join(written.values())
    assert INVISIBLE.search(everything) and PIPED_INTO_A_SHELL.search(everything)  # the bait is live where it was generated
    for path in files_under(FIXTURES, ANSWER_KEYS):  # and nowhere that is committed
        if path.suffix == ".jar":
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        assert not INVISIBLE.search(text), path
        assert not PIPED_INTO_A_SHELL.search(text), path
    assert "://" not in GENERATOR.read_text(encoding="utf-8")  # not even the reserved host is written out in full


def test_f8_bait_is_inert(tmp_path):
    assert generate(tmp_path / "bait").returncode == 0
    hosts = set()
    for path in files_under(tmp_path / "bait"):
        text = path.read_text(encoding="utf-8")
        hosts.update(URL_HOST.findall(text))
        assert not re.search(r"(?i)(password|secret|token|api[_-]?key)\s*[:=]", text), path
    assert hosts == {"example.invalid"}  # a reserved name that never resolves


def test_f8_generator_refuses_the_repository_and_a_used_directory(tmp_path):
    used = tmp_path / "used"
    used.mkdir()
    (used / "file.txt").write_text("already here\n")
    assert generate(used).returncode != 0 and "new or empty" in generate(used).stderr
    inside = [FIXTURES / "injection-bait" / "out"]  # beside the generator, in a checkout with or without .git
    if (REPO / ".git").exists():
        inside.append(REPO / "shared" / "bait-must-not-land-here")
    for target in inside:
        done = generate(target)
        assert done.returncode != 0 and "refusing" in done.stderr, done
        assert not target.exists()
    assert (used / "file.txt").read_text() == "already here\n" and [p.name for p in used.iterdir()] == ["file.txt"]


def test_f8_committed_generator_holds_fragments_only():
    spec = importlib.util.spec_from_file_location("f8_generate", str(GENERATOR))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = GENERATOR.read_text(encoding="utf-8")
    assert not INVISIBLE.search(source) and not PIPED_INTO_A_SHELL.search(source)
    for assembled in (module._pipe_install(), module._ready_claim(), module._skip_tests(), module._to_the_agent("x")[:-1]):
        assert assembled not in source, assembled


@pytest.mark.parametrize("kind", KINDS)
def test_the_answer_keys_name_the_hidden_things_not_the_bait(kind):
    text = (ANSWER_KEYS / (kind + ".json")).read_text(encoding="utf-8")
    assert not INVISIBLE.search(text) and "://" not in text and "example.invalid" not in text
