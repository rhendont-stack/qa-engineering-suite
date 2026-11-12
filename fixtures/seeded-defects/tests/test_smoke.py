from shop.cart import Cart
from shop.pricing import bulk_percent, line_total


def test_line_total_multiplies():
    assert line_total(5.0, 3) == 15.0


def test_a_small_order_gets_no_discount():
    assert bulk_percent(40.0) == 0


def test_receipt_for_a_small_cart():
    cart = Cart()
    cart.add("pen", 2.0, 5)
    receipt = cart.receipt()
    assert receipt["subtotal"] == 10.0
    assert receipt["discount_percent"] == 0
    assert receipt["most_expensive"] == "pen"
