from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from . import SCHEMA_VERSION, schema
from .canonical import loads
from .errors import QaError
from .ids import new_id

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt

# QaError codes raised here: no_workspace, invalid_workspace, config_invalid, lock_timeout, schema_too_new,
# schema_invalid, invalid_session, invalid_json (from canonical.loads).

SUBDIRS = ("ledger", "evidence", "state", "reports", "work")
AUTHORITY_KEY_FILE = "authority.key"
GITIGNORE = "*\n!.gitignore\n!config.json\n"
_POLL_S = 0.05
_VERSION_RE = re.compile(r"[0-9]+(\.[0-9]+)*")
MAX_CONFIG_BYTES = 1 << 20
_MAX_SESSION_BYTES = 256

# Reentrancy state, keyed by the resolved lock path: key -> [thread id, depth].
_HELD: Dict[str, List[int]] = {}
_GUARD = threading.Lock()


class FileRefused(OSError):
    """Not a regular file. An ``OSError``, so every caller that already handles an unreadable file handles this."""


class FileTooLarge(FileRefused):
    """A regular file over the size the caller allows."""


def read_limited(path: Path, limit: int, *, follow: bool = False) -> bytes:
    """``path``'s bytes when it is a regular file of at most ``limit`` bytes, otherwise an ``OSError``.

    Opening with ``O_NONBLOCK`` keeps a FIFO from hanging and ``fstat`` answers for the object actually opened, so no
    file can be swapped in after the check. ``follow`` is for a path the user named; a file inside ``.qa/`` is never a
    symlink the kernel made.
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | (0 if follow else getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(str(path), flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise FileRefused("not a regular file: %s" % path)
        if info.st_size > limit:
            raise FileTooLarge("over %d bytes: %s" % (limit, path))
        with open(fd, "rb", closefd=False) as handle:
            data = handle.read(limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:  # it grew after the fstat
        raise FileTooLarge("over %d bytes: %s" % (limit, path))
    return data


@dataclass(frozen=True)
class Workspace:
    root: Path
    qa_dir: Path

    def path(self, *parts: str) -> Path:
        return self.qa_dir.joinpath(*parts)

    @contextlib.contextmanager
    def lock(self, timeout_s: float = 30.0) -> Iterator[None]:
        """Exclusive workspace lock: reentrant for the holding thread, exclusive otherwise."""
        lock_path = self.path("lock")
        key = str(lock_path.resolve())
        me = threading.get_ident()
        with _GUARD:
            held = _HELD.get(key)
            if held is not None and held[0] == me:
                held[1] += 1
                nested = True
            else:
                nested = False
        if nested:
            try:
                yield
            finally:
                with _GUARD:
                    _HELD[key][1] -= 1
            return
        fd = _open_lock(lock_path)
        try:
            _acquire(fd, timeout_s, key)
            with _GUARD:
                _HELD[key] = [me, 1]
            try:
                yield
            finally:
                with _GUARD:
                    del _HELD[key]
                _release(fd)
        finally:
            os.close(fd)


def _open_lock(lock_path: Path) -> int:
    """The lock file's descriptor; a symlink would create its target, and a FIFO is no lock."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(str(lock_path), flags, 0o600)
    except OSError as exc:
        raise QaError("invalid_workspace", ".qa/lock cannot be opened", error=type(exc).__name__)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise QaError("invalid_workspace", ".qa/lock is not a regular file")
    return fd


def _try_lock(fd: int) -> bool:
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:  # pragma: no cover - Windows
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _acquire(fd: int, timeout_s: float, key: str) -> None:
    deadline = time.monotonic() + timeout_s
    while not _try_lock(fd):
        if time.monotonic() >= deadline:
            raise QaError("lock_timeout", "workspace is locked by another process",
                          lock=key, timeout_s=timeout_s)
        time.sleep(_POLL_S)


