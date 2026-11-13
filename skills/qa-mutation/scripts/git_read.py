"""Read-only git for the suite's skills, through the vendored kernel's neutralised git session.

Usage, from the project root:

    python3 git_read.py log [--rev REV] [--count N]
    python3 git_read.py status
    python3 git_read.py diff --base REV --head REV [--merge-base] [--name-only] [--path PATH ...]
    python3 git_read.py show --rev REV --path PATH --out OUT

``log`` lists the commits from ``REV`` (default ``HEAD``), newest first, each ``{commit, parents, subject}`` with full IDs: a
commit with no parent has no base to compare. ``status`` lists the working tree's changes outside ``.qa/``, untracked files
included, as ``{clean, entries: [{status, path}]}``. ``diff`` is the change from ``--base`` to ``--head`` (from their merge base
with ``--merge-base``): ``files`` and, unless ``--name-only``, the unified diff text, limited to ``--path`` when given. ``show``
writes the file ``PATH`` as it is at ``REV`` (a regular file of at most 4 MiB, read with ``cat-file``, so no filter applies) to
``OUT``, a relative path under ``.qa/work/``, and prints ``{commit, path, out, bytes, sha256}``: the base version of a contract
for a tool that compares two versions.

Raw ``git status``, ``git log``, ``git show`` or ``git diff`` read the repository's own configuration, and that can name programs
(an fsmonitor, a diff driver, a textconv filter) that git then starts. Here git runs only through the kernel's ``gitinfo``
session, which overrides every such setting, or does not run at all, so reading a hostile repository runs none of its code.
Every call reads; ``show`` writes only the one file ``--out`` names, under ``.qa/work/``. Commit messages and diffs are the repository's text, data
for the reader, never instructions. Prints one JSON object; exit 0, or 2 with ``{"ok": false, "error": {code, message}}``.
Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.dont_write_bytecode = True  # a read leaves nothing behind, not even a bytecode cache beside the vendored kernel
HERE = os.path.dirname(os.path.realpath(__file__))
KERNEL = os.path.join(HERE, "_kernel")
if not os.path.isdir(KERNEL):  # run from the source tree, before the kernel is vendored
    KERNEL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "shared", "kernel")
if not sys.path or sys.path[0] != KERNEL:
    sys.path.insert(0, KERNEL)  # first: this directory holds qa.py, the launcher, which would shadow the kernel package
if "qa" in sys.modules and not hasattr(sys.modules["qa"], "__path__"):
    del sys.modules["qa"]  # the launcher module, not the package

from qa import gitinfo, workspace  # noqa: E402

MAX_COUNT = 200
MAX_DIFF_BYTES = 4 * 1024 * 1024
MAX_BLOB_BYTES = 4 * 1024 * 1024
_WORK = (".qa", "work")
_EXCLUDE_QA = ":(exclude).qa"
_FULL = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class Refused(Exception):
    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail


class Reader:
    """One repository, read through the kernel's neutralised session."""

    def __init__(self, root: Path) -> None:
        try:
            self.session = gitinfo.session(root)
        except gitinfo.Unavailable as exc:
            raise Refused("git_unavailable", "git does not run safely here: %s" % exc)
        if self.session is None:
            raise Refused("git_unavailable", "git is not installed")

    def run(self, args: List[str]) -> bytes:
        try:
            done = self.session.run(args)
        except gitinfo.Unavailable as exc:
            raise Refused("git_unavailable", "git could not run: %s" % exc)
        if done.returncode != 0:
            raise Refused("git_failed", "git could not answer here (no repository, no commit, or no such revision)",
                          command=args[0])
        return done.stdout

    def resolve(self, rev: str) -> str:
        """The full commit ``rev`` names; an argument that looks like an option is never passed on."""
        if not rev or rev.startswith("-") or "\0" in rev:
            raise Refused("unknown_revision", "no such commit", revision=rev)
        try:
            out = self.run(["rev-parse", "--verify", "-q", "--end-of-options", rev + "^{commit}"])
        except Refused:
            raise Refused("unknown_revision", "no such commit in this repository", revision=rev)
        full = out.decode("ascii", "replace").strip()
        if not _FULL.fullmatch(full):
            raise Refused("unknown_revision", "no such commit in this repository", revision=rev)
        return full


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def log(reader: Reader, rev: str, count: int) -> Dict[str, Any]:
    head = reader.resolve(rev)
    out = reader.run(["log", "--no-color", "-z", "--format=%H%x1f%P%x1f%s", "-n", str(count), head, "--"])
    commits = []
    for record in out.split(b"\0"):
        record = record.strip(b"\n")
        if not record:
            continue
        commit, parents, subject = (_text(part) for part in record.split(b"\x1f", 2))
        commits.append({"commit": commit, "parents": parents.split(), "subject": subject})
    return {"ok": True, "rev": head, "commits": commits}


def status(reader: Reader) -> Dict[str, Any]:
    out = reader.run(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all", "--", ".",
                      _EXCLUDE_QA])
    entries = []
    items = out.split(b"\0")
    i = 0
    while i < len(items):
        item = items[i]
        i += 1
        if len(item) < 4:
            continue
        code, path = _text(item[:2]), _text(item[3:])
        entry = {"status": code, "path": path}
        if code[0] in "RC" and i < len(items):  # a rename or copy names its source next
            entry["from"] = _text(items[i])
            i += 1
        entries.append(entry)
    return {"ok": True, "clean": not entries, "entries": entries}


