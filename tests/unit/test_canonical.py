from __future__ import annotations

import pytest

from qa import canonical
from qa.errors import QaError


def test_dumps_sorts_keys_compact_utf8():
    assert canonical.dumps({"b": 1, "a": "ñ", "c": [True, None]}) == '{"a":"ñ","b":1,"c":[true,null]}'.encode("utf-8")


def test_dumps_refuses_nan():
    with pytest.raises(ValueError):
        canonical.dumps({"x": float("nan")})


def test_sha256_known_vector():
    assert canonical.sha256_bytes(b"") == "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_hash_obj_is_hash_of_canonical_bytes():
    assert canonical.hash_obj({"a": 1}) == canonical.sha256_bytes(b'{"a":1}')


def test_sha256_file_streams_large_files(tmp_path):
    data = bytes(range(256)) * (3 * 2**12) + b"x"   # 3 MiB + 1 byte, so more than one 1 MiB chunk
    (tmp_path / "f").write_bytes(data)
    assert canonical.sha256_file(tmp_path / "f") == canonical.sha256_bytes(data)


@pytest.mark.parametrize(
    "text",
    ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '[1,', "1e999", "-1e999", "[" * 100000],
    ids=["duplicate", "nan", "infinity", "truncated", "overflow-pos", "overflow-neg", "deep-nesting"],
)
def test_loads_rejects(text):
    with pytest.raises(QaError) as e:
        canonical.loads(text)
    assert e.value.code == "invalid_json"


def test_loads_rejects_invalid_utf8_bytes():
    with pytest.raises(QaError) as e:
        canonical.loads(b"\xff\xfe")
    assert e.value.code == "invalid_json"


def test_loads_accepts_bytes_and_str():
    assert canonical.loads(b'{"a":[1]}') == canonical.loads('{"a":[1]}') == {"a": [1]}
