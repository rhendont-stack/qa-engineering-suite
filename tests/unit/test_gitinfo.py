from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

from qa import gitinfo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(root, *a):
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("1")
    git(r, "add", "a.txt")
    git(r, "commit", "-q", "-m", "c1")
    return r


def test_not_a_repo(tmp_path):
    assert gitinfo.git_info(tmp_path) == (None, "not_a_git_repo")


def test_clean_repo(repo):
    info, lim = gitinfo.git_info(repo)
    assert (
        lim is None
        and re.fullmatch(r"[0-9a-f]{40}", info["commit"])
        and info["branch"] == "main"
        and not info["dirty"]
    )
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", info["diff_hash"])


def test_no_commit(tmp_path):
    r = tmp_path / "fresh"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    assert gitinfo.git_info(r) == (None, "no_commit")


def test_qa_dir_never_dirties(repo):
    (repo / ".qa").mkdir()
    (repo / ".qa" / "x").write_text("y")
    assert not gitinfo.git_info(repo)[0]["dirty"]


def test_qa_config_never_changes_diff_hash(repo):
    h = gitinfo.git_info(repo)[0]["diff_hash"]
    (repo / ".qa").mkdir()
    (repo / ".qa" / "config.json").write_text('{"x": 1}')
    assert gitinfo.git_info(repo)[0]["diff_hash"] == h


def test_untracked_content_changes_diff_hash(repo):
    (repo / "new.txt").write_text("a")
    h1 = gitinfo.git_info(repo)[0]
    (repo / "new.txt").write_text("b")
    h2 = gitinfo.git_info(repo)[0]
    assert h1["dirty"] and h1["diff_hash"] != h2["diff_hash"]


def test_tracked_change_changes_diff_hash(repo):
    h0 = gitinfo.git_info(repo)[0]["diff_hash"]
    (repo / "a.txt").write_text("2")
    info = gitinfo.git_info(repo)[0]
    assert info["dirty"] and info["diff_hash"] != h0


def test_binary_change_changes_diff_hash(repo):
    (repo / "b.bin").write_bytes(b"\x00\x01\x02\x03")
    git(repo, "add", "b.bin")
    git(repo, "commit", "-q", "-m", "bin")
    (repo / "b.bin").write_bytes(b"\x00\x01\x02\x04")
    h1 = gitinfo.git_info(repo)[0]["diff_hash"]
    (repo / "b.bin").write_bytes(b"\x00\x01\x02\x05")
    h2 = gitinfo.git_info(repo)[0]["diff_hash"]
    assert h1 != h2


def test_detached_head_has_no_branch(repo):
    git(repo, "checkout", "-q", "--detach")
    assert gitinfo.git_info(repo)[0]["branch"] is None


def test_fsmonitor_hook_not_executed(repo, tmp_path):
    hook = tmp_path / "hook.sh"
    marker = tmp_path / "ran"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    git(repo, "config", "core.fsmonitor", str(hook))
    gitinfo.git_info(repo)
    assert not marker.exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no os.mkfifo")
def test_untracked_fifo_does_not_hang(repo):
    os.mkfifo(repo / "pipe")
    info, lim = gitinfo.git_info(repo)
    assert lim is None and info["commit"]


@pytest.mark.skipif(not hasattr(os, "symlink") or not os.path.exists("/dev/zero"), reason="no symlink or /dev/zero")
def test_untracked_symlink_to_dev_zero_does_not_hang(repo):
    os.symlink("/dev/zero", repo / "zero")
    assert gitinfo.git_info(repo)[0]["dirty"]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlink")
def test_untracked_symlink_hashed_by_target(repo):
    os.symlink("one", repo / "link")
    h1 = gitinfo.git_info(repo)[0]["diff_hash"]
    os.remove(repo / "link")
    os.symlink("two", repo / "link")
    h2 = gitinfo.git_info(repo)[0]["diff_hash"]
    assert h1 != h2


def test_git_missing(repo, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert gitinfo.git_info(repo) == (None, "git_unavailable")


def test_git_cannot_start(repo, monkeypatch):
    def boom(*a, **k):
        raise OSError("cannot exec")

    monkeypatch.setattr(gitinfo.subprocess, "run", boom)
    assert gitinfo.git_info(repo) == (None, "git_unavailable")


def test_git_timeout(repo, monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=30)

    monkeypatch.setattr(gitinfo.subprocess, "run", slow)
    assert gitinfo.git_info(repo) == (None, "git_unavailable")
