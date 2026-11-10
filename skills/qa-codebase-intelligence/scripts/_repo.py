"""Shared helpers of the codebase-intelligence scripts: git access that never writes, the list of repository files, and
the fingerprint that decides whether a map is still fresh. Standard library only."""
from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

SKIPPED_DIRS = frozenset({".git", ".qa", ".claude", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", "target", "build",
                          "dist", ".tox", ".gradle", ".idea", ".pytest_cache", ".mypy_cache", "vendor"})
LOCKFILES = frozenset({"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "Pipfile.lock", "Cargo.lock",
                       "go.sum", "gradle.lockfile", "composer.lock", "Gemfile.lock"})
LANGUAGES = {".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
             ".ts": "typescript", ".tsx": "typescript", ".java": "java", ".kt": "kotlin", ".go": "go", ".rs": "rust",
             ".rb": "ruby", ".php": "php", ".cs": "csharp"}
MAX_FILE_BYTES = 1 << 20
MAX_FILES = 20000


def language_of(path: str) -> Optional[str]:
    return LANGUAGES.get(os.path.splitext(path)[1].lower())


NO_EXEC = ["-c", "log.showSignature=false", "-c", "gpg.program=" + os.devnull, "-c", "gpg.openpgp.program=" + os.devnull,
           "-c", "gpg.ssh.program=" + os.devnull, "-c", "gpg.x509.program=" + os.devnull, "-c", "core.quotepath=off", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull,
           "-c", "core.untrackedCache=false", "-c", "protocol.ext.allow=never"]
HASH_CAP = 32 << 20


