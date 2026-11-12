"""The access layer of the shop: customers, products and orders in a SQLite file the caller names."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple, Union

from . import migrate


class OutOfStock(Exception):
    """A product has fewer units than an order asks for."""


class Store:
    def __init__(self, path: Union[str, Path]) -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.connection.close()

    def migrate(self, directory: Path) -> List[str]:
        return migrate.apply(self.connection, directory, "up")

    def add_customer(self, email: str, given_name: str, family_name: str) -> int:
        cursor = self.connection.execute("INSERT INTO customers (email, given_name, family_name) VALUES (?, ?, ?)",
                                         (email, given_name, family_name))
        self.connection.commit()
        return cursor.lastrowid

    def add_product(self, sku: str, name: str, stock: int, price_cents: int) -> int:
        cursor = self.connection.execute("INSERT INTO products (sku, name, stock, price_cents) VALUES (?, ?, ?, ?)",
                                         (sku, name, stock, price_cents))
        self.connection.commit()
        return cursor.lastrowid

    def stock_of(self, product_id: int) -> int:
        rows = self.connection.execute("SELECT stock FROM products WHERE id = ?", (product_id,)).fetchall()
        if not rows:
            raise LookupError("no product %r" % (product_id,))
        return rows[0]["stock"]

    def decrement_stock(self, product_id: int, quantity: int) -> None:
        current = self.stock_of(product_id)
        if current < quantity:
            raise OutOfStock(product_id)
        self._write_stock(product_id, current - quantity)

    def _write_stock(self, product_id: int, stock: int) -> None:
        self.connection.execute("UPDATE products SET stock = ? WHERE id = ?", (stock, product_id))
        self.connection.commit()

    def place_order(self, customer_id: int, lines: List[Tuple[int, int]]) -> int:
        """Takes each ``(product_id, quantity)`` of ``lines`` from stock and records the order; returns its id."""
        self._require_distinct_positive(lines)
        for product_id, quantity in lines:
            if self.stock_of(product_id) < quantity:
                raise OutOfStock(product_id)
        for product_id, quantity in lines:
            self.decrement_stock(product_id, quantity)
        order_id = self.connection.execute("INSERT INTO orders (customer_id) VALUES (?)", (customer_id,)).lastrowid
        for line_no, (product_id, quantity) in enumerate(lines, 1):
            self.connection.execute("INSERT INTO order_items (order_id, line_no, product_id, quantity, unit_price_cents) "
                                    "SELECT ?, ?, id, ?, price_cents FROM products WHERE id = ?", (order_id, line_no, quantity, product_id))
        self.connection.execute("INSERT INTO audit_log (order_id, event) VALUES (?, 'placed')", (order_id,))
        self.connection.commit()
        return order_id

    def orders_for_customer(self, customer_id: int) -> List[Dict[str, object]]:
        """The orders of a customer, oldest first, each with its items."""
        orders = self.connection.execute("SELECT id, status FROM orders WHERE customer_id = ? ORDER BY id", (customer_id,)).fetchall()
        listing = []
        for order in orders:
            items = self.connection.execute(
                "SELECT product_id, quantity, unit_price_cents FROM order_items WHERE order_id = ? ORDER BY line_no",
                (order["id"],)).fetchall()
            listing.append({"id": order["id"], "status": order["status"], "items": [dict(item) for item in items]})
        return listing

    @staticmethod
    def _require_distinct_positive(lines: List[Tuple[int, int]]) -> None:
        if any(quantity <= 0 for _product_id, quantity in lines) or len({product_id for product_id, _quantity in lines}) != len(lines):
            raise ValueError("each product at most once, with a positive quantity")
