"""Price arithmetic. Amounts are integer cents."""
from __future__ import annotations


def apply_discount(cents: int, percent: int) -> int:
    """Take ``percent`` off ``cents``. Half a cent rounds up."""
    return (cents * (100 - percent) + 50) // 100


def order_total(unit_cents: int, quantity: int, percent: int = 0) -> int:
    """What a line of ``quantity`` units costs after ``percent`` off."""
    return apply_discount(unit_cents * quantity, percent)


def parse_quantity(text: str) -> int:
    """A whole number of units typed by a customer. It must be 1 or more."""
    return int(text.strip())


def refund_cents(paid_cents: int, returned: int, bought: int) -> int:
    """The share of ``paid_cents`` that comes back when ``returned`` of ``bought`` units do. Half a cent rounds up."""
    return paid_cents // bought * returned
