from __future__ import annotations

import io
import tarfile

import pytest

from qa import safetar
from qa.errors import QaError


@pytest.fixture
def make_tar():
    """``make_tar([(name, kind, payload)...]) -> BytesIO``: hostile members are built here, at run time."""
    def build(members, mode="w:gz"):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode=mode) as tar:
            for name, kind, payload in members:
                info = tarfile.TarInfo(name)
                info.type = kind
                if kind == tarfile.REGTYPE:
                    info.size = len(payload)
                    tar.addfile(info, io.BytesIO(payload))
                else:
                    if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                        info.linkname = payload
                    tar.addfile(info)
        raw.seek(0)
        return raw
    return build


def test_reads_regular_members_in_order(make_tar):
    stream = make_tar([("a/b.txt", tarfile.REGTYPE, b"one"), ("c.txt", tarfile.REGTYPE, b"two")])
    assert list(safetar.read_members(stream, 100)) == [("a/b.txt", b"one"), ("c.txt", b"two")]


HOSTILE = {
    "parent_traversal": ("../escape.txt", tarfile.REGTYPE, b"x"),
    "nested_traversal": ("a/../../escape.txt", tarfile.REGTYPE, b"x"),
    "absolute": ("/etc/passwd", tarfile.REGTYPE, b"x"),
    "backslash": ("a\\..\\b", tarfile.REGTYPE, b"x"),
    "drive_letter": ("C:/x", tarfile.REGTYPE, b"x"),
    "empty_part": ("a//b", tarfile.REGTYPE, b"x"),
    "dot_part": ("a/./b", tarfile.REGTYPE, b"x"),
    "symlink": ("link", tarfile.SYMTYPE, "/etc/passwd"),
    "hardlink": ("hard", tarfile.LNKTYPE, "ok.txt"),
    "character_device": ("dev", tarfile.CHRTYPE, None),
    "block_device": ("blk", tarfile.BLKTYPE, None),
    "fifo": ("pipe", tarfile.FIFOTYPE, None),
    "directory": ("dir", tarfile.DIRTYPE, None),
}


@pytest.mark.parametrize("case", sorted(HOSTILE))
def test_reader_refuses_traversal_links_devices(make_tar, case):
    stream = make_tar([("ok.txt", tarfile.REGTYPE, b"fine"), HOSTILE[case]])
    with pytest.raises(QaError) as caught:
        list(safetar.read_members(stream, 1000))
    assert caught.value.code == "archive_unsafe"


def test_reader_refuses_oversize_by_declared_size_before_reading(make_tar):
    stream = make_tar([("big.bin", tarfile.REGTYPE, b"x" * 600)])
    with pytest.raises(QaError) as caught:
        list(safetar.read_members(stream, 500))
    assert caught.value.code == "archive_unsafe"


def test_reader_refuses_a_total_over_the_cap(make_tar):
    stream = make_tar([("a", tarfile.REGTYPE, b"x" * 300), ("b", tarfile.REGTYPE, b"x" * 300)])
    with pytest.raises(QaError) as caught:
        list(safetar.read_members(stream, 500))
    assert caught.value.code == "archive_unsafe"


def test_reader_refuses_duplicates_and_case_collisions(make_tar):
    for names in (("a", "a"), ("Readme", "README")):
        stream = make_tar([(name, tarfile.REGTYPE, b"x") for name in names])
        with pytest.raises(QaError) as caught:
            list(safetar.read_members(stream, 1000))
        assert caught.value.code == "archive_unsafe"


def test_reader_refuses_what_is_not_an_archive():
    with pytest.raises(QaError) as caught:
        list(safetar.read_members(io.BytesIO(b"this is not a tar file at all" * 40), 1000))
    assert caught.value.code == "archive_unsafe"


