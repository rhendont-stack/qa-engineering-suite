from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

from helpers import HOSTILE_GIT, hostile_git_repo
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


def test_untracked_files_count_whatever_the_users_git_config_says(tmp_path):  # M2
    if shutil.which("git") is None: pytest.skip("tool missing: git")
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "a.txt").write_text("1")
    for args in (["add", "a.txt"], ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "c"],
                 ["config", "status.showUntrackedFiles", "no"]):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True)
    assert gitinfo.git_info(tmp_path)[0]["dirty"] is False
    (tmp_path / "new.txt").write_text("2")
    assert gitinfo.git_info(tmp_path)[0]["dirty"] is True


# --------------------------------------------------------------------------- a hostile repository's own git config (C1)

@pytest.mark.parametrize("how", HOSTILE_GIT)
def test_a_program_the_repository_configures_never_runs(tmp_path, how):
    root = hostile_git_repo(tmp_path / "repo", tmp_path / "PWNED", how)
    info, lim = gitinfo.git_info(root)
    assert not (tmp_path / "PWNED").exists()
    assert lim is None and info["commit"]


@pytest.mark.parametrize("how", HOSTILE_GIT)
def test_the_file_listings_run_no_repository_program_either(tmp_path, how):
    root = hostile_git_repo(tmp_path / "repo", tmp_path / "PWNED", how)
    gitinfo.project_files(root)
    gitinfo.tracked_under_qa(root)
    assert not (tmp_path / "PWNED").exists()


def test_a_neutralised_filter_leaves_the_modified_file_dirty(tmp_path):
    root = hostile_git_repo(tmp_path / "repo", tmp_path / "PWNED")
    assert gitinfo.git_info(root)[0]["dirty"] is True


