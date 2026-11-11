"""Records of the files the suite writes into a project (§12.4): ``qa changes list`` shows exactly what it touched.

The suite never modifies production code, and writes tests only to paths a ``write-tests`` grant names. ``record`` is
called after a file is written: it checks the path, the grant and, for a change to a file git tracks, the user's approval,
and only then records one ``file.created`` or ``file.modified`` event per path, with the sha256 of the bytes on disk. The
checks, in this order, and a refusal records nothing at all:

- the kind is ``created`` or ``modified``, the test case id has its form, and at least one path is given (``invalid_kind``,
  ``invalid_testcase``, ``invalid_path``);
- the workspace's authority holds: the refusals ``execution`` applies to a run (``integrity.authority``: a tracked ``.qa/``, a
  ledger that does not verify, an unusable key) and a drifted ``config.json`` refuse a record too (``authority_refused``,
  ``config_drift``), since a grant counts for nothing there;
- every path is resolved inside the workspace root without following a symlink: a ``..`` component, a path outside the root,
  a symlink anywhere on the way, a missing file or one that is not a regular file is ``invalid_path``. A path is then
  spelled as the filesystem lists it, so on one that ignores case or Unicode form every spelling is one file, one key;
- no path is agent or IDE configuration (``vocab.PROTECTED_PATHS``, any depth, any case) or under ``.qa/``: ``protected_path``,
  whatever grants exist;
- a grant that this machine recorded (``state.load`` holds no other), is not revoked, has not expired and names the activity
  ``write-tests`` has ``paths`` globs matching the path, or it is ``write_not_authorized``; the globs match as
  ``fnmatch.fnmatchcase`` matches, with ``**/`` also standing for no directory, as the ``write_scopes`` rules read them;
- ``created`` of a file git tracks is ``already_tracked`` (that is a modification). A listing name is the same file as the
  path when it is the same name in Unicode form, or another spelling of the same device and inode; otherwise it is another file;
- when git cannot answer at all (missing, its configuration cannot be neutralised, or it refuses a repository that is
  there), nothing can be shown untracked, so every path counts as tracked: ``created`` is ``tracked_unknown`` (record it as
  modified, which needs the same justification and approval as any tracked modification), and ``modified`` goes on with
  ``tracked: true`` and ``git_unknown: true`` in its event (each flag is written only when true);
- ``modified`` of a file counted as tracked needs ``justification`` and ``approval``
  (``justification_required``, ``approval_required``), and the approval travels in its own signed event,
  ``file.modification_approved``, over the path and the bytes' sha256.

``file.created`` and ``file.modified`` are plain events and always fold, so a change never disappears from the list; only
the approval is conferring, and a tracked modification without a valid one is listed with ``approved: false``.
"""
from __future__ import annotations

import fnmatch
import os
import re
import stat
import unicodedata
from typing import Any, Dict, List, Optional, Set, Tuple

from . import gitinfo, ids, integrity, ledger, state, vocab, worktree
from .canonical import sha256_bytes
from .errors import QaError
from .workspace import Workspace

# QaError codes raised here: invalid_kind, invalid_path, invalid_testcase, protected_path, write_not_authorized,
# justification_required, approval_required, already_tracked, tracked_unknown, authority_refused, config_drift; plus whatever ledger.append raises.

KINDS = ("created", "modified")
ACTIVITY = "write-tests"
_TC = re.compile(r"TC-[0-9]{3,}")
_PROTECTED = tuple((tuple(e.rstrip("/").lower().split("/")), e.endswith("/")) for e in vocab.PROTECTED_PATHS)


def _invalid(why: str, **context: Any) -> QaError:
    return QaError("invalid_path", why, **context)


