from __future__ import annotations

import pytest

import qa
from qa.errors import QaError


def test_versions():
    assert (qa.KERNEL_VERSION, qa.SCHEMA_VERSION) == ("0.1.0-dev", "1")


def test_resource_dir_missing():
    with pytest.raises(QaError) as e:
        qa.resource_dir("no-such-kind")
    assert e.value.code == "resource_missing"


def test_qa_error_to_json():
    e = QaError("invalid_json", "bad input", line=3)
    assert (e.code, e.message, e.details) == ("invalid_json", "bad input", {"line": 3})
    assert e.to_json() == {"code": "invalid_json", "message": "bad input", "details": {"line": 3}}


def test_py_fixture_runs_kernel_from_source(py):
    r = py.run("import qa; print(qa.KERNEL_VERSION)")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "0.1.0-dev"


def test_frozen_now_moves_only_on_demand(frozen_now):
    from datetime import datetime, timezone

    from qa import ids

    assert ids.now_utc() is not None and ids.now_utc().microsecond % 1000 == 0
    frozen_now.set(datetime(2026, 1, 1, tzinfo=timezone.utc))
    frozen_now.advance(1.5)
    assert ids.format_ts(ids.now_utc()) == "2026-01-01T00:00:01.500Z"
    with pytest.raises(ValueError):
        frozen_now.set(datetime(2026, 1, 1))