def _global_upcase_filter(tmp_path, monkeypatch):
    """A user-level filter (as git-lfs installs one) whose clean step upper-cases ``*.txt``; the repository commits through it."""
    cfg = tmp_path / "global.gitconfig"
    cfg.write_text('[filter "up"]\n\tclean = tr a-z A-Z\n\trequired = true\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / ".gitattributes").write_text("*.txt filter=up\n")
    (root / "a.txt").write_text("hello\n")
    git(root, "add", ".gitattributes", "a.txt")
    git(root, "commit", "-q", "-m", "c1")
    later = os.stat(root / "a.txt").st_mtime + 10
    os.utime(root / "a.txt", (later, later))  # a changed stat makes git re-read the file through the filter
    return root


def test_a_user_level_filter_still_applies_so_the_tree_stays_clean(tmp_path, monkeypatch):
    root = _global_upcase_filter(tmp_path, monkeypatch)
    info, lim = gitinfo.git_info(root)
    assert lim is None and info["dirty"] is False


def test_a_repository_cannot_replace_a_user_level_filter(tmp_path, monkeypatch):
    import shlex

    root = _global_upcase_filter(tmp_path, monkeypatch)
    marker = tmp_path / "PWNED"
    git(root, "config", "filter.up.clean", "sh -c %s" % shlex.quote("touch %s; tr a-z A-Z" % shlex.quote(str(marker))))
    info, lim = gitinfo.git_info(root)
    assert not marker.exists()
    assert lim is None and info["dirty"] is False  # the user's own driver ran in its place


def test_a_repository_filter_marked_required_does_not_break_the_read(tmp_path):
    root = hostile_git_repo(tmp_path / "repo", tmp_path / "PWNED")
    git(root, "config", "filter.x.required", "true")
    info, lim = gitinfo.git_info(root)
    assert not (tmp_path / "PWNED").exists() and lim is None and info["dirty"] is True


def test_a_key_that_cannot_be_overridden_refuses_git(tmp_path):
    root = hostile_git_repo(tmp_path / "repo", tmp_path / "PWNED")
    git(root, "config", "filter.a=b.clean", "sh -c 'touch %s'" % (tmp_path / "PWNED"))
    assert gitinfo.git_info(root) == (None, "git_unavailable")
    assert gitinfo.project_files(root) is None
    assert not (tmp_path / "PWNED").exists()


def test_git_is_not_run_when_the_configuration_cannot_be_listed(repo, monkeypatch):
    real = gitinfo.subprocess.run
    seen = []

    def listing_fails(argv, *a, **k):
        seen.append(argv)
        if "config" in argv and "--show-scope" in argv:
            return subprocess.CompletedProcess(argv, 1, b"", b"")
        return real(argv, *a, **k)

    monkeypatch.setattr(gitinfo.subprocess, "run", listing_fails)
    assert gitinfo.git_info(repo) == (None, "git_unavailable")
    assert all("config" in argv for argv in seen)  # nothing but the listing ran


def test_every_git_call_carries_the_hardening_options(repo, monkeypatch):
    real = gitinfo.subprocess.run
    seen = []

    def spy(argv, *a, **k):
        seen.append(argv)
        return real(argv, *a, **k)

    monkeypatch.setattr(gitinfo.subprocess, "run", spy)
    gitinfo.git_info(repo)
    for option in ("core.fsmonitor=false", "core.hooksPath=" + os.devnull, "protocol.ext.allow=never",
                   "log.showSignature=false", "gpg.program=" + os.devnull):
        assert all(option in argv for argv in seen), option
    for argv in seen:
        if "status" in argv or "diff" in argv:
            assert "--ignore-submodules=all" in argv


def test_the_diff_is_hashed_as_a_stream_with_the_same_value(repo, monkeypatch):
    import hashlib

    payload = bytes(range(256)) * (1 << 15)  # 8 MiB
    (repo / "big.bin").write_bytes(payload)
    git(repo, "add", "big.bin")
    git(repo, "commit", "-q", "-m", "big")
    (repo / "big.bin").write_bytes(payload[::-1])
    captured = []
    real = gitinfo.subprocess.run

    def spy(argv, *a, **k):
        if "diff" in argv:
            captured.append(k.get("stdout"))
        return real(argv, *a, **k)

    monkeypatch.setattr(gitinfo.subprocess, "run", spy)
    info = gitinfo.git_info(repo)[0]
    assert captured and all(hasattr(out, "fileno") for out in captured)  # a file, not a pipe read into memory
    monkeypatch.setattr(gitinfo.subprocess, "run", real)
    diff = subprocess.run(["git", "-C", str(repo), "diff", "HEAD", "--no-ext-diff", "--no-textconv", "--binary", "--no-color",
                           "--ignore-submodules=all", "--", ".", ":(exclude).qa"], capture_output=True, check=True).stdout
    assert info["diff_hash"] == "sha256:" + hashlib.sha256(diff).hexdigest()


@pytest.mark.parametrize("how", HOSTILE_GIT[:3])
def test_qa_gate_evaluate_runs_no_repository_program(trusted, qa_cli, tmp_path, how):
    marker = tmp_path / "PWNED"
    hostile_git_repo(trusted.root, marker, how)
    code, _out = qa_cli("gate", "evaluate")
    assert code in (0, 1) and not marker.exists()


@pytest.mark.parametrize("how", HOSTILE_GIT[:3])
def test_qa_plan_create_runs_no_repository_program(trusted, qa_cli, tmp_path, how):
    marker = tmp_path / "PWNED"
    hostile_git_repo(trusted.root, marker, how)
    template = tmp_path / "template.json"
    template.write_text(json.dumps({"mode": "audit", "steps": [
        {"id": "S01", "skill": "qa-test-execution", "depends_on": [], "requires_authorization": ["test-execution-local"],
         "expected_outputs": [{"kind": "execution", "skill": "qa-test-execution"},
                              {"kind": "result_set", "skill": "qa-test-execution"}], "exclusive": False}]}))
    code, out = qa_cli("plan", "create", "--template", str(template))
    assert not marker.exists()
    assert code == 0, out


@pytest.mark.parametrize("key, expected", [
    ("lfs.extension.x.clean", True), ("lfs.extension.x.smudge", True), ("LFS.Extension.Dotted.Name.CLEAN", True),
    ("lfs.extension.x.priority", False), ("lfs.extension.clean", False), ("lfs.clean", False), ("lfs.url", False),
    ("lfs.customtransfer.agent.path", True), ("lfs.customtransfer.a.b.args", True), ("lfs.standalonetransferagent", True),
    ("lfs.customtransfer.agent.concurrent", False), ("lfs.customtransfer.path", False), ("lfs.standalonetransferagent.x", False),
    ("credential.helper", True), ("credential.https://example.invalid.helper", True), ("credential.username", False),
    ("core.sshCommand", True), ("core.askPass", True), ("core.gitProxy", True), ("core.editor", False), ("core.sshcommand.x", False),
    ("remote.origin.uploadpack", True), ("remote.a.b.receivepack", True), ("remote.origin.vcs", True), ("remote.origin.url", False),
    ("remote.uploadpack", False), ("lfs.https://example.invalid/x.standalonetransferagent", True),
    ("filter.x.clean", True), ("diff.x.textconv", True), ("core.editor", False),
])
def test_names_a_program(key, expected):
    assert gitinfo._names_a_program(key) is expected


def test_repository_lfs_extension_is_blanked_and_the_users_is_kept(repo, tmp_path, monkeypatch):
    user = tmp_path / "user.gitconfig"
    user.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(user))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    git(repo, "config", "lfs.extension.mine.clean", "evil")
    git(repo, "config", "lfs.extension.both.smudge", "evil")
    subprocess.run(["git", "config", "-f", str(user), "lfs.extension.both.smudge", "trusted"], check=True)
    git(repo, "config", "lfs.extension.mine.priority", "0")
    options = gitinfo._program_overrides("git", repo)
    assert options == ["-c", "lfs.extension.mine.clean=", "-c", "lfs.extension.both.smudge=trusted"]


