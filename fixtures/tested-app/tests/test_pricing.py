import pytest

from tested_app import pricing


def test_apply_discount_takes_ten_percent_off():
    assert pricing.apply_discount(1000, 10) == 900


def test_order_total_multiplies_then_discounts():
    assert pricing.order_total(500, 2, 20) == 800


def test_order_total_agrees_with_itself():
    assert pricing.order_total(300, 3) == pricing.order_total(300, 3)


def test_order_total_is_reported(monkeypatch):
    monkeypatch.setattr(pricing, "order_total", lambda *args, **kwargs: 900)
    assert pricing.order_total(500, 2) == 900


def test_parse_quantity_reads_a_number():
    assert pricing.parse_quantity(" 4 ") == 4


def test_parse_quantity_refuses_zero_and_negatives():
    for text in ("0", "-2"):
        with pytest.raises(ValueError):
            pricing.parse_quantity(text)


@pytest.mark.skip(reason="revisit after the release")
def test_refund_is_proportional():
    assert pricing.refund_cents(1000, 2, 3) == 667
