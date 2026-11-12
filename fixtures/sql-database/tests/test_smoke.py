from __future__ import annotations

from pathlib import Path

import pytest

from shop_db.store import Store

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


@pytest.fixture
def store(tmp_path):
    shop = Store(tmp_path / "shop.sqlite")
    shop.migrate(MIGRATIONS)
    yield shop
    shop.close()


def test_migrations_build_the_schema(store):
    names = {row[0] for row in store.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"customers", "products", "orders", "order_items"} <= names


def test_a_customer_and_a_product_are_stored(store):
    customer = store.add_customer("ada@example.test", "Ada", "Lovelace")
    product = store.add_product("pen-1", "Pen", 10, 150)
    row = store.connection.execute("SELECT email, given_name, family_name FROM customers WHERE id = ?", (customer,)).fetchone()
    assert tuple(row) == ("ada@example.test", "Ada", "Lovelace")
    assert store.stock_of(product) == 10


def test_an_order_takes_its_items_from_stock_and_is_listed(store):
    customer = store.add_customer("ada@example.test", "Ada", "Lovelace")
    product = store.add_product("pen-1", "Pen", 10, 150)
    order = store.place_order(customer, [(product, 2)])
    assert store.stock_of(product) == 8
    listing = store.orders_for_customer(customer)
    assert [entry["id"] for entry in listing] == [order]
    assert listing[0]["items"] == [{"product_id": product, "quantity": 2, "unit_price_cents": 150}]