def resolve(ws: Workspace, raw: Any) -> str:
    """The posix path of ``raw`` relative to the workspace root, or ``invalid_path``.

    Nothing is followed: a ``..`` component, a path outside the root, or a symlink among the components is refused, and the
    last component must be a regular file.
    """
    if not isinstance(raw, str) or not raw or "\0" in raw:
        raise _invalid("a path is non-empty text without NUL")
    if ".." in re.split(r"[/\\]", raw):
        raise _invalid("a path must not contain '..'", path=raw)
    root = str(ws.root)
    rel = os.path.relpath(os.path.normpath(os.path.join(root, raw)), root)
    if rel == "." or rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
        raise _invalid("the path is outside the project", path=raw)
    here, spelled = root, []
    for part in rel.split(os.sep):
        here = os.path.join(here, part)
        try:
            mode = os.lstat(here).st_mode
        except OSError:
            raise _invalid("no such file", path=raw)
        if stat.S_ISLNK(mode):
            raise _invalid("a symbolic link is never followed", path=raw)
        actual = _on_disk(os.path.dirname(here), part)
        spelled.append(actual)
        here = os.path.join(os.path.dirname(here), actual)
    if not stat.S_ISREG(mode):
        raise _invalid("not a regular file", path=raw)
    return "/".join(spelled)


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _on_disk(parent: str, part: str) -> str:
    """``part`` as the directory ``parent`` spells it. On a filesystem that ignores case or Unicode form, a variant reaches
    the same file; its one true spelling is the entry the filesystem lists, so a file has one name here."""
    try:
        names = os.listdir(parent)
    except OSError:
        return part
    if part in names:
        return part
    wanted = _identity(os.path.join(parent, part))
    for name in names:
        if _fold(name) == _fold(part) and wanted is not None and _identity(os.path.join(parent, name)) == wanted:
            return name
    return part


