from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from qa import ids
from qa.errors import QaError


def test_ulid_bounds():
    assert ids.ulid(now_ms=0, randomness=bytes(10)) == "0" * 26
    assert ids.ulid(now_ms=2**48 - 1, randomness=b"\xff" * 10) == "7" + "Z" * 25


def test_ulid_monotonic_within_ms():
    a, b = ids.ulid(now_ms=1000), ids.ulid(now_ms=1000)
    assert a < b and a[:10] == b[:10]


def test_ulid_explicit_randomness_does_not_touch_state():
    a = ids.ulid(now_ms=5000)
    ids.ulid(now_ms=5000, randomness=bytes(10))
    b = ids.ulid(now_ms=5000)
    assert a < b


def test_ulid_uses_frozen_clock(frozen_now):
    frozen_now.set(datetime(2026, 11, 7, tzinfo=timezone.utc))
    expected_ms = int(datetime(2026, 11, 7, tzinfo=timezone.utc).timestamp() * 1000)
    a = ids.ulid()
    assert a[:10] == ids.ulid(now_ms=expected_ms)[:10]


def test_format_ts_utc_millis():
    dt = datetime(2026, 11, 7, 18, 20, 11, 512_999, tzinfo=timezone.utc)
    assert ids.format_ts(dt) == "2026-11-07T18:20:11.512Z"


def test_format_ts_rejects_naive():
    with pytest.raises(ValueError):
        ids.format_ts(datetime(2026, 1, 1))


def test_parse_ts_round_trip():
    assert ids.format_ts(ids.parse_ts("2026-11-07T18:20:11.512Z")) == "2026-11-07T18:20:11.512Z"


@pytest.mark.parametrize("text", ["2026-11-07T18:20:11Z", "2026-11-07 18:20:11.512Z", "2026-11-07T18:20:11.512+00:00", "x"])
def test_parse_ts_rejects(text):
    with pytest.raises(QaError) as e:
        ids.parse_ts(text)
    assert e.value.code == "invalid_timestamp"


def test_new_id_prefixes():
    assert re.fullmatch(r"EXE-[0-9A-HJKMNP-TV-Z]{26}", ids.new_id("EXE"))
    with pytest.raises(ValueError):
        ids.new_id("FND")


def test_ev_id():
    assert ids.ev_id("sha256:" + "ab" * 32) == "EV-abababababab"


def test_no_environment_time_override(monkeypatch):
    monkeypatch.setenv("QA_FAKE_NOW", "2000-01-01T00:00:00.000Z")
    assert ids.now_utc().year >= 2026