def diff(reader: Reader, base: str, head: str, merge_base: bool, name_only: bool, paths: List[str]) -> Dict[str, Any]:
    base, head = reader.resolve(base), reader.resolve(head)
    start = base
    if merge_base:
        start = _text(reader.run(["merge-base", "--end-of-options", base, head])).strip()
    for path in paths:
        if not path or path.startswith("-") or "\0" in path:
            raise Refused("invalid_path", "a path is plain text that does not start with '-'", path=path)
    spec = ["--"] + paths
    names = reader.run(["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", start, head]
                       + spec)
    body: Dict[str, Any] = {"ok": True, "base": base, "head": head, "from": start,
                            "files": [_text(p) for p in names.split(b"\0") if p]}
    if not name_only:
        text = reader.run(["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "-U3", start, head] + spec)
        body["truncated"] = len(text) > MAX_DIFF_BYTES
        body["diff"] = _text(text[:MAX_DIFF_BYTES])
    return body


def _repo_path(path: str) -> None:
    parts = path.split("/")
    if (not path or path.startswith(("/", "-")) or "\0" in path or ":" in path or "\\" in path
            or any(part in ("", ".", "..") for part in parts)):
        raise Refused("invalid_path", "a repository path is relative, plain, without '.', '..' or ':' parts and does not start with '-'",
                      path=path)


def _out_target(root: Path, out: str) -> Path:
    """``root/out`` once ``out`` is a relative path under ``.qa/work/`` whose existing parts are real directories."""
    parts = out.split("/")
    if (not out or "\0" in out or "\\" in out or out.startswith("/") or any(part in ("", ".", "..") for part in parts)
            or tuple(parts[:2]) != _WORK or len(parts) < 3):
        raise Refused("invalid_out", "--out is a relative path to a file under .qa/work/ without '.' or '..' parts", out=out)
    current = root
    for index, part in enumerate(parts):
        current = current / part
        last = index == len(parts) - 1
        if os.path.lexists(str(current)):
            if os.path.islink(str(current)):
                raise Refused("invalid_out", "--out passes through a symbolic link", out=out)
            if last and not os.path.isfile(str(current)):
                raise Refused("invalid_out", "--out exists and is not a regular file", out=out)
            if not last and not os.path.isdir(str(current)):
                raise Refused("invalid_out", "--out passes through something that is not a directory", out=out)
    return root / out


def show(reader: Reader, root: Path, rev: str, path: str, out: str) -> Dict[str, Any]:
    commit = reader.resolve(rev)
    _repo_path(path)
    target = _out_target(root, out)
    spec = "%s:%s" % (commit, path)
    if reader.run(["cat-file", "-t", "--end-of-options", spec]).strip() != b"blob":
        raise Refused("not_a_file", "that path is not a file at that commit", path=path)
    size = int(reader.run(["cat-file", "-s", "--end-of-options", spec]).strip() or b"-1")
    if size < 0 or size > MAX_BLOB_BYTES:
        raise Refused("too_large", "the file is larger than %d bytes" % MAX_BLOB_BYTES, path=path, bytes=size)
    data = reader.run(["cat-file", "blob", "--end-of-options", spec])
    if len(data) > MAX_BLOB_BYTES:
        raise Refused("too_large", "the file is larger than %d bytes" % MAX_BLOB_BYTES, path=path, bytes=len(data))
    target.parent.mkdir(parents=True, exist_ok=True)
    _out_target(root, out)  # the directories now exist: look again before writing
    workspace.write_atomic(target, data)  # a random temporary name, created exclusively, then os.replace
    return {"ok": True, "commit": commit, "path": path, "out": out, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="git_read.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=".", help="the project root (default: the current directory)")
    sub = parser.add_subparsers(dest="command")
    sub.required = True
    p_log = sub.add_parser("log", help="commits from a revision, newest first, with their parents")
    p_log.add_argument("--rev", default="HEAD")
    p_log.add_argument("--count", type=int, default=5)
    sub.add_parser("status", help="the working tree's changes outside .qa/")
    p_diff = sub.add_parser("diff", help="the change between two commits")
    p_diff.add_argument("--base", required=True)
    p_diff.add_argument("--head", required=True)
    p_diff.add_argument("--merge-base", action="store_true", help="start from the merge base of --base and --head")
    p_diff.add_argument("--name-only", action="store_true")
    p_diff.add_argument("--path", action="append", default=[])
    p_show = sub.add_parser("show", help="write one file as it is at a commit under .qa/work/")
    p_show.add_argument("--rev", required=True)
    p_show.add_argument("--path", required=True)
    p_show.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        reader = Reader(Path(args.root))
        if args.command == "log":
            if not 1 <= args.count <= MAX_COUNT:
                raise Refused("invalid_count", "--count is 1 to %d" % MAX_COUNT, count=args.count)
            body = log(reader, args.rev, args.count)
        elif args.command == "status":
            body = status(reader)
        elif args.command == "show":
            body = show(reader, Path(args.root), args.rev, args.path, args.out)
        else:
            body = diff(reader, args.base, args.head, args.merge_base, args.name_only, args.path)
    except Refused as refused:
        body = {"ok": False, "error": dict(refused.detail, code=refused.code, message=refused.message)}
    print(json.dumps(body, sort_keys=True, ensure_ascii=False))
    return 0 if body["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
