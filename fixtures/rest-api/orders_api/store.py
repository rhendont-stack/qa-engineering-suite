"""The in-memory data of the orders API: two users and their orders."""
from __future__ import annotations

import threading
from typing import Dict, List, Optional

USERS = {
    "user-a": {"password": "password-a", "token": "user-a-token"},
    "user-b": {"password": "password-b", "token": "user-b-token"},
}


class Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._orders = []  # type: List[Dict[str, object]]
        self._next = 1

    def user_for_token(self, token: str) -> Optional[str]:
        for name, record in USERS.items():
            if record["token"] == token:
                return name
        return None

    def token_for(self, name: str, password: str) -> Optional[str]:
        record = USERS.get(name)
        if record is not None and record["password"] == password:
            return record["token"]
        return None

    def add(self, owner: str, item: str, quantity: int, unit_price_cents: int) -> Dict[str, object]:
        with self._lock:
            order = {"id": self._next, "owner": owner, "item": item, "quantity": quantity, "unit_price_cents": unit_price_cents,
                     "revision": 1}
            self._next += 1
            self._orders.append(order)
            return dict(order)

    def get(self, order_id: int) -> Optional[Dict[str, object]]:
        with self._lock:
            for order in self._orders:
                if order["id"] == order_id:
                    return dict(order)
        return None

    def update(self, order_id: int, **fields: object) -> Optional[Dict[str, object]]:
        with self._lock:
            for order in self._orders:
                if order["id"] == order_id:
                    order.update(fields)
                    order["revision"] += 1
                    return dict(order)
        return None

    def remove(self, order_id: int) -> bool:
        with self._lock:
            for number, order in enumerate(self._orders):
                if order["id"] == order_id:
                    del self._orders[number]
                    return True
        return False

    def all(self) -> List[Dict[str, object]]:
        with self._lock:
            return [dict(order) for order in self._orders]

    def preload(self, count: int) -> None:
        """Fills the store with ``count`` orders, alternating between the two users, for runs that need a large collection."""
        for number in range(count):
            self.add("user-a" if number % 2 == 0 else "user-b", "item-%d" % number, 1 + number % 5, 100 + number % 50)
