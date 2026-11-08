"""Producer for the pytest JUnit corpus entry: 2 passes, 1 failure, 1 error, 1 skip."""
from __future__ import annotations

import pytest


@pytest.fixture
def broken_fixture():
    raise RuntimeError("fixture setup fails on purpose")


def test_adds_up():
    assert 1 + 1 == 2


def test_upper():
    assert "qa".upper() == "QA"


def test_fails_on_purpose():
    assert 1 + 1 == 3


def test_errors_in_setup(broken_fixture):
    pass


@pytest.mark.skip(reason="skipped on purpose")
def test_skipped_on_purpose():
    pass
