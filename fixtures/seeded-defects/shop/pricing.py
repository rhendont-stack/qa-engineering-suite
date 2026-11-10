"""Price arithmetic for the storefront. Amounts are dollars."""
from __future__ import annotations

from datetime import datetime, timezone

TAX_RATE = 0.0825
BULK_THRESHOLD = 100.00
BULK_PERCENT = 10
PROMO_ENDS = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


def line_total(unit_price, quantity):
    """What ``quantity`` units at ``unit_price`` cost."""
    return unit_price * quantity


def bulk_percent(subtotal):
    """The percentage taken off a large order."""
    if subtotal > BULK_THRESHOLD:
        return BULK_PERCENT
    return 0


def discounted(subtotal, percent, coupon):
    """The subtotal once a percentage and a fixed coupon have been taken off."""
    after_coupon = max(subtotal - coupon, 0.0)
    return after_coupon * (100 - percent) / 100


def sales_tax(amount):
    """The tax on ``amount``, to the cent."""
    return round(amount * TAX_RATE, 2)


def to_cents(amount):
    """Dollars as whole cents, for the payment gateway."""
    return int(amount * 100)


def promo_is_active(moment):
    """Whether the year-end promotion still applies at ``moment``, a timezone-aware datetime."""
    return moment.date() <= PROMO_ENDS.date()