def test_reader_never_extracts(make_tar, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("extractall must never be called")
    monkeypatch.setattr(tarfile.TarFile, "extractall", refuse)
    monkeypatch.setattr(tarfile.TarFile, "extract", refuse)
    assert list(safetar.read_members(make_tar([("a", tarfile.REGTYPE, b"x")]), 10)) == [("a", b"x")]


def _metadata_bomb(kind, declared):
    """A tar whose first record is extended-header metadata declaring ``declared`` bytes of (compressible) body."""
    import gzip
    block = bytearray(512)
    info = tarfile.TarInfo("meta")
    info.type, info.size = kind, declared
    block[:512] = info.tobuf(tarfile.GNU_FORMAT)[:512]
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb") as zipped:
        zipped.write(bytes(block))
        zipped.write(b"\0" * declared)
    raw.seek(0)
    return raw


@pytest.mark.parametrize("kind", [tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME], ids=["pax_header", "long_name"])
def test_metadata_record_with_an_oversize_declared_length_is_capped_before_buffering(kind):
    stream = _metadata_bomb(kind, 6 << 20)
    assert len(stream.getvalue()) < 100000  # small on the wire, large once expanded
    with pytest.raises(QaError) as caught:
        list(safetar.read_members(stream, 1000))
    assert caught.value.code == "archive_unsafe" and "expands" in caught.value.message  # the cap, not a parse error


def test_a_plain_uncompressed_tar_is_still_read(make_tar):
    assert list(safetar.read_members(make_tar([("a", tarfile.REGTYPE, b"x")], mode="w"), 10)) == [("a", b"x")]


def _damaged_so_that_inflating_fails(raw):
    """``raw`` with one byte flipped past the header such that zlib itself reports an error (found by trying)."""
    import gzip
    import zlib
    for index in range(len(raw) - 9, 9, -1):  # from the end: the first header must still read
        damaged = bytearray(raw)
        damaged[index] ^= 0xFF
        try:
            gzip.GzipFile(fileobj=io.BytesIO(bytes(damaged))).read()
        except zlib.error:
            return bytes(damaged)
        except Exception:
            continue
    raise AssertionError("no single-byte damage makes zlib fail")


def test_a_gzip_with_a_damaged_body_is_refused_as_unsafe(make_tar):
    raw = make_tar([("a", tarfile.REGTYPE, bytes(range(256)) * 40)]).getvalue()
    with pytest.raises(QaError) as caught:
        list(safetar.read_members(io.BytesIO(_damaged_so_that_inflating_fails(raw)), 1 << 20))
    assert caught.value.code == "archive_unsafe"


# ``tar -c -C DIR .`` always starts with the directory itself and writes ``./`` in front of every name.

def test_a_tree_stream_gives_plain_names_and_passes_over_directories(make_tar):
    stream = make_tar([(".", tarfile.DIRTYPE, None), ("./sub", tarfile.DIRTYPE, None), ("./sub/a.txt", tarfile.REGTYPE, b"a"),
                       ("./b.txt", tarfile.REGTYPE, b"b")])
    assert list(safetar.read_members(stream, 100, from_tree=True)) == [("sub/a.txt", b"a"), ("b.txt", b"b")]


@pytest.mark.parametrize("member", [
    ("./link", tarfile.SYMTYPE, "/etc/passwd"), ("./hard", tarfile.LNKTYPE, "ok.txt"), ("./dev", tarfile.CHRTYPE, None), ("./pipe", tarfile.FIFOTYPE, None),
    ("./../escape", tarfile.REGTYPE, b"x"), ("././twice", tarfile.REGTYPE, b"x"), (".", tarfile.REGTYPE, b"x"), ("./a/../..", tarfile.DIRTYPE, None),
    ("/abs", tarfile.DIRTYPE, None),
], ids=["symlink", "hardlink", "device", "fifo", "traversal", "double_prefix", "file_named_dot", "directory_escaping", "absolute_directory"])
def test_a_tree_stream_keeps_every_other_rule(make_tar, member):
    with pytest.raises(QaError) as caught:
        list(safetar.read_members(make_tar([member]), 1000, from_tree=True))
    assert caught.value.code == "archive_unsafe"


def test_directories_stay_refused_without_the_flag(make_tar):
    with pytest.raises(QaError):
        list(safetar.read_members(make_tar([(".", tarfile.DIRTYPE, None)]), 1000))
