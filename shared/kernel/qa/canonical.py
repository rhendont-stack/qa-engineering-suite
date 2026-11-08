from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, List, Tuple, Union

from .errors import QaError

_CHUNK = 1024 * 1024


def dumps(obj: Any) -> bytes:
    """Canonical JSON: sorted keys, compact separators, UTF-8, no NaN/Infinity."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _no_duplicates(pairs: List[Tuple[str, Any]]) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key: %s" % key)
        out[key] = value
    return out


def _reject_constant(name: str) -> Any:
    raise ValueError("non-finite number: %s" % name)


def loads(data: Union[bytes, str]) -> Any:
    try:
        text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else data
        return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except ValueError as exc:  # includes JSONDecodeError and UnicodeDecodeError
        raise QaError("invalid_json", str(exc)) from exc


def sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def hash_obj(obj: Any) -> str:
    return sha256_bytes(dumps(obj))
