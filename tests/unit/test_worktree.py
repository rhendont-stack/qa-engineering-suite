from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from helpers import WRITE_JUNIT, req
from qa import config, execution, gitinfo, ledger, state, workspace, worktree
from qa.errors import QaError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

FAKE_LFS = """#!/bin/sh
# Stands for git-lfs: runs every lfs.extension.<name>.clean that git's configuration gives, then passes the content on.
for key in $(git config --name-only --get-regexp '^lfs\\.extension\\..*\\.clean$' | sort -u); do
  cmd=$(git config --get "$key")
  if [ -n "$cmd" ]; then sh -c "$cmd" </dev/null; fi
done
agent=$(git config --get lfs.standalonetransferagent)
if [ -n "$agent" ]; then
  program=$(git config --get "lfs.customtransfer.$agent.path")
  if [ -n "$program" ]; then sh -c "$program" </dev/null; fi
fi
cat
"""


def git(root, *args, check=True):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "commit.gpgsign=false", *args], check=check, capture_output=True, text=True)


def head(root):
    return git(root, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def repo(trusted):
    root = trusted.root
    git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("1")
    git(root, "add", "a.txt")
    git(root, "commit", "-q", "-m", "c1")
    return trusted


def commit_again(ws, text="2"):
    (ws.root / "a.txt").write_text(text)
    git(ws.root, "commit", "-qam", "next " + text)
    return head(ws.root)


def listed(ws):
    return git(ws.root, "worktree", "list", "--porcelain").stdout


@pytest.fixture
def user_level(tmp_path, monkeypatch):
    """A user-level git configuration (what ``~/.gitconfig`` is) where tests put what the user, not a repository, configured."""
    path = tmp_path / "user.gitconfig"
    path.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return path


def set_in(file, key, value):
    subprocess.run(["git", "config", "-f", str(file), key, value], check=True, capture_output=True)


def touching(marker):
    return "sh -c %s" % shlex.quote("touch %s" % shlex.quote(str(marker)))


@pytest.fixture
def lfs_repo(repo, tmp_path, user_level):
    """A repository whose every file is attributed to ``filter=lfs``, served by a fake git-lfs in the user's configuration;
    ``a.txt`` is modified so that reading the tree runs the clean filter, and the repository sets its own lfs extension."""
    root = repo.root
    (root / ".gitattributes").write_text("* filter=lfs\n")
    git(root, "add", ".gitattributes")
    git(root, "commit", "-q", "-m", "attributes")
    script = tmp_path / "fake-lfs.sh"
    script.write_text(FAKE_LFS)
    for name in ("clean", "smudge"):
        set_in(user_level, "filter.lfs." + name, "sh " + shlex.quote(str(script)))
    marker = tmp_path / "repository-extension-ran"
    set_in(root / ".git" / "config", "lfs.extension.x.clean", touching(marker))
    (root / "a.txt").write_text("changed")
    return SimpleNamespace(ws=repo, root=root, marker=marker)


def test_repository_lfs_extension_runs_nothing(lfs_repo, actor, tmp_path):
    root, marker = lfs_repo.root, lfs_repo.marker
    git(root, "diff", "HEAD")  # the control: unprotected, the fake git-lfs does run the repository's extension
    assert marker.exists()
    marker.unlink()
    gitinfo.git_info(root)
    assert not marker.exists()
    control = tmp_path / "control"
    git(root, "worktree", "add", "-q", "--detach", str(control), head(root))
    assert marker.exists()  # the control for the add side: unprotected, checking files out runs the extension too
    marker.unlink()
    worktree.add(lfs_repo.ws, actor, head(root))
    assert not marker.exists()


def test_repository_transfer_agent_runs_nothing(lfs_repo, actor, tmp_path):
    root = lfs_repo.root
    marker = tmp_path / "agent-ran"
    set_in(root / ".git" / "config", "lfs.standalonetransferagent", "t")
    set_in(root / ".git" / "config", "lfs.customtransfer.t.path", touching(marker))
    set_in(root / ".git" / "config", "lfs.customtransfer.t.args", "--x")
    git(root, "worktree", "add", "-q", "--detach", str(tmp_path / "control"), head(root))
    assert marker.exists()  # the control: the stand-in downloads through the agent unless it is neutralised
    marker.unlink()
    gitinfo.git_info(root)
    worktree.add(lfs_repo.ws, actor, head(root))
    assert not marker.exists()


def _marker_command(marker):
    return "sh -c %s" % shlex.quote("touch %s; exit 1" % shlex.quote(str(marker)))


def _helper_command(marker):
    return "!touch %s" % shlex.quote(str(marker))


def _askpass_script(marker):
    script = marker.parent / (marker.name + ".sh")
    script.write_text("#!/bin/sh\ntouch %s\necho x\n" % shlex.quote(str(marker)))
    script.chmod(0o755)
    return str(script)


CREDENTIAL_INPUT = b"protocol=https\nhost=example.invalid\n\n"
# Keys git resolves with the last value, so a command-scope blank replaces the repository's. Each runs through real git:
# name -> (key, value that runs the marker, git arguments that make git start the program, stdin).
OVERRIDABLE = {
    "ssh command": ("core.sshcommand", _marker_command, ["ls-remote", "ssh://127.0.0.1:1/x"], None),
    "remote helper": ("remote.origin.vcs", lambda marker: "qaprobe", ["ls-remote", "origin"], None),
    "askpass": ("core.askpass", _askpass_script, ["credential", "fill"], CREDENTIAL_INPUT),
    "credential helper": ("credential.helper", _helper_command, ["credential", "fill"], CREDENTIAL_INPUT),
    "url credential helper": ("credential.https://example.invalid.helper", _helper_command, ["credential", "fill"], CREDENTIAL_INPUT),
}


@pytest.fixture
def transport(repo, tmp_path, user_level, monkeypatch):
    """The workspace repository with a remote ``origin`` and a ``git-remote-qaprobe`` that touches a marker when git runs it."""
    root = repo.root
    git(root, "remote", "add", "origin", "file://" + str(tmp_path / "nowhere"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    helper = bindir / "git-remote-qaprobe"
    helper.write_text("#!/bin/sh\ntouch %s\nexit 1\n" % shlex.quote(str(tmp_path / "ran-remote helper")))
    helper.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    return SimpleNamespace(ws=repo, root=root)


def _plain(root, args, stdin):
    return subprocess.run(["git", "-C", str(root), *args], input=stdin, capture_output=True, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))


@pytest.mark.parametrize("name", sorted(OVERRIDABLE))
def test_repository_transport_program_runs_nothing(transport, tmp_path, name):
    key, value, args, stdin = OVERRIDABLE[name]
    marker = tmp_path / ("ran-" + name)
    set_in(transport.root / ".git" / "config", key, value(marker))
    _plain(transport.root, args, stdin)
    assert marker.exists()  # the control: unprotected, git starts the program the repository names
    marker.unlink()
    gitinfo.session(transport.root).run(args, input=stdin)
    assert not marker.exists()


@pytest.mark.parametrize("name", sorted(OVERRIDABLE))
def test_user_level_transport_program_still_applies(transport, user_level, tmp_path, name):
    key, value, args, stdin = OVERRIDABLE[name]
    marker = tmp_path / ("ran-" + name)
    set_in(user_level, key, value(marker))
    gitinfo.session(transport.root).run(args, input=stdin)
    assert marker.exists()


def test_a_repository_credential_helper_does_not_drop_the_users(transport, user_level, tmp_path):
    kept = tmp_path / "ran-generic-user-helper"
    set_in(user_level, "credential.helper", _helper_command(kept))
    set_in(transport.root / ".git" / "config", "credential.https://example.invalid.helper", _helper_command(tmp_path / "ran-repo"))
    gitinfo.session(transport.root).run(["credential", "fill"], input=CREDENTIAL_INPUT)
    assert kept.exists() and not (tmp_path / "ran-repo").exists()


# Keys git resolves with the FIRST value it reads, and the repository's scope comes before the command line's, so no
# command-scope blank can replace a repository's value. The kernel refuses to run git while the repository sets one.
FIRST_VALUE = {
    "upload-pack": ("remote.origin.uploadpack", ["ls-remote", "origin"]),
    "receive-pack": ("remote.origin.receivepack", ["push", "origin", "HEAD:refs/heads/z"]),
    "proxy": ("core.gitproxy", ["ls-remote", "git://127.0.0.1:1/x"]),
}


@pytest.mark.parametrize("name", sorted(FIRST_VALUE))
def test_a_repository_key_git_cannot_override_makes_the_kernel_refuse(transport, actor, tmp_path, name):
    key, args = FIRST_VALUE[name]
    marker = tmp_path / ("ran-" + name)
    script = tmp_path / (name + ".sh")
    script.write_text("#!/bin/sh\ntouch %s\nexit 1\n" % shlex.quote(str(marker)))
    script.chmod(0o755)
    set_in(transport.root / ".git" / "config", key, "sh " + str(script) if name != "proxy" else str(script))
    _plain(transport.root, ["-c", key + "="] + args, None)
    assert marker.exists()  # the control: even with a command-scope blank, git still starts the repository's program
    marker.unlink()
    with pytest.raises(gitinfo.Unavailable):
        gitinfo.session(transport.root)
    assert gitinfo.git_info(transport.root) == (None, "git_unavailable")
    with pytest.raises(QaError) as caught:
        worktree.add(transport.ws, actor, head(transport.root))
    assert caught.value.code == "git_config_refused" and key in caught.value.details["keys"]
    assert not marker.exists() and not any(e["type"].startswith("worktree.") for e in ledger.iter_all_events(transport.ws))
    assert os.listdir(str(transport.ws.path("work"))) == []


def test_the_same_keys_set_by_the_user_do_not_refuse(transport, user_level, tmp_path):
    set_in(user_level, "remote.origin.uploadpack", "git-upload-pack")
    set_in(user_level, "core.gitproxy", "none")
    assert gitinfo.session(transport.root) is not None


def test_user_level_transfer_agent_still_applies(lfs_repo, user_level, tmp_path):
    marker = tmp_path / "user-agent-ran"
    set_in(user_level, "lfs.standalonetransferagent", "u")
    set_in(user_level, "lfs.customtransfer.u.path", touching(marker))
    gitinfo.git_info(lfs_repo.root)
    assert marker.exists()


def test_user_level_lfs_extension_still_applies(lfs_repo, user_level, tmp_path):
    marker = tmp_path / "user-extension-ran"
    set_in(user_level, "lfs.extension.y.clean", touching(marker))
    gitinfo.git_info(lfs_repo.root)
    assert marker.exists()


def test_a_repository_extension_does_not_shadow_the_users(lfs_repo, user_level, tmp_path):
    set_in(user_level, "lfs.extension.x.clean", touching(tmp_path / "user-x"))
    gitinfo.git_info(lfs_repo.root)
    assert (tmp_path / "user-x").exists() and not lfs_repo.marker.exists()  # the user's own value for the key wins


def test_worktree_add_leaves_the_working_tree_untouched(repo, actor):
    (repo.root / "dirty.txt").write_text("untracked")
    before = git(repo.root, "status", "--porcelain").stdout
    index = repo.root / ".git" / "index"
    stamp = index.stat().st_mtime_ns
    worktree.add(repo, actor, head(repo.root))
    assert index.stat().st_mtime_ns == stamp  # before anything else reads the tree: a plain `git status` may refresh it
    assert git(repo.root, "status", "--porcelain").stdout == before
    assert (repo.root / "dirty.txt").read_text() == "untracked" and head(repo.root) == git(repo.root, "rev-parse", "main").stdout.strip()


def test_worktree_lives_under_qa_work_and_is_recorded(repo, actor):
    first = head(repo.root)
    second = commit_again(repo)
    done = worktree.add(repo, actor, first)
    assert set(done) == {"path", "commit", "event"}
    assert done["commit"] == first and done["path"] == ".qa/work/wt-" + first[:12]
    folder = repo.root / done["path"]
    assert (folder / "a.txt").read_text() == "1" and head(folder) == first and second != first
    st = state.load(repo)
    assert st["worktrees"][done["path"]]["commit"] == first and st["worktrees"][done["path"]]["removed"] is None
    event = next(e for e in ledger.iter_all_events(repo) if e["type"] == "worktree.created")
    assert event["id"] == done["event"] and event["data"] == {"path": done["path"], "commit": first, "activity": "static-analysis"}
    assert re.fullmatch(r"[0-9a-f]{40}", event["data"]["commit"])


def test_abbreviated_commit_is_recorded_in_full(repo, actor):
    full = head(repo.root)
    assert worktree.add(repo, actor, full[:10])["commit"] == full


def test_remove_closes_the_worktree(repo, actor):
    done = worktree.add(repo, actor, head(repo.root))
    removed = worktree.remove(repo, actor, done["path"])
    assert removed["path"] == done["path"] and not (repo.root / done["path"]).exists()
    assert done["path"] not in listed(repo) and state.load(repo)["worktrees"][done["path"]]["removed"] == removed["event"]
    with pytest.raises(QaError, match="worktree_not_ours"):
        worktree.remove(repo, actor, done["path"])  # already closed
    assert worktree.add(repo, actor, done["commit"])["path"] == done["path"]  # a closed path can be made again


def test_remove_refuses_a_worktree_the_suite_did_not_create(repo, actor, tmp_path):
    other = tmp_path / "elsewhere"
    git(repo.root, "worktree", "add", "-q", "--detach", str(other), head(repo.root))
    for target in (".qa/work/wt-elsewhere", str(other)):
        with pytest.raises(QaError, match="worktree_not_ours"):
            worktree.remove(repo, actor, target)
    assert other.is_dir() and not any(e["type"] == "worktree.removed" for e in ledger.iter_all_events(repo))


def test_adding_the_same_commit_twice_is_refused(repo, actor):
    worktree.add(repo, actor, head(repo.root))
    with pytest.raises(QaError, match="worktree_exists"):
        worktree.add(repo, actor, head(repo.root))


@pytest.fixture
def hostile(repo, tmp_path):
    """The repository's own configuration names a smudge filter, a clean filter and a post-checkout hook that each touch a marker."""
    root = repo.root
    (root / ".gitattributes").write_text("* filter=x\n")
    git(root, "add", ".gitattributes")
    git(root, "commit", "-q", "-m", "attributes")
    markers = {name: tmp_path / ("ran-" + name) for name in ("smudge", "clean", "hook")}
    set_in(root / ".git" / "config", "filter.x.smudge", "sh -c %s" % shlex.quote("touch %s; cat" % markers["smudge"]))
    set_in(root / ".git" / "config", "filter.x.clean", "sh -c %s" % shlex.quote("touch %s; cat" % markers["clean"]))
    hook = root / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\ntouch %s\n" % shlex.quote(str(markers["hook"])))
    hook.chmod(0o755)
    return SimpleNamespace(ws=repo, root=root, markers=markers)


def test_worktree_add_runs_no_repository_hook_or_filter(hostile, actor, tmp_path):
    control = tmp_path / "control"
    git(hostile.root, "worktree", "add", "-q", "--detach", str(control), head(hostile.root))
    assert all(marker.exists() for name, marker in hostile.markers.items() if name != "clean")  # unprotected git runs them
    for marker in hostile.markers.values():
        marker.exists() and marker.unlink()
    done = worktree.add(hostile.ws, actor, head(hostile.root))
    assert (hostile.root / done["path"] / "a.txt").read_text() == "1"
    assert not any(marker.exists() for marker in hostile.markers.values())
    worktree.remove(hostile.ws, actor, done["path"])
    assert not any(marker.exists() for marker in hostile.markers.values())


def test_unknown_commit_is_refused_and_nothing_recorded(repo, actor):
    before = listed(repo)
    for bad in ("f" * 40, "no-such-branch", "--help", "-x", ""):
        with pytest.raises(QaError) as caught:
            worktree.add(repo, actor, bad)
        assert caught.value.code == "unknown_commit"
    assert listed(repo) == before and not any(e["type"].startswith("worktree.") for e in ledger.iter_all_events(repo))
    assert os.listdir(str(repo.path("work"))) == []


def test_worktree_add_is_refused_under_drift(repo, actor):
    repo.path("config.json").write_bytes(repo.path("config.json").read_bytes() + b" ")
    with pytest.raises(QaError) as caught:
        worktree.add(repo, actor, head(repo.root))
    assert caught.value.code == "config_drift" and "worktree.created" not in {e["type"] for e in ledger.iter_all_events(repo)}
    assert os.listdir(str(repo.path("work"))) == []


def test_run_in_a_worktree_records_its_commit(repo, actor, py):
    first = head(repo.root)
    second = commit_again(repo)
    done = worktree.add(repo, actor, first)
    inside = repo.root / done["path"]
    (inside / "scratch.txt").write_text("untracked in the worktree")
    ran = execution.execute(repo, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass", cwd=inside))
    recorded = state.load(repo)["executions"][ran["execution"]]["git"]
    assert recorded["commit"] == first != second and recorded["worktree"] == done["path"] and recorded["dirty"] is True
    plain = execution.execute(repo, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))
    main = state.load(repo)["executions"][plain["execution"]]["git"]
    assert main["commit"] == second and "worktree" not in main  # the main tree's own record is unchanged
    assert main["diff_hash"] != recorded["diff_hash"]
    deeper = inside / "sub"
    deeper.mkdir()
    again = execution.execute(repo, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass", cwd=deeper))
    assert state.load(repo)["executions"][again["execution"]]["git"]["worktree"] == done["path"]
    worktree.remove(repo, actor, done["path"])
    after = execution.execute(repo, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))
    assert "worktree" not in state.load(repo)["executions"][after["execution"]]["git"]


def test_worktree_with_a_tracked_config_is_not_a_workspace(repo, actor):
    git(repo.root, "add", "-f", ".qa/config.json")
    git(repo.root, "commit", "-q", "-m", "track the config")
    done = worktree.add(repo, actor, head(repo.root))
    inside = repo.root / done["path"]
    assert (inside / ".qa" / "config.json").is_file()  # the worktree carries a config of its own
    assert workspace.find_workspace(inside).root == repo.root
    assert workspace.find_workspace(inside / ".qa").root == repo.root
    assert workspace.find_workspace(repo.root).root == repo.root


@pytest.mark.parametrize("depth", ["", "pkg", "pkg/deep"])
def test_a_tracked_config_below_a_worktree_is_not_a_workspace(repo, actor, depth):
    nested = repo.root / "pkg"
    (nested / ".qa").mkdir(parents=True)
    (nested / "deep").mkdir()
    (nested / ".qa" / "config.json").write_text("{}")
    git(repo.root, "add", "-f", "pkg/.qa/config.json")
    git(repo.root, "commit", "-q", "-m", "a sub-project with its own config")
    done = worktree.add(repo, actor, head(repo.root))
    inside = repo.root / done["path"] / depth
    assert (repo.root / done["path"] / "pkg" / ".qa" / "config.json").is_file()
    assert workspace.find_workspace(inside).root == repo.root


def test_a_container_run_inside_a_worktree_is_refused(repo, actor, fake_engine):
    done = worktree.add(repo, actor, head(repo.root))
    before = len(state.load(repo)["executions"])
    with pytest.raises(QaError) as caught:
        execution.execute(repo, actor, execution.RunRequest(command=["true"], container="python@sha256:" + "a" * 64,
                                                            cwd=repo.root / done["path"]))
    assert caught.value.code == "container_in_worktree"
    assert len(state.load(repo)["executions"]) == before and fake_engine.calls == []


def make_partial_clone(root, origin, tmp_path, tag, program=True):
    """``root`` (an existing repository) as a partial clone of ``origin``: its blobs are missing, so a checkout must fetch them.
    With ``program`` its own configuration also names a program for the fetch. Returns (marker, commit)."""
    marker = tmp_path / ("fetch-program-ran-" + tag)
    script = tmp_path / ("upload-pack-%s.sh" % tag)
    script.write_text("#!/bin/sh\ntouch %s\nexec git upload-pack \"$@\"\n" % shlex.quote(str(marker)))
    git(root, "remote", "add", "origin", "file://" + str(origin))
    git(root, "config", "remote.origin.promisor", "true")
    git(root, "config", "remote.origin.partialclonefilter", "blob:none")
    git(root, "config", "extensions.partialClone", "origin")
    if program:
        git(root, "config", "remote.origin.uploadpack", "sh " + str(script))
    git(root, "fetch", "-q", "--filter=blob:none", "origin", "main")
    if marker.exists():
        marker.unlink()
    return marker, git(root, "rev-parse", "FETCH_HEAD").stdout.strip()


@pytest.fixture
def origin(tmp_path):
    path = tmp_path / "origin"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "big.txt").write_text("blob that is not local")
    git(path, "add", "big.txt")
    git(path, "commit", "-q", "-m", "o1")
    git(path, "config", "uploadpack.allowFilter", "true")
    git(path, "config", "uploadpack.allowAnySHA1InWant", "true")
    return path


@pytest.fixture
def old_git(monkeypatch):
    """A git that cannot switch lazy fetch off: the kernel's environment variable does nothing and the probe says so."""
    def environment():
        env = dict(os.environ)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    monkeypatch.setattr(gitinfo, "_env", environment)
    monkeypatch.setattr(gitinfo, "lazy_fetch_blocked", lambda: False)


def test_partial_clone_with_a_repository_fetch_program_is_refused(repo, actor, origin, tmp_path):
    control = tmp_path / "control-repo"  # its own partial clone: the control's fetch must not fill the workspace's object store
    control.mkdir()
    git(control, "init", "-q", "-b", "main")
    control_marker, control_commit = make_partial_clone(control, origin, tmp_path, "control")
    git(control, "worktree", "add", "-q", "--detach", str(tmp_path / "control-tree"), control_commit)
    assert control_marker.exists()  # the control: unprotected, the checkout fetches through the repository's own program
    marker, commit = make_partial_clone(repo.root, origin, tmp_path, "ws")
    with pytest.raises(QaError) as caught:
        worktree.add(repo, actor, commit)
    assert caught.value.code == "git_config_refused" and "remote.origin.uploadpack" in caught.value.details["keys"]
    assert not marker.exists() and gitinfo.git_info(repo.root) == (None, "git_unavailable")


def test_lazy_fetch_is_off_for_a_worktree_checkout_in_a_partial_clone(repo, actor, origin, tmp_path):
    if not gitinfo.lazy_fetch_blocked():
        pytest.skip("this git cannot switch lazy fetch off; test_old_git_is_refused_in_a_partial_clone covers it")
    _marker, commit = make_partial_clone(repo.root, origin, tmp_path, "ws", program=False)
    with pytest.raises(QaError) as caught:
        worktree.add(repo, actor, commit)
    assert caught.value.code == "worktree_failed"  # the missing blob was not fetched; without the switch the checkout succeeds
    assert gitinfo.git_info(repo.root)[1] != "git_unavailable"


def test_old_git_is_refused_in_a_partial_clone(repo, actor, origin, tmp_path, old_git):
    _marker, commit = make_partial_clone(repo.root, origin, tmp_path, "ws", program=False)
    with pytest.raises(QaError) as caught:
        worktree.add(repo, actor, commit)
    assert caught.value.code == "git_config_refused" and "partial" in caught.value.message
    assert gitinfo.git_info(repo.root) == (None, "git_unavailable")
    assert os.listdir(str(repo.path("work"))) == []


def test_old_git_still_works_outside_a_partial_clone(repo, actor, old_git):
    assert gitinfo.git_info(repo.root)[1] is None
    assert worktree.add(repo, actor, head(repo.root))["commit"] == head(repo.root)


def test_git_calls_never_prompt_and_lazy_fetch_support_is_detected():
    env = gitinfo._env()
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_NO_LAZY_FETCH"] == "1"
    probed = subprocess.run(["git", "--no-lazy-fetch", "version"], capture_output=True).returncode == 0
    assert gitinfo.lazy_fetch_blocked() is probed


def _case_insensitive(path):
    probe = path / "CaseProbe"
    probe.mkdir()
    return (path / "caseprobe").exists()


def test_case_variant_start_path_inside_a_worktree_is_not_a_workspace(repo, actor, tmp_path):
    if not _case_insensitive(tmp_path):
        pytest.skip("the filesystem is case sensitive, so a differently spelled path names another directory")
    nested = repo.root / "pkg"
    (nested / ".qa").mkdir(parents=True)
    (nested / ".qa" / "config.json").write_text("{}")
    git(repo.root, "add", "-f", "pkg/.qa/config.json")
    git(repo.root, "commit", "-q", "-m", "a sub-project with its own config")
    done = worktree.add(repo, actor, head(repo.root))
    spelled = repo.root / ".QA" / "WORK" / done["path"].split("/")[-1] / "pkg"
    assert spelled.is_dir()
    assert workspace.find_workspace(spelled).root == repo.root


def test_case_variant_cwd_inside_a_worktree_is_attributed_to_it(repo, actor, tmp_path):
    if not _case_insensitive(tmp_path):
        pytest.skip("the filesystem is case sensitive, so a differently spelled path names another directory")
    done = worktree.add(repo, actor, head(repo.root))
    spelled = repo.root / ".QA" / "Work" / done["path"].split("/")[-1]
    assert worktree.containing(repo, state.load(repo), str(spelled)) == done["path"]


def test_the_untrusted_hint_in_a_worktree_does_not_suggest_a_container(ws, actor, py):
    git(ws.root, "init", "-q", "-b", "main")
    (ws.root / "a.txt").write_text("1")
    git(ws.root, "add", "a.txt")
    git(ws.root, "commit", "-q", "-m", "c1")
    config.record_initialized(ws, actor)  # the project stays untrusted
    done = worktree.add(ws, actor, head(ws.root))
    ran = execution.execute(ws, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass", cwd=ws.root / done["path"]))
    record = state.load(ws)["executions"][ran["execution"]]
    assert record["status"] == "blocked" and record["git"]["worktree"] == done["path"]
    text = " ".join(record["limitations"])
    assert "not_authorized" in text and "project.trust" in text and "--container" not in text
    plain = execution.execute(ws, actor, req(py, WRITE_JUNIT, "{evidence}/junit.xml", "pass"))
    assert "--container" in " ".join(state.load(ws)["executions"][plain["execution"]]["limitations"])  # elsewhere it still does


def test_cli_adds_and_removes(repo, qa_cli):
    code, body = qa_cli("worktree", "add", head(repo.root))
    assert code == 0 and body["ok"] and re.fullmatch(r"\.qa/work/wt-[0-9a-f]{12}", body["path"])
    assert (repo.root / body["path"]).is_dir()
    code, body = qa_cli("worktree", "remove", body["path"])
    assert code == 0 and body["ok"] and not any(p.name.startswith("wt-") for p in repo.path("work").iterdir())
    code, body = qa_cli("worktree", "remove", ".qa/work/wt-elsewhere")
    assert code != 0 and body["error"]["code"] == "worktree_not_ours"


def test_cli_worktree_commands_are_write_commands():
    from qa import cli

    assert {"worktree add", "worktree remove"} <= set(cli.WRITE_COMMANDS)