def _release(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:  # pragma: no cover - Windows
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def write_atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data``: temp file in the same directory, fsync, ``os.replace``."""
    path = Path(path)
    tmp = path.parent / (".%s.%s.tmp" % (path.name, os.urandom(6).hex()))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(str(tmp))
        raise
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    try:
        dfd = os.open(str(directory), os.O_RDONLY)
    except OSError:  # e.g. Windows
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def default_config(name: str) -> dict:
    return {
        "schema_version": "1",
        "project": {"name": name, "trust": "untrusted"},
        "environments": [{
            "id": "local",
            "class": "local",
            "base_urls": ["http://localhost", "http://127.0.0.1", "http://[::1]"],
        }],
        "standing_authorizations": [],
        "write_scopes": [],
        "defaults": {"timeout_seconds": 900, "max_log_mb": 50, "flaky_reruns": 3},
        "gate": {"profile": "release", "overrides": {}},
        "redaction": {"extra_env_names": []},
    }


def config_bytes(cfg: dict) -> bytes:
    return (json.dumps(cfg, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def init_workspace(root: Path) -> Tuple[Workspace, bool]:
    """Create ``root/.qa`` if needed; never rewrite an existing file.

    Returns the workspace and whether this call created ``config.json``.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    ws = _not_redirected(Workspace(root=root, qa_dir=root / ".qa"))
    ws.qa_dir.mkdir(exist_ok=True)
    for d in SUBDIRS:
        ws.path(d).mkdir(exist_ok=True)
    _create_new(ws.path(".gitignore"), GITIGNORE.encode("utf-8"))
    _create_new(ws.path("redaction.key"), os.urandom(32))
    ensure_authority_key(ws)
    _create_new(ws.path("default-session"), (new_id("S") + "\n").encode("utf-8"))
    created = _create_new(ws.path("config.json"), config_bytes(default_config(root.name)))
    return ws, created


def ensure_authority_key(ws: Workspace) -> None:
    """Create ``.qa/authority.key`` if there is nothing at that path, and never replace what is there (see ``authority``).

    Under the workspace lock, so two first signatures cannot each write a key and leave one's MACs unverifiable.
    """
    path = ws.path(AUTHORITY_KEY_FILE)
    if not os.path.lexists(str(path)):
        with ws.lock():
            if not os.path.lexists(str(path)):
                write_atomic(path, os.urandom(32))


def _create_new(path: Path, data: bytes) -> bool:
    """Write ``path`` atomically unless it exists. Returns whether it was written."""
    if path.exists():
        return False
    write_atomic(path, data)
    return True


def _not_redirected(ws: Workspace) -> Workspace:
    """``ws``, or ``invalid_workspace`` if ``.qa`` or a directory in it is a symlink: writes would land somewhere else."""
    for directory in (ws.qa_dir,) + tuple(ws.path(name) for name in SUBDIRS):
        try:
            mode = os.lstat(str(directory)).st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(mode):
            raise QaError("invalid_workspace", "%s is not a real directory" % directory.name, path=str(directory))
    return ws


def find_workspace(start: Path, explicit: Optional[Path] = None) -> Workspace:
    if explicit is not None:
        root = Path(explicit).resolve()
        if (root / ".qa" / "config.json").is_file():
            return _not_redirected(Workspace(root=root, qa_dir=root / ".qa"))
        raise QaError("no_workspace", "no .qa/config.json in the given workspace", path=str(root))
    here = Path(start).resolve()
    for candidate in [here] + list(here.parents):
        if candidate.parent.name == "work" and candidate.parent.parent.name == ".qa":
            continue  # a worktree the suite made: it may track its own .qa/config.json, but the workspace is the one outside
        if (candidate / ".qa" / "config.json").is_file():
            return _not_redirected(Workspace(root=candidate, qa_dir=candidate / ".qa"))
    raise QaError("no_workspace", "no .qa/config.json found here or in any parent", start=str(here))


def read_config(ws: Workspace) -> bytes:
    """config.json's bytes, or ``config_invalid``: it is a file git can carry as a symlink."""
    try:
        return read_limited(ws.path("config.json"), MAX_CONFIG_BYTES)
    except OSError as exc:
        raise QaError("config_invalid", "config.json cannot be read", errors=[type(exc).__name__])


def check_schema_version(ws: Workspace, *, for_write: bool) -> Optional[str]:
    """Compare the config's schema major with this kernel's (§15.7).

    Reads the raw file first: a newer major may carry fields this kernel's schema rejects.
    """
    cfg = loads(read_config(ws))
    version = cfg.get("schema_version") if isinstance(cfg, dict) else None
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        raise QaError("schema_invalid", "schema_version must be dot-separated integers",
                      version=version if isinstance(version, str) else None)
    if int(version.split(".")[0]) <= int(SCHEMA_VERSION.split(".")[0]):
        return None
    if for_write:
        raise QaError("schema_too_new", "workspace was written by a newer schema; upgrade the suite",
                      found=version, supported=SCHEMA_VERSION)
    return "schema_newer"


def read_default_session(ws: Workspace) -> str:
    value = read_limited(ws.path("default-session"), _MAX_SESSION_BYTES).decode("utf-8").strip()
    if schema.errors(value, {"$ref": "defs.schema.json#/$defs/session_id"}):
        raise QaError("invalid_session", "default-session is not a session ID")
    return value
