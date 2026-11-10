"""Git worktrees the suite owns, so that a run at another commit never touches the user's working tree.

``add`` makes ``.qa/work/wt-<12 hex of the commit>`` with ``git worktree add --detach`` and records ``worktree.created``;
``remove`` deletes only a worktree such an event names and no ``worktree.removed`` closes. Both go through the neutralised
git session of ``gitinfo``: the repository's filters, diff drivers, hooks, fsmonitor and lfs extensions run nothing, and a
user-level driver (git-lfs itself) still applies. The activity is ``static-analysis``, since creating a worktree runs no
project code; ``add`` also writes ``.git/worktrees`` and checks files out, so it refuses a workspace whose ``config.json``
was edited by hand (stricter than ``config.check_drift``, which exempts that activity).

A run whose working directory is inside an open worktree records that worktree's commit (``containing``).
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Dict, Optional

from . import config, gitinfo, ledger, state
from .errors import QaError
from .workspace import Workspace

ACTIVITY = "static-analysis"
_PREFIX = ".qa/work/wt-"
_SHORT = 12
_ADD_TIMEOUT_S = 300  # a checkout of a large tree takes longer than a read
_FULL = re.compile(r"[0-9a-f]{40}")


def _session(ws: Workspace) -> "gitinfo._Git":
    try:
        session = gitinfo.session(ws.root)
    except gitinfo.UnsafeConfiguration as refused:
        raise QaError("git_config_refused", "git does not run here: %s" % refused.reason, keys=list(refused.keys))
    except gitinfo.Unavailable:
        raise QaError("git_unavailable", "git cannot run safely here: its configuration cannot be listed or neutralised")
    if session is None:
        raise QaError("git_unavailable", "git is not installed")
    return session


def _resolve(session: "gitinfo._Git", commit: str) -> str:
    """The 40-hex commit ``commit`` names, or ``unknown_commit``; an argument that looks like an option is never passed on."""
    if not isinstance(commit, str) or not commit or commit.startswith("-") or "\0" in commit:
        raise QaError("unknown_commit", "no such commit", commit=commit if isinstance(commit, str) else None)
    try:
        done = session.run(["rev-parse", "--verify", "-q", "--end-of-options", commit + "^{commit}"])
    except gitinfo.Unavailable:
        raise QaError("git_unavailable", "git could not run")
    full = done.stdout.strip().decode("ascii", "replace")
    if done.returncode != 0 or not _FULL.fullmatch(full):
        raise QaError("unknown_commit", "no such commit in this repository", commit=commit)
    return full


def _relative(ws: Workspace, path: str) -> str:
    """``path`` (relative to the workspace, or absolute) as the posix path relative to it that events carry."""
    joined = os.path.normpath(os.path.join(str(ws.root), path))
    return os.path.relpath(joined, str(ws.root)).replace(os.sep, "/")


def open_worktrees(st: dict) -> Dict[str, dict]:
    """Path -> record, for every worktree a ``worktree.created`` event names and no ``worktree.removed`` event closes."""
    return {path: record for path, record in st.get("worktrees", {}).items() if record["removed"] is None}


def containing(ws: Workspace, st: dict, cwd: str) -> Optional[str]:
    """The path (relative to the workspace) of the open worktree that holds ``cwd``, else None."""
    here = os.path.realpath(cwd)
    for path in open_worktrees(st):
        root = os.path.join(str(ws.root), path)
        current = here
        while True:  # the filesystem says whether a directory is the worktree, so letter case in the spelling does not matter
            try:
                if os.path.samefile(current, root):
                    return path
            except OSError:
                pass
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    return None


def _refuse_drift(ws: Workspace, st: dict) -> None:
    if config.current_hash(ws) != config.accepted_hash(st):
        raise QaError("config_drift", config.DRIFT_REFUSAL)


def add(ws: Workspace, actor: ledger.Actor, commit: str) -> dict:
    """Check ``commit`` out, detached, into ``.qa/work/wt-<12 hex>`` and record it; returns path (relative), commit and event."""
    with ws.lock():
        st = state.load(ws)
        _refuse_drift(ws, st)
        session = _session(ws)
        full = _resolve(session, commit)
        path = _PREFIX + full[:_SHORT]
        target = Path(ws.root) / path
        if path in open_worktrees(st) or os.path.lexists(str(target)):
            raise QaError("worktree_exists", "a worktree for this commit already exists", path=path)
        try:
            done = session.run(["worktree", "add", "--detach", "--", str(target), full], timeout_s=_ADD_TIMEOUT_S)
        except gitinfo.Unavailable:
            raise QaError("git_unavailable", "git could not run")
        if done.returncode != 0:
            _discard(session, target)
            raise QaError("worktree_failed", "git could not create the worktree", error=done.stderr.decode("utf-8", "replace")[-300:])
        try:
            event = ledger.append(ws, actor, "worktree.created", {"path": path, "commit": full, "activity": ACTIVITY})
        except BaseException:
            _discard(session, target)
            raise
    return {"path": path, "commit": full, "event": event["id"]}


def _discard(session: "gitinfo._Git", target: Path) -> None:
    """Take a worktree out again; best effort, since it is cleaning up after a failure that is already being reported."""
    try:
        session.run(["worktree", "remove", "--force", "--", str(target)])
        session.run(["worktree", "prune"])
    except gitinfo.Unavailable:
        pass
    if target.is_dir():
        shutil.rmtree(str(target), ignore_errors=True)
        try:
            session.run(["worktree", "prune"])
        except gitinfo.Unavailable:
            pass


def remove(ws: Workspace, actor: ledger.Actor, path: str) -> dict:
    """Remove a worktree this suite created and did not already remove; any other path is ``worktree_not_ours``."""
    with ws.lock():
        st = state.load(ws)
        relative = _relative(ws, path) if isinstance(path, str) and path and "\0" not in path else None
        if relative is None or relative not in open_worktrees(st):
            raise QaError("worktree_not_ours", "the suite created no open worktree at this path", path=path)
        session = _session(ws)
        target = Path(ws.root) / relative
        try:
            done = session.run(["worktree", "remove", "--force", "--", str(target)])
            if done.returncode != 0 and not target.exists():  # already deleted by hand: git only has to forget it
                done = session.run(["worktree", "prune"])
        except gitinfo.Unavailable:
            raise QaError("git_unavailable", "git could not run")
        if done.returncode != 0:
            raise QaError("worktree_failed", "git could not remove the worktree", error=done.stderr.decode("utf-8", "replace")[-300:])
        event = ledger.append(ws, actor, "worktree.removed", {"path": relative})
    return {"path": relative, "event": event["id"]}