def _run(root: Path, config: List[str], args: List[str], env: Dict[str, str]):
    try:
        done = subprocess.run(["git", "-C", str(root)] + config + args, capture_output=True, env=env, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


PROGRAM_VARS = {"filter": ("clean", "smudge", "process"), "diff": ("command", "textconv")}
REFUSED: List[str] = []  # why git was not run, when the repository's config could not be neutralised


def _program_overrides(root: Path, env: Dict[str, str]) -> Optional[List[str]]:
    """``-c`` options that blank every setting of the repository's own config (includes and ``config.worktree`` too) that names a
    program git would run on the read-only commands: ``filter.<name>.clean|smudge|process`` and ``diff.<name>.command|textconv``.
    None when the config cannot be listed or a key cannot be overridden: git must not run unprotected."""
    out = _run(root, [], ["config", "--null", "--name-only", "--list"], env)
    if out is None:
        return None
    over: List[str] = []
    for raw in out.split(b"\0"):
        parts = raw.decode("utf-8", "replace").split(".")
        if len(parts) >= 3 and parts[0].lower() in PROGRAM_VARS and parts[-1].lower() in PROGRAM_VARS[parts[0].lower()]:
            key = ".".join(parts)
            if "=" in key or "\n" in key:
                return None
            over += ["-c", key + "="]
    return over


def git(root: Path, args: List[str], binary: bool = False):
    """Output of a read-only git command, or None when git is missing, the directory is not a repository, or it fails. The
    repository's own config is data: nothing it configures (fsmonitor, hooks, filters, external diff, textconv) may run."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    over = _program_overrides(root, env)
    if over is None:
        REFUSED.append("git was not run: the repository's configuration could not be listed or neutralised")
        return None
    out = _run(root, NO_EXEC + over, args, env)
    if out is None:
        return None
    return out if binary else out.decode("utf-8", "replace")


def head(root: Path) -> Optional[str]:
    out = git(root, ["rev-parse", "--verify", "HEAD"])
    value = out.strip() if out else ""
    return value if len(value) == 40 and all(c in "0123456789abcdef" for c in value) else None


def excluded(path: str) -> bool:
    return path.split("/", 1)[0] in (".qa", ".claude") or any(p in SKIPPED_DIRS for p in path.split("/")[:-1])


def _listing(root: Path):
    root = Path(root)
    listed = git(root, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"], binary=True)
    if listed is not None:
        names = [os.fsdecode(n) for n in listed.split(b"\0") if n]
    else:
        names = []
        for here, dirs, found in os.walk(str(root)):
            dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRS and not os.path.islink(os.path.join(here, d)))
            rel = os.path.relpath(here, str(root))
            names += [os.path.normpath(os.path.join(rel, f)).replace(os.sep, "/") for f in found]
    keep: List[str] = []
    bad: List[str] = []
    for name in sorted(set(names)):
        try:
            name.encode("utf-8")
        except UnicodeEncodeError:
            if not excluded(name):
                bad.append(name)
            continue
        if not excluded(name) and _lstat(root, name) is not None:
            keep.append(name)
    return keep[:MAX_FILES], bad


def files(root: Path) -> List[str]:
    """Sorted POSIX paths of the regular files of the repository: tracked plus untracked and not ignored when it is a git
    repository, else every file below ``root``. Never ``.qa/``, ``.claude/`` or dependency and build trees, symlinks, or
    names that are not valid UTF-8 (see ``undecodable``)."""
    return _listing(root)[0]


def undecodable(root: Path) -> List[str]:
    """The names of repository files that are not valid UTF-8, with ``\\x`` escapes for the bad bytes: no script reads them."""
    return [os.fsencode(n).decode("ascii", "backslashreplace") for n in _listing(root)[1]]


def _lstat(root: Path, rel: str, limit: Optional[int] = None):
    """The ``lstat`` of a regular file that lies inside ``root`` (no symlink in the way), else None."""
    try:
        full = os.path.join(str(root), rel)
        info = os.lstat(full)
        if not stat.S_ISREG(info.st_mode) or (limit is not None and info.st_size > limit):
            return None
        if os.path.realpath(full) != os.path.join(os.path.realpath(str(root)), os.path.normpath(rel)):
            return None
        return info
    except (OSError, ValueError):
        return None


def read_text(root: Path, rel: str, limit: int = MAX_FILE_BYTES) -> Optional[str]:
    """The text of a regular file inside ``root`` (never a symlink or a special file), or None for a binary, an oversized
    or an unreadable one."""
    if _lstat(root, rel, limit) is None:
        return None
    try:
        with open(os.path.join(str(root), rel), "rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        return None
    if len(data) > limit or b"\0" in data:
        return None
    return data.decode("utf-8", "replace")


def _changed(root: Path) -> Optional[List[str]]:
    out = git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all"], binary=True)
    if out is None:
        return None
    fields = [os.fsdecode(f) for f in out.split(b"\0") if f]
    paths: List[str] = []
    skip = False
    for field in fields:
        if skip:  # the origin of a rename or copy
            skip = False
            continue
        code, path = field[:2], field[3:]
        if "R" in code or "C" in code:
            skip = True
        if not excluded(path):
            paths.append(path)
    return sorted(set(paths))


def _fingerprint(root: Path, name: str) -> bytes:
    """What stands for one changed path in the tree fingerprint, without ever following a link or reading a special file:
    the digest of a regular file's content, the target string of a symlink, else a marker of its type and size."""
    full = os.path.join(str(root), name)
    try:
        info = os.lstat(full)
    except (OSError, ValueError):
        return b"missing"
    if stat.S_ISLNK(info.st_mode):
        try:
            return b"link:" + hashlib.sha256(os.fsencode(os.readlink(full))).digest()
        except OSError:
            return b"link:unreadable"
    if not stat.S_ISREG(info.st_mode):
        return b"special:%o" % stat.S_IFMT(info.st_mode)
    if info.st_size > HASH_CAP:
        return b"large:%d" % info.st_size
    if _lstat(root, name) is None:
        return b"outside"
    try:
        digest = hashlib.sha256()
        with open(full, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.digest()
    except OSError:
        return b"missing"


def tree_state(root: Path) -> Dict[str, object]:
    """``{"commit", "dirty", "tree"}``: the commit (None outside git), whether the working tree differs from it (outside
    ``.qa/`` and ``.claude/``), and a ``sha256:`` fingerprint of the commit plus the content of every changed file. Without
    git the fingerprint covers every file."""
    root = Path(root)
    commit = head(root)
    changed = _changed(root) if commit else None
    digest = hashlib.sha256()
    digest.update(("commit:%s\n" % commit).encode())
    names = changed if changed is not None else files(root)
    for name in names:
        digest.update(b"%s\0" % os.fsencode(name))
        digest.update(_fingerprint(root, name))
    return {"commit": commit, "dirty": bool(changed) if changed is not None else False, "tree": "sha256:" + digest.hexdigest()}
