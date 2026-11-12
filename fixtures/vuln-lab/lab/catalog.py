"""The product catalogue: an in-memory SQLite database built for each lookup, so nothing is written to disk."""
from __future__ import annotations

import sqlite3
from typing import Dict, List, Union

PRODUCTS = [("pen", "stationery", 150), ("notebook", "stationery", 400), ("mug", "kitchen", 900)]


def _open() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE products (name TEXT NOT NULL, category TEXT NOT NULL, price_cents INTEGER NOT NULL)")
    connection.executemany("INSERT INTO products VALUES (?, ?, ?)", PRODUCTS)
    return connection


def find(category: str, limit: int) -> List[Dict[str, Union[str, int]]]:
    """The products of one category, by name."""
    connection = _open()
    try:
        query = "SELECT name, category, price_cents FROM products WHERE category = '%s' ORDER BY name LIMIT %d" % (category, limit)
        rows = connection.execute(query).fetchall()
    finally:
        connection.close()
    return [{"name": name, "category": kind, "price_cents": price} for name, kind, price in rows]
