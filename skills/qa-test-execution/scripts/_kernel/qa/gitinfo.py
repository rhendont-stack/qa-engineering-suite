"""Read-only facts about the user's git tree (commit, branch, dirty flag, diff hash).

Only read-only git subcommands run here (worktree.py reuses the same neutralised session to add and remove the suite's
own worktrees), and the repository's own configuration is data: nothing it names may run. Every call
disables fsmonitor, the untracked cache, hooks, signature checks and the ``ext::`` transport, and sets GIT_OPTIONAL_LOCKS=0,
so inspecting a tree neither runs a program nor rewrites its index. Before each read the configuration is listed with its
scope; every ``filter.<driver>.clean|smudge|process``, ``diff.<driver>.command|textconv`` and
``lfs.extension.<name>.clean|smudge``, ``lfs.customtransfer.<name>.path|args`` and
``lfs.standalonetransferagent`` that the repository sets (its ``.git/config``, a file that includes, or ``config.worktree``) is replaced by the user's own value for that key, or blanked
when the user has none. A user-level driver such as git-lfs therefore still applies. Submodules are never entered, since
each has a configuration of its own. When the listing fails, or a key cannot be overridden, git does not run.

Limitations returned instead of facts: not_a_git_repo, no_commit, git_unavailable.
"""
from __future__ import annotations

import functools
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .canonical import sha256_bytes, sha256_file

_TIMEOUT_S = 30
_EXCLUDE_QA = ":(exclude).qa"
_BASE_OPTS = ["-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-c", "core.hooksPath=" + os.devnull,
              "-c", "protocol.ext.allow=never", "-c", "log.showSignature=false", "-c", "gpg.program=" + os.devnull,
              "-c", "gpg.openpgp.program=" + os.devnull, "-c", "gpg.ssh.program=" + os.devnull,
              "-c", "gpg.x509.program=" + os.devnull]
_QA_PATHSPEC = ":(icase).qa"  # a case-insensitive filesystem reads a tracked .QA/ as this .qa/
_SHAREABLE = (".qa/.gitignore", ".qa/config.json")  # .qa/.gitignore keeps everything else out of git
_PROGRAM_KEYS = {"filter": ("clean", "smudge", "process"), "diff": ("command", "textconv")}
_LFS_SHAPES = {"extension": ("clean", "smudge"), "customtransfer": ("path", "args")}  # lfs.<kind>.<name>.<key>
_LFS_SELECTORS = ("standalonetransferagent",)  # lfs.<key>: picks a transfer agent by name (the program is lfs.customtransfer.*)
# Keys that name a transport or authentication program, which a fetch (a lazy one included) would start.
_CORE_PROGRAMS = ("sshcommand", "askpass", "gitproxy")  # core.<key>
_REMOTE_PROGRAMS = ("uploadpack", "receivepack", "vcs")  # remote.<name>.<key>
# These three are resolved with the first value git reads, and a repository's scope comes before the command line's, so a
# command-scope blank cannot replace a repository value: while the repository sets one, git does not run.
_FIRST_VALUE_REMOTE = ("uploadpack", "receivepack")  # remote.<name>.<key>
_MULTI_VALUED = "credential"  # credential.helper lists helpers; an empty value resets the list, so it is reset and then refilled
_TRUSTED_SCOPES = ("system", "global", "command")  # the user's own configuration and environment, not the repository's
_CHUNK = 1 << 20


class _Unavailable(Exception):
    """git is missing, cannot start, timed out, or its configuration could not be listed or neutralised."""


class UnsafeConfiguration(_Unavailable):
    """The repository's configuration cannot be neutralised, so git does not run. ``keys`` names the settings (never values)."""

    def __init__(self, reason: str, keys: Tuple[str, ...] = ()) -> None:
        super().__init__("%s: %s" % (reason, ", ".join(keys)) if keys else reason)
        self.reason, self.keys = reason, keys