def test_repository_transfer_agent_keys_are_blanked_and_the_users_is_kept(repo, tmp_path, monkeypatch):
    user = tmp_path / "user.gitconfig"
    user.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(user))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    git(repo, "config", "lfs.standalonetransferagent", "evil")
    git(repo, "config", "lfs.customtransfer.evil.path", "x")
    git(repo, "config", "lfs.customtransfer.evil.args", "y")
    subprocess.run(["git", "config", "-f", str(user), "lfs.customtransfer.evil.args", "mine"], check=True)
    assert gitinfo._program_overrides("git", repo) == ["-c", "lfs.standalonetransferagent=", "-c", "lfs.customtransfer.evil.path=",
                                                       "-c", "lfs.customtransfer.evil.args=mine"]


def test_repository_transport_keys_are_blanked_and_the_users_credential_helpers_all_stay(repo, tmp_path, monkeypatch):
    user = tmp_path / "user.gitconfig"
    user.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(user))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    git(repo, "config", "core.sshCommand", "x")
    git(repo, "config", "credential.https://example.invalid.helper", "!x")
    git(repo, "config", "remote.origin.vcs", "z")
    subprocess.run(["git", "config", "-f", str(user), "--add", "credential.helper", "one"], check=True)
    subprocess.run(["git", "config", "-f", str(user), "--add", "credential.helper", "two"], check=True)
    assert gitinfo._program_overrides("git", repo) == [
        "-c", "core.sshcommand=", "-c", "credential.https://example.invalid.helper=", "-c", "remote.origin.vcs=",
        "-c", "credential.helper=one", "-c", "credential.helper=two"]  # after the resets: every helper the user configured


@pytest.mark.parametrize("key", ["remote.origin.uploadpack", "remote.a.b.receivepack", "core.gitProxy"])
def test_a_repository_key_that_cannot_be_overridden_is_refused(repo, key):
    git(repo, "config", key, "x")
    with pytest.raises(gitinfo.Unavailable) as caught:
        gitinfo._program_overrides("git", repo)
    assert key.lower() in str(caught.value)
    assert gitinfo.git_info(repo) == (None, "git_unavailable")


def test_a_partial_clone_is_refused_only_where_lazy_fetch_cannot_be_switched_off(repo, monkeypatch):
    git(repo, "config", "extensions.partialClone", "origin")
    git(repo, "config", "remote.origin.promisor", "true")
    monkeypatch.setattr(gitinfo, "lazy_fetch_blocked", lambda: True)
    assert gitinfo._program_overrides("git", repo) == []
    monkeypatch.setattr(gitinfo, "lazy_fetch_blocked", lambda: False)
    with pytest.raises(gitinfo.Unavailable, match="partial"):
        gitinfo._program_overrides("git", repo)


@pytest.mark.parametrize("value, partial", [
    ("true", True), ("yes", True), ("on", True), ("1", True), ("2", True), ("-1", True), ("garbage", True), ("", True),
    (None, True),  # a key with no value at all, which git reads as true
    ("false", False), ("no", False), ("off", False), ("0", False), ("FALSE", False),
])
def test_a_promisor_remote_counts_unless_it_is_explicitly_false(repo, monkeypatch, value, partial):
    if value is None:
        with open(str(repo / ".git" / "config"), "a") as handle:
            handle.write('[remote "origin"]\n\tpromisor\n')
    else:
        git(repo, "config", "remote.origin.promisor", value)
    monkeypatch.setattr(gitinfo, "lazy_fetch_blocked", lambda: False)  # a git that cannot switch lazy fetch off
    if partial:
        with pytest.raises(gitinfo.Unavailable, match="partial"):
            gitinfo._program_overrides("git", repo)
    else:
        assert gitinfo._program_overrides("git", repo) == []


# --------------------------------------------------------------------------- K5: a repository git refuses is not "nothing tracked"

def _no_repository_above(path):
    here = path.resolve()
    return not any((parent / ".git").exists() for parent in (here, *here.parents))


def test_tracked_files_outside_any_repository_is_the_empty_set(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    if not _no_repository_above(plain):
        pytest.skip("the temporary directory lies inside a git repository")
    assert gitinfo.tracked_files(plain) == set()


def test_tracked_files_of_a_repository_git_refuses_is_unknown(tmp_path):
    root = tmp_path / "broken"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("1")
    git(root, "add", "a.txt")
    git(root, "commit", "-q", "-m", "c")
    assert gitinfo.tracked_files(root) == {"a.txt"}
    shutil.rmtree(str(root / ".git"))
    (root / ".git").write_text("gitdir: %s\n" % (tmp_path / "gone"))   # a repository is there, and git cannot read it
    assert gitinfo.tracked_files(root) is None
    (root / "sub").mkdir()
    assert gitinfo.tracked_files(root / "sub") is None   # a .git entry above the root counts too