def _identity(path: str) -> Optional[Tuple[int, int]]:
    """(device, inode) of ``path`` itself, never of what a symlink points at; None for a symlink or a path that is not there."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return None if stat.S_ISLNK(info.st_mode) else (info.st_dev, info.st_ino)


def _read(ws: Workspace, rel: str) -> bytes:
    try:
        fd = os.open(os.path.join(str(ws.root), rel), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        raise _invalid("the file cannot be read: %s" % exc.strerror, path=rel)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _invalid("not a regular file", path=rel)
        with open(fd, "rb", closefd=False) as handle:
            return handle.read()
    except OSError as exc:
        raise _invalid("the file cannot be read: %s" % exc.strerror, path=rel)
    finally:
        os.close(fd)


def _is_protected(rel: str) -> bool:
    if rel.split("/")[0].lower() == ".qa":  # the workspace's own ledger, key and evidence are never project files
        return True
    segments = [s.lower() for s in rel.split("/") if s not in ("", ".")]
    for entry, is_dir in _PROTECTED:
        n = len(entry)
        for k in range(len(segments) - n + 1):
            if tuple(segments[k:k + n]) == entry and (is_dir or k + n == len(segments)):
                return True
    return False


def _glob_matches(glob: str, rel: str) -> bool:
    segments = [s for s in glob.replace("\\", "/").split("/") if s not in ("", ".")]
    collapsed = [s for i, s in enumerate(segments) if not (s == "**" and i < len(segments) - 1)]
    return any(fnmatch.fnmatchcase(rel, "/".join(segs)) for segs in (segments, collapsed))


def _granting(st: dict, rel: str) -> Optional[str]:
    """The id of the first grant (by number) that covers writing ``rel`` now, else None."""
    now = ids.now_utc()
    for grant in sorted(st["authorizations"].values(), key=lambda g: int(g["id"][len("AUTH-"):])):
        if grant["revoked_at"] is not None or ACTIVITY not in grant["activities"]:
            continue
        try:
            if not now < ids.parse_ts(grant["expires_at"]):
                continue
        except QaError:
            continue
        paths = grant["limits"].get(ACTIVITY, {}).get("paths")
        if isinstance(paths, list) and any(isinstance(g, str) and _glob_matches(g, rel) for g in paths):
            return grant["id"]
    return None


def _is_tracked(ws: Workspace, rel: str, listing: Set[str]) -> bool:
    """Whether git's ``listing`` holds ``rel``: the same name in Unicode form, or another spelling the filesystem says is the
    same file (same device and inode). Where it does not, as with two case-variants on a case-sensitive one, they are two files."""
    here = unicodedata.normalize("NFC", rel)
    if any(unicodedata.normalize("NFC", name) == here for name in listing):
        return True
    own = _identity(os.path.join(str(ws.root), rel))
    return own is not None and any(_fold(name) == _fold(rel) and _identity(os.path.join(str(ws.root), name)) == own
                                   for name in listing)


def _text(value: Optional[str]) -> bool:
    return isinstance(value, str) and bool(value.strip())


def record(ws: Workspace, actor: ledger.Actor, paths: List[str], *, kind: str, testcase: Optional[str],
           justification: Optional[str] = None, approval: Optional[str] = None) -> dict:
    """Record ``paths`` as written by the suite; see the module docstring. Returns ``{kind, files: [{path, sha256, grant, event}]}``."""
    if kind not in KINDS:
        raise QaError("invalid_kind", "kind is created or modified", kind=kind)
    if not paths:
        raise _invalid("at least one path is needed")
    if testcase is not None and not (isinstance(testcase, str) and _TC.fullmatch(testcase)):
        raise QaError("invalid_testcase", "a test case id looks like TC-001")
    with ws.lock():
        st = state.load(ws)
        refused_authority = integrity.authority(ws)["refused"]  # as execution does: a shipped or unverifiable ledger authorizes nothing
        if refused_authority:
            raise QaError("authority_refused", refused_authority[0], limitations=[r.split(":")[0] for r in refused_authority])
        worktree._refuse_drift(ws, st)
        rels: List[str] = []
        for raw in paths:
            rel = resolve(ws, raw)
            if rel not in rels:
                rels.append(rel)
        for rel in rels:
            if _is_protected(rel):
                raise QaError("protected_path", "agent and IDE configuration is never written", path=rel)
        grants = {}
        for rel in rels:
            grants[rel] = _granting(st, rel)
            if grants[rel] is None:
                raise QaError("write_not_authorized", "no valid write-tests grant covers this path: record one with `qa authorize`",
                              path=rel)
        listing = gitinfo.tracked_files(ws.root)
        git_unknown = listing is None
        if listing is None:  # fail closed: nothing can be shown untracked
            if kind == "created":
                raise QaError("tracked_unknown", "git cannot say whether this file is tracked here, so it cannot be recorded as "
                              "created: record it as modified, with a justification and the user's approval", path=rels[0])
            tracked = {rel: True for rel in rels}
        else:
            tracked = {rel: _is_tracked(ws, rel, listing) for rel in rels}
        if kind == "created":
            for rel in rels:
                if tracked[rel]:
                    raise QaError("already_tracked", "git tracks this file, so writing it is a modification: record it as "
                                  "modified, with a justification and the user's approval", path=rel)
        if kind == "modified" and any(tracked.values()):
            if not _text(justification):
                raise QaError("justification_required", "modifying a file git tracks needs the reason for the change")
            if not _text(approval):
                raise QaError("approval_required", "modifying a file git tracks needs the user's approval quote")
        digests = {rel: sha256_bytes(_read(ws, rel)) for rel in rels}
        files = []
        for rel in rels:
            data: Dict[str, Any] = {"path": rel, "sha256": digests[rel], "grant": grants[rel], "testcase": testcase}
            if kind == "modified" and tracked[rel]:  # flags are written only when true (absent means false)
                data["tracked"] = True
                if git_unknown:
                    data["git_unknown"] = True
            event = ledger.append(ws, actor, "file." + kind, data)
            row = {"path": rel, "sha256": digests[rel], "grant": grants[rel], "event": event["id"]}
            if kind == "modified" and tracked[rel]:
                signed = ledger.append(ws, actor, "file.modification_approved", {
                    "path": rel, "sha256": digests[rel], "justification": justification, "approval": approval})
                row["approval_event"] = signed["id"]
            files.append(row)
    return {"kind": kind, "files": files}


def list_changes(ws: Workspace, st: dict) -> List[dict]:
    """One row per recorded change, in recording order per path, with the file's bytes as they are now.

    ``changed_since`` is true when the current sha256 differs from the recorded one; a missing file, a symlink or a
    non-regular file has ``current_sha256: null`` and counts as changed. ``approval_required`` is true for the modification
    of a file git tracked; ``approved`` is false for such a change without a valid signed approval of those bytes.
    """
    rows = []
    for path, records in st["files"].items():
        try:
            current: Optional[str] = sha256_bytes(_read(ws, resolve(ws, path)))
        except QaError:
            current = None
        for rec in records:
            required = rec["kind"] == "modified" and rec.get("tracked", False)
            approval = rec["approval"]
            rows.append({
                "path": path, "kind": rec["kind"], "event": rec["event"], "grant": rec["grant"], "testcase": rec["testcase"],
                "recorded_sha256": rec["sha256"], "current_sha256": current, "changed_since": current != rec["sha256"],
                "git_unknown": rec.get("git_unknown", False), "approval_required": required, "approved": (not required) or approval is not None,
                "approval_event": None if approval is None else approval["event"],
                "justification": None if approval is None else approval["justification"],
            })
    return rows
