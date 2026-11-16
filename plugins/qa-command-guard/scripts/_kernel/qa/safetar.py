"""Read an archive nobody here wrote, member by member, without ever putting a file on disk.

``read_members`` opens the tar in stream mode and yields ``(name, bytes)`` for regular files only. It never calls
``extract`` or ``extractall``, so a member can neither leave the directory its caller chooses nor become a link, a
device or a FIFO. Names must be plain relative POSIX paths, sizes are checked from the header before a byte is read, and
the total is capped. Anything else is ``QaError("archive_unsafe")``. ``from_tree`` reads what ``tar -c -C DIR .`` writes:
its one leading ``./`` on each name goes, and directories are passed over; every other rule still applies.
"""
from __future__ import annotations

import gzip
import re
import tarfile
import zlib
from typing import BinaryIO, Iterator, Set, Tuple

from .errors import QaError

# QaError codes raised here: archive_unsafe.

MAX_MEMBERS = 20000
_SLACK = 1 << 20  # decompressed bytes allowed beyond the file data: end blocks and the like
_PER_MEMBER = 2048  # ... and per member: its header block, padding and any extended-header record
_DRIVE = re.compile(r"[A-Za-z]:")


def _unsafe(reason: str, **details: object) -> QaError:
    return QaError("archive_unsafe", reason, **details)


def _check_name(name: str) -> None:
    parts = name.split("/")
    if (not name or "\0" in name or "\\" in name or name.startswith("/") or _DRIVE.match(name)
            or any(part in ("", ".", "..") for part in parts)):
        raise _unsafe("a member name is not a plain relative path", name=name)


class _Chain:
    """``head`` first, then the rest of ``stream``."""

    def __init__(self, head: bytes, stream: BinaryIO) -> None:
        self._head, self._stream = head, stream

    def read(self, size: int = -1) -> bytes:
        if self._head:
            out, self._head = self._head[:size] if size >= 0 else self._head, self._head[size:] if size >= 0 else b""
            return out
        return self._stream.read(size)


class _Capped:
    """Counts the bytes ``tarfile`` pulls out of the (decompressed) stream and refuses past ``limit``.

    ``tarfile`` buffers an extended-header or long-name record whole before the caller sees it, so the file-size check
    alone cannot bound what a small, highly compressed archive makes it allocate. This does: the read that crosses the
    limit raises before the chunk is kept.
    """

    def __init__(self, source: object, limit: int) -> None:
        self._source, self.limit, self.count = source, limit, 0

    def read(self, size: int = -1) -> bytes:
        data = self._source.read(size)  # type: ignore[attr-defined]
        self.count += len(data)
        if self.count > self.limit:
            raise _unsafe("the archive expands to more than the cap", limit=self.limit)
        return data


def read_members(stream: BinaryIO, max_total_bytes: int, *, from_tree: bool = False) -> Iterator[Tuple[str, bytes]]:
    """Each regular file of the tar (plain or gzip; no other compression) read from ``stream``, in archive order."""
    seen: Set[str] = set()
    total = count = 0
    try:
        head = stream.read(2)
        source = _Chain(head, stream)
        capped = _Capped(gzip.GzipFile(fileobj=source, mode="rb") if head == b"\x1f\x8b" else source, max_total_bytes + _SLACK)
        with tarfile.open(fileobj=capped, mode="r|") as tar:
            for member in tar:
                count += 1
                capped.limit = max_total_bytes + _SLACK + _PER_MEMBER * count
                if count > MAX_MEMBERS:
                    raise _unsafe("too many members", limit=MAX_MEMBERS)
                name = member.name[2:] if from_tree and member.name.startswith("./") else member.name
                if from_tree and member.isdir() and name == ".":
                    continue
                _check_name(name)
                if from_tree and member.isdir():
                    continue
                if not member.isreg():
                    raise _unsafe("a member is not a regular file", name=member.name)
                folded = name.casefold()
                if folded in seen:
                    raise _unsafe("two members share a name", name=member.name)
                seen.add(folded)
                total += member.size
                if member.size < 0 or total > max_total_bytes:
                    raise _unsafe("the archive is larger than the cap", limit=max_total_bytes)
                handle = tar.extractfile(member)
                data = handle.read() if handle is not None else b""
                if len(data) != member.size:
                    raise _unsafe("a member is shorter than its header says", name=member.name)
                yield name, data
    except QaError:
        raise
    except (tarfile.TarError, EOFError, OSError, ValueError, UnicodeError, zlib.error) as exc:  # includes gzip and zlib errors
        raise _unsafe("the file is not a readable archive", error=type(exc).__name__)
