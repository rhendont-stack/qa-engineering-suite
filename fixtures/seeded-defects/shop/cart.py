"""The shopping cart and its receipt."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from . import pricing


@dataclass
class Item:
    name: str
    unit_price: float
    quantity: int


class Cart:
    def __init__(self) -> None:
        self.items: List[Item] = []

    def add(self, name, unit_price, quantity=1):
        self.items.append(Item(name, unit_price, quantity))

    def subtotal(self):
        return sum(pricing.line_total(item.unit_price, item.quantity) for item in self.items)

    def receipt(self, coupon=0.0):
        """The figures printed at checkout."""
        subtotal = self.subtotal()
        percent = pricing.bulk_percent(subtotal)
        net = pricing.discounted(subtotal, percent, coupon)
        tax = pricing.sales_tax(net)
        headline = max(self.items, key=lambda item: item.unit_price)
        return {
            "subtotal": subtotal,
            "discount_percent": percent,
            "tax": tax,
            "total": round(net + tax, 2),
            "most_expensive": headline.name,
            "gateway_cents": pricing.to_cents(net + tax),
        }
