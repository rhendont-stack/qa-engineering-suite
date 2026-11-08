"""Read-only facts about the user's git tree (commit, branch, dirty flag, diff hash).

Only read-only git subcommands run. Every call disables fsmonitor and the untracked cache
and sets GIT_OPTIONAL_LOCKS=0, so inspecting a tree never executes a configured hook or
rewrites its index.

Limitations returned instead of facts: not_a_git_repo, no_commit, git_unavailable.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .canonical import sha256_bytes, sha256_file

_TIMEOUT_S = 30
_EXCLUDE_QA = ":(exclude).qa"
_BASE_OPTS = ["-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false"]


class _Unavailable(Exception):
    """git is missing, cannot start, or timed out."""


def _run(git: str, root: Path, args: List[str]) -> "subprocess.CompletedProcess[bytes]":
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        return subprocess.run(
            [git, "-C", str(root), *_BASE_OPTS, *args],
            capture_output=True,
            timeout=_TIMEOUT_S,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _Unavailable() from exc


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


def git_info(root: Path) -> Tuple[Optional[Dict[str, object]], Optional[str]]:
    """Return (facts, None) or (None, limitation); never raises for git problems."""
    git = shutil.which("git")
    if git is None:
        return None, "git_unavailable"
    root = Path(root)
    try:
        inside = _run(git, root, ["rev-parse", "--is-inside-work-tree"])
        if inside.returncode != 0 or inside.stdout.strip() != b"true":
            return None, "not_a_git_repo"
        head = _run(git, root, ["rev-parse", "--verify", "-q", "HEAD"])
        commit = head.stdout.strip().decode("ascii", "replace")
        if head.returncode != 0 or not commit:
            return None, "no_commit"
        branch_run = _run(git, root, ["symbolic-ref", "--short", "-q", "HEAD"])
        branch = branch_run.stdout.strip().decode("utf-8", "replace") if branch_run.returncode == 0 else None
        status = _run(git, root, ["status", "--porcelain", "--", ".", _EXCLUDE_QA])
        diff = _run(
            git,
            root,
            ["diff", "HEAD", "--no-ext-diff", "--no-textconv", "--binary", "--no-color", "--", ".", _EXCLUDE_QA],
        )
        others = _run(git, root, ["ls-files", "--others", "--exclude-standard", "-z", "--", ".", _EXCLUDE_QA])
    except _Unavailable:
        return None, "git_unavailable"
    if status.returncode != 0 or diff.returncode != 0 or others.returncode != 0:
        return None, "git_unavailable"
    pairs = sorted((p, _untracked_hash(root, p)) for p in others.stdout.split(b"\0") if p)
    payload = bytearray(diff.stdout)
    for rel, digest in pairs:
        payload += b"\0" + rel + b"\0" + digest.encode("ascii")
    return (
        {
            "commit": commit,
            "branch": branch,
            "dirty": bool(status.stdout.strip()),
            "diff_hash": sha256_bytes(bytes(payload)),
        },
        None,
    )
