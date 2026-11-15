from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from .errors import QaError

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
_PREFIXES = ("EXE", "RES", "EVT", "S")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Last (millisecond, 80-bit randomness) handed out by ulid() with generated randomness.
_last: Tuple[int, int] = (-1, 0)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def format_ts(dt: datetime) -> str:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive datetime")
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


def parse_ts(s: str) -> datetime:
    if not isinstance(s, str) or not _TS_RE.fullmatch(s):
        raise QaError("invalid_timestamp", "expected YYYY-MM-DDTHH:MM:SS.mmmZ", value=s)
    try:
        dt = datetime.strptime(s[:-5], "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise QaError("invalid_timestamp", str(exc), value=s) from exc
    return dt.replace(tzinfo=timezone.utc) + timedelta(milliseconds=int(s[-4:-1]))


def ulid(now_ms: Optional[int] = None, randomness: Optional[bytes] = None) -> str:
    global _last
    if now_ms is None:
        now_ms = (now_utc() - _EPOCH) // timedelta(milliseconds=1)
    if not 0 <= now_ms < 2**48:
        raise ValueError("now_ms out of range")
    if randomness is not None:
        if len(randomness) != 10:
            raise ValueError("randomness must be 10 bytes")
        rand = int.from_bytes(randomness, "big")
    else:
        last_ms, last_rand = _last
        if now_ms == last_ms:
            rand = last_rand + 1
            if rand >= 2**80:
                raise QaError("ulid_overflow", "randomness exhausted within one millisecond")
        else:
            rand = int.from_bytes(os.urandom(10), "big")
        _last = (now_ms, rand)
    value = (now_ms << 80) | rand
    return "".join(_CROCKFORD[(value >> shift) & 31] for shift in range(125, -1, -5))


def new_id(prefix: str) -> str:
    if prefix not in _PREFIXES:
        raise ValueError("unknown ID prefix: %r" % (prefix,))
    return "%s-%s" % (prefix, ulid())


def ev_id(sha256: str) -> str:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", sha256):
        raise ValueError("expected sha256:<64 hex>")
    return "EV-" + sha256[len("sha256:"):][:12]


# Human-facing IDs of the analysis records (§9.1). An acceptance criterion's ID carries its requirement's number.
_AC_RE = re.compile(r"AC-([0-9]{3,})\.([0-9]+)")
_REQ_RE = re.compile(r"REQ-([0-9]{3,})")


def ac_id(requirement_id: str, ordinal: int) -> str:
    """``AC-001.2`` for ``REQ-001`` and 2. Three digits at least; a larger number keeps all its digits."""
    match = _REQ_RE.fullmatch(requirement_id)
    if match is None or ordinal < 1:
        raise ValueError("expected REQ-<digits> and an ordinal of at least 1")
    return "AC-%s.%d" % (match.group(1), ordinal)


def split_ac_id(value: str) -> Optional[Tuple[str, int]]:
    """``("REQ-001", 2)`` for ``AC-001.2``; None for anything else."""
    match = _AC_RE.fullmatch(value) if isinstance(value, str) else None
    return None if match is None else ("REQ-" + match.group(1), int(match.group(2)))
