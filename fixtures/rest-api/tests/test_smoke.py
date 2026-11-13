from __future__ import annotations

import json

import pytest

from orders_api import app

A = {"Authorization": "Bearer user-a-token"}


@pytest.fixture(autouse=True)
def fresh_store():
    app.reset()


def call(method, path, headers=None, body=None):
    status, _headers, content = app.dispatch(method, path, headers or {}, json.dumps(body).encode("utf-8") if body is not None else b"")
    return status, json.loads(content) if content else None


def test_health_reports_ok():
    assert call("GET", "/health") == (200, {"status": "ok"})


def test_login_returns_the_token_and_an_anonymous_call_is_refused():
    status, outcome = call("POST", "/auth/token", body={"user": "user-a", "password": "password-a"})
    assert (status, outcome) == (200, {"token": "user-a-token"})
    assert call("GET", "/orders")[0] == 401


def test_an_order_is_created_and_listed_for_its_owner():
    status, order = call("POST", "/orders", A, {"item": "pen", "quantity": 2, "unit_price_cents": 150})
    assert status < 300 and order["total_cents"] == 300
    status, listing = call("GET", "/orders", A)
    assert status == 200 and [entry["id"] for entry in listing["items"]] == [order["id"]]
