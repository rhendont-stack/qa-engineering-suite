"""Records of the files the suite writes into a project (§12.4): ``qa changes list`` shows exactly what it touched.

The suite never modifies production code, and writes tests only to paths a ``write-tests`` grant names. ``record`` is
called after a file is written: it checks the path, the grant and, for a change to a file git tracks, the user's approval,
and only then records one ``file.created`` or ``file.modified`` event per path, with the sha256 of the bytes on disk. The
checks, in this order, and a refusal records nothing at all:

- the kind is ``created`` or ``modified`` and at least one path is given (``invalid_kind``, ``invalid_path``);
- every path is resolved inside the workspace root without following a symlink: a ``..`` component, a path outside the root,
  a symlink anywhere on the way, a missing file or one that is not a regular file is ``invalid_path``;
- no path is agent or IDE configuration (``vocab.PROTECTED_PATHS``, any depth, any case): ``protected_path``, whatever grants exist;
- a grant that this machine recorded (``state.load`` holds no other), is not revoked, has not expired and names the activity
  ``write-tests`` has ``paths`` globs matching the path, or it is ``write_not_authorized``; the globs match as
  ``fnmatch.fnmatchcase`` matches, with ``**/`` also standing for no directory, as the ``write_scopes`` rules read them;
- ``modified`` of a file git tracks (or of any file when git cannot say) needs ``justification`` and ``approval``
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
from typing import Any, Dict, List, Optional, Tuple

from . import gitinfo, ids, ledger, state, vocab
from .canonical import sha256_bytes
from .errors import QaError
from .workspace import Workspace

# QaError codes raised here: invalid_kind, invalid_path, invalid_testcase, protected_path, write_not_authorized,
# justification_required, approval_required; plus whatever ledger.append raises.

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
    here = root
    for part in rel.split(os.sep):
        here = os.path.join(here, part)
        try:
            mode = os.lstat(here).st_mode
        except OSError:
            raise _invalid("no such file", path=raw)
        if stat.S_ISLNK(mode):
            raise _invalid("a symbolic link is never followed", path=raw)
    if not stat.S_ISREG(mode):
        raise _invalid("not a regular file", path=raw)
    return rel.replace(os.sep, "/")


def _read(ws: Workspace, rel: str) -> bytes:
    try:
        with open(os.path.join(str(ws.root), rel), "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise _invalid("the file cannot be read: %s" % exc.strerror, path=rel)


def _is_protected(rel: str) -> bool:
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
        rels: List[str] = []
        for raw in paths:
            rel = resolve(ws, raw)
            if rel not in rels:
                rels.append(rel)
        for rel in rels:
            if _is_protected(rel):
                raise QaError("protected_path", "agent and IDE configuration is never written (§12.4)", path=rel)
        grants = {}
        for rel in rels:
            grants[rel] = _granting(st, rel)
            if grants[rel] is None:
                raise QaError("write_not_authorized", "no valid write-tests grant covers this path: record one with `qa authorize`",
                              path=rel)
        tracked_set = gitinfo.tracked_files(ws.root) if kind == "modified" else set()
        tracked = {rel: tracked_set is None or rel in tracked_set for rel in rels}  # git cannot say: assume it tracks
        if kind == "modified" and any(tracked.values()):
            if not _text(justification):
                raise QaError("justification_required", "modifying a file git tracks needs the reason for the change (§12.4)")
            if not _text(approval):
                raise QaError("approval_required", "modifying a file git tracks needs the user's approval quote (§12.4)")
        digests = {rel: sha256_bytes(_read(ws, rel)) for rel in rels}
        files = []
        for rel in rels:
            data: Dict[str, Any] = {"path": rel, "sha256": digests[rel], "grant": grants[rel], "testcase": testcase}
            if kind == "modified":
                data["tracked"] = tracked[rel]
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
            required = rec["kind"] == "modified" and rec["tracked"]
            approval = rec["approval"]
            rows.append({
                "path": path, "kind": rec["kind"], "event": rec["event"], "grant": rec["grant"], "testcase": rec["testcase"],
                "recorded_sha256": rec["sha256"], "current_sha256": current, "changed_since": current != rec["sha256"],
                "approval_required": required, "approved": (not required) or approval is not None,
                "approval_event": None if approval is None else approval["event"],
                "justification": None if approval is None else approval["justification"],
            })
    return rows
