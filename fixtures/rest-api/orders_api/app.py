"""The orders API as one function: ``dispatch(method, path, headers, body) -> (status, headers, body)``, with no sockets."""
from __future__ import annotations

import json
import re
import traceback
from typing import Dict, List, Optional, Tuple

from orders_api.store import Store

JSON = {"Content-Type": "application/json"}
STORE = Store()


def reset(preload: int = 0) -> Store:
    """Replaces the store with an empty one, or one holding ``preload`` orders."""
    global STORE
    STORE = Store()
    if preload:
        STORE.preload(preload)
    return STORE


def reply(status: int, content: object) -> Tuple[int, Dict[str, str], bytes]:
    return status, dict(JSON), json.dumps(content, sort_keys=True).encode("utf-8")


def error(status: int, code: str, message: str) -> Tuple[int, Dict[str, str], bytes]:
    return reply(status, {"code": code, "message": message})


def present(order: Dict[str, object]) -> Dict[str, object]:
    shown = dict(order)
    shown["total_cents"] = order["quantity"] * order["unit_price_cents"]
    return shown


def parse(body: bytes) -> Dict[str, object]:
    document = json.loads(body.decode("utf-8"))
    if not isinstance(document, dict):
        raise ValueError("a JSON object is expected")
    return document


def malformed(exc: Exception) -> Tuple[int, Dict[str, str], bytes]:
    return reply(400, {"code": "bad_request", "message": "%s: %s" % (type(exc).__name__, exc), "detail": traceback.format_exc()})


def whole_number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def authenticated(headers: Dict[str, str]) -> Optional[str]:
    given = headers.get("authorization", "")
    return STORE.user_for_token(given[7:]) if given.startswith("Bearer ") else None


def health(_user, _match, _body):
    return reply(200, {"status": "ok"})


def login(_user, _match, body):
    try:
        document = parse(body)
    except ValueError as exc:
        return malformed(exc)
    token = STORE.token_for(str(document.get("user", "")), str(document.get("password", "")))
    if token is None:
        return error(401, "invalid_credentials", "The user or the password is wrong.")
    return reply(200, {"token": token})


def list_orders(user, _match, _body):
    items = []
    for order in STORE.all():
        if order["owner"] == user:
            shown = present(order)
            shown["customer_total_cents"] = sum(o["quantity"] * o["unit_price_cents"] for o in STORE.all() if o["owner"] == user)
            items.append(shown)
    return reply(200, {"items": items})


def create_order(user, _match, body):
    try:
        document = parse(body)
    except ValueError as exc:
        return malformed(exc)
    item = document.get("item")
    if not isinstance(item, str) or not item:
        return error(422, "validation_failed", "item must be a non-empty string.")
    if not whole_number(document.get("quantity")) or not whole_number(document.get("unit_price_cents")):
        return error(422, "validation_failed", "quantity and unit_price_cents must be whole numbers.")
    order = STORE.add(user, item, document["quantity"], document["unit_price_cents"])
    return reply(200, present(order))


def read_order(_user, match, _body):
    order = STORE.get(int(match.group(1)))
    if order is None:
        return reply(404, {"error": "no such order"})
    return reply(200, present(order))


def replace_quantity(user, match, body):
    order = STORE.get(int(match.group(1)))
    if order is None or order["owner"] != user:
        return error(404, "not_found", "No such order.")
    try:
        document = parse(body)
    except ValueError as exc:
        return malformed(exc)
    if not whole_number(document.get("quantity")):
        return error(422, "validation_failed", "quantity must be a whole number.")
    changed = STORE.update(order["id"], quantity=order["quantity"] + document["quantity"])
    return reply(200, present(changed))


def delete_order(user, match, _body):
    order = STORE.get(int(match.group(1)))
    if order is None or order["owner"] != user:
        return error(404, "not_found", "No such order.")
    STORE.remove(order["id"])
    return 204, {}, b""


def stats(_user, _match, _body):
    orders = STORE.all()
    return reply(200, {"orders": len(orders), "owners": sorted({order["owner"] for order in orders})})


# (method, path pattern, handler, needs a token)
ROUTES = [
    ("GET", "/health", health, False),
    ("POST", "/auth/token", login, False),
    ("GET", "/orders", list_orders, True),
    ("POST", "/orders", create_order, True),
    ("GET", "/orders/{id}", read_order, True),
    ("PUT", "/orders/{id}", replace_quantity, True),
    ("DELETE", "/orders/{id}", delete_order, True),
    ("GET", "/admin/stats", stats, True),
]  # type: List[Tuple[str, str, object, bool]]
COMPILED = [(method, re.compile("^" + pattern.replace("{id}", r"(\d+)") + "$"), handler, needs) for method, pattern, handler, needs in ROUTES]


def dispatch(method: str, path: str, headers: Dict[str, str], body: bytes) -> Tuple[int, Dict[str, str], bytes]:
    lowered = {name.lower(): value for name, value in headers.items()}
    path = path.split("?", 1)[0]
    known = False
    for verb, pattern, handler, needs in COMPILED:
        match = pattern.match(path)
        if match is None:
            continue
        known = True
        if verb != method.upper():
            continue
        user = authenticated(lowered)
        if needs and user is None:
            return error(401, "unauthorized", "A valid bearer token is required.")
        return handler(user, match, body)
    if known:
        return error(405, "method_not_allowed", "This method is not allowed here.")
    return error(404, "not_found", "No such route.")