def _env() -> Dict[str, str]:
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"  # a credential or passphrase is never asked for on a terminal
    env["GIT_NO_LAZY_FETCH"] = "1"  # git that has it never fetches a missing object of a partial clone; older git ignores it
    return env


@functools.lru_cache(maxsize=None)
def _lazy_fetch_blocked(git: str) -> bool:
    try:
        done = subprocess.run([git, "--no-lazy-fetch", "version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=_TIMEOUT_S, env=_env())
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def lazy_fetch_blocked() -> bool:
    """Whether the installed git supports switching lazy fetch off (it does from git 2.44), so that the environment this
    module sets really stops it. Detected by running git, not by comparing version numbers."""
    git = shutil.which("git")
    return git is not None and _lazy_fetch_blocked(git)


def _spawn(git: str, root: Path, opts: List[str], args: List[str], stdout=subprocess.PIPE,
           timeout_s: int = _TIMEOUT_S, stdin: Optional[bytes] = None) -> "subprocess.CompletedProcess[bytes]":
    try:
        return subprocess.run([git, "-C", str(root), *_BASE_OPTS, *opts, *args], stdout=stdout, stderr=subprocess.PIPE,
                              timeout=timeout_s, env=_env(), input=stdin)
    except (OSError, subprocess.SubprocessError) as exc:
        raise _Unavailable() from exc


def _names_a_program(key: str) -> bool:
    parts = key.lower().split(".")
    if parts[0] == "lfs":  # only the lfs keys that name a program to run, or select the one that will; lfs.url and the like do not
        if len(parts) >= 2 and parts[-1] in _LFS_SELECTORS:
            return True
        return len(parts) >= 4 and _LFS_SHAPES.get(parts[1]) is not None and parts[-1] in _LFS_SHAPES[parts[1]]
    if parts[0] == "credential":  # credential.helper and credential.<url>.helper
        return len(parts) >= 2 and parts[-1] == "helper"
    if parts[0] == "core":
        return len(parts) == 2 and parts[1] in _CORE_PROGRAMS
    if parts[0] == "remote":
        return len(parts) >= 3 and parts[-1] in _REMOTE_PROGRAMS
    return len(parts) >= 3 and parts[-1] in _PROGRAM_KEYS.get(parts[0], ())


def _first_value_key(key: str) -> bool:
    parts = key.lower().split(".")
    return (parts[0] == "core" and parts[1:] == ["gitproxy"]) or (
        parts[0] == "remote" and len(parts) >= 3 and parts[-1] in _FIRST_VALUE_REMOTE)


def _explicitly_false(value: bytes) -> bool:
    """Git's false: ``false``, ``no``, ``off`` or an integer that is zero. Anything else, including a value that does not parse
    and an empty one, is treated as true here, since the question is whether a fetch could happen."""
    text = value.strip().lower()
    if text in (b"false", b"no", b"off"):
        return True
    try:
        return int(text, 0) == 0
    except ValueError:
        return False


def _partial_clone_setting(key: str, value: bytes) -> bool:
    """Whether this entry makes the repository a partial clone: ``extensions.partialClone``, or a ``remote.<name>.promisor``
    that is not explicitly false (a key with no value is true to git, so it counts)."""
    parts = key.lower().split(".")
    if parts == ["extensions", "partialclone"]:
        return True
    return parts[0] == "remote" and parts[-1] == "promisor" and len(parts) >= 3 and not _explicitly_false(value)


def _program_overrides(git: str, root: Path) -> List[str]:
    """``-c`` options that put the user's own value (or nothing) in place of every program-naming key the repository sets.

    Raises ``_Unavailable`` when the configuration cannot be listed or a key cannot be written as ``-c key=value``, and
    ``UnsafeConfiguration`` when the repository sets a key a ``-c`` option cannot override, or is a partial clone and this git
    cannot switch lazy fetch off (a missing object would then be fetched through the repository's own remote)."""
    listed = _spawn(git, root, [], ["config", "--list", "--null", "--show-scope"])
    if listed.returncode != 0:
        raise _Unavailable()
    items = listed.stdout.split(b"\0")  # "<scope>" NUL "<key>" LF "<value>" NUL, for each entry
    if items and items[-1] == b"":
        items.pop()
    if len(items) % 2:
        raise _Unavailable()
    trusted: Dict[str, List[str]] = {}
    repository: List[str] = []
    refused: List[str] = []
    partial = False
    for scope_raw, entry in zip(items[0::2], items[1::2]):
        key_raw, _, value_raw = entry.partition(b"\n")
        key = key_raw.decode("utf-8", "surrogateescape")
        partial = partial or _partial_clone_setting(key, value_raw)
        if not _names_a_program(key):
            continue
        if scope_raw.decode("ascii", "replace") in _TRUSTED_SCOPES:
            trusted.setdefault(key, []).append(value_raw.decode("utf-8", "surrogateescape"))
        elif _first_value_key(key):
            refused.append(key)
        else:
            repository.append(key)
    if refused:
        raise UnsafeConfiguration("the repository sets a transport program that cannot be neutralised", tuple(dict.fromkeys(refused)))
    if partial and not lazy_fetch_blocked():
        raise UnsafeConfiguration("a partial clone, and this git cannot switch off the fetch of missing objects")
    opts: List[str] = []
    drivers = set()
    credentials = False
    for key in dict.fromkeys(repository):
        if "=" in key:
            raise _Unavailable()  # "-c" would split the key there
        if key.split(".")[0].lower() == _MULTI_VALUED:
            opts += ["-c", key + "="]  # a reset drops the user's helpers of other keys too, so they all come back below
            credentials = True
        else:
            opts += ["-c", "%s=%s" % (key, (trusted.get(key) or [""])[-1])]
        if key.split(".")[0].lower() == "filter":
            drivers.add(key.rsplit(".", 1)[0])
    if credentials:
        for name in (name for name in trusted if name.split(".")[0].lower() == _MULTI_VALUED):
            for value in trusted[name]:
                opts += ["-c", "%s=%s" % (name, value)]
    for driver in sorted(drivers):
        if not any((trusted.get("%s.%s" % (driver, name)) or [""])[-1] for name in _PROGRAM_KEYS["filter"]):
            opts += ["-c", driver + ".required=false"]  # a blanked driver must not turn into a failed read
    return opts


class _Git:
    """git for one tree, with the tree's own program settings neutralised once and applied to every call."""

    def __init__(self, git: str, root: Path) -> None:
        self.git, self.root = git, root
        self.opts = _program_overrides(git, root)

    def run(self, args: List[str], stdout=subprocess.PIPE, timeout_s: int = _TIMEOUT_S,
            input: Optional[bytes] = None) -> "subprocess.CompletedProcess[bytes]":
        return _spawn(self.git, self.root, self.opts, args, stdout=stdout, timeout_s=timeout_s, stdin=input)


Unavailable = _Unavailable  # for the callers that change the tree (worktree.py) and so need the same neutralised session


def session(root: Path) -> Optional[_Git]:
    """A neutralised git session on ``root``; None when git is missing. Raises ``Unavailable`` when the configuration cannot be
    listed or a key cannot be overridden, so that git then does not run."""
    git = shutil.which("git")
    return None if git is None else _Git(git, Path(root))


def _run(git: str, root: Path, args: List[str]) -> "subprocess.CompletedProcess[bytes]":
    """One read-only git call on ``root`` with every protection; raises ``_Unavailable``."""
    return _Git(git, root).run(args)


def _untracked_hash(root: Path, rel: bytes) -> str:
    """Hash one untracked path without following links or opening special files."""
    path = root / os.fsdecode(rel)
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return sha256_bytes(b"missing")
    if stat.S_ISLNK(mode):
        try:
            return sha256_bytes(b"symlink:" + os.fsencode(os.readlink(path)))
        except OSError:
            return sha256_bytes(b"missing")
    if stat.S_ISREG(mode):
        try:
            return sha256_file(path)
        except OSError:
            return sha256_bytes(b"missing")
    return sha256_bytes(b"special:" + str(stat.S_IFMT(mode)).encode("ascii"))


def project_files(root: Path) -> Optional[List[str]]:
    """Every path ``git ls-files -co --exclude-standard`` lists under ``root``; ``None`` when git cannot say."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        listed = _run(git, root, ["ls-files", "-co", "--exclude-standard", "-z"])
    except _Unavailable:
        return None
    if listed.returncode != 0:
        return None
    return [os.fsdecode(path) for path in listed.stdout.split(b"\0") if path]


def tracked_under_qa(root: Path) -> Tuple[List[str], Optional[str]]:
    """(paths git tracks in ``.qa/`` beyond the two files meant to be shared, limitation).

    A limitation means nothing here can tell: ``not_a_git_repo`` or ``git_unavailable``.
    """
    git = shutil.which("git")
    if git is None:
        return [], "git_unavailable"
    try:
        session = _Git(git, Path(root))
        inside = session.run(["rev-parse", "--is-inside-work-tree"])
        if inside.returncode != 0 or inside.stdout.strip() != b"true":
            return [], "not_a_git_repo"
        listed = session.run(["ls-files", "-z", "--", _QA_PATHSPEC])
    except _Unavailable:
        return [], "git_unavailable"
    if listed.returncode != 0:
        return [], "git_unavailable"
    names = (os.fsdecode(path) for path in listed.stdout.split(b"\0") if path)
    return sorted(name for name in names if name.lower() not in _SHAREABLE), None


def _diff_digest(session: _Git) -> Optional["hashlib._Hash"]:
    """The running SHA-256 of ``git diff HEAD --binary``, read back from a temporary file in chunks so a large diff is never
    held in memory; None when git fails."""
    with tempfile.TemporaryFile(prefix="qa-diff-") as out:
        done = session.run(["diff", "HEAD", "--no-ext-diff", "--no-textconv", "--binary", "--no-color", "--ignore-submodules=all",
                            "--", ".", _EXCLUDE_QA], stdout=out)
        if done.returncode != 0:
            return None
        out.seek(0)
        digest = hashlib.sha256()
        for chunk in iter(lambda: out.read(_CHUNK), b""):
            digest.update(chunk)
        return digest


def git_info(root: Path) -> Tuple[Optional[Dict[str, object]], Optional[str]]:
    """Return (facts, None) or (None, limitation); never raises for git problems."""
    git = shutil.which("git")
    if git is None:
        return None, "git_unavailable"
    root = Path(root)
    try:
        session = _Git(git, root)
        inside = session.run(["rev-parse", "--is-inside-work-tree"])
        if inside.returncode != 0 or inside.stdout.strip() != b"true":
            return None, "not_a_git_repo"
        head = session.run(["rev-parse", "--verify", "-q", "HEAD"])
        commit = head.stdout.strip().decode("ascii", "replace")
        if head.returncode != 0 or not commit:
            return None, "no_commit"
        branch_run = session.run(["symbolic-ref", "--short", "-q", "HEAD"])
        branch = branch_run.stdout.strip().decode("utf-8", "replace") if branch_run.returncode == 0 else None
        status = session.run(["status", "--porcelain", "--untracked-files=normal", "--ignore-submodules=all", "--", ".",
                              _EXCLUDE_QA])
        digest = _diff_digest(session)
        others = session.run(["ls-files", "--others", "--exclude-standard", "-z", "--", ".", _EXCLUDE_QA])
    except (_Unavailable, OSError):
        return None, "git_unavailable"
    if status.returncode != 0 or digest is None or others.returncode != 0:
        return None, "git_unavailable"
    pairs = sorted((p, _untracked_hash(root, p)) for p in others.stdout.split(b"\0") if p)
    for rel, file_digest in pairs:
        digest.update(b"\0" + rel + b"\0" + file_digest.encode("ascii"))
    return (
        {
            "commit": commit,
            "branch": branch,
            "dirty": bool(status.stdout.strip()),
            "diff_hash": "sha256:" + digest.hexdigest(),
        },
        None,
    )
