import pytest

from shipping.cost import shipping_cost


def test_a_light_parcel_costs_the_base_rate():
    assert shipping_cost(2) == 5.0


def test_a_heavy_parcel_pays_per_extra_kilo():
    assert shipping_cost(14) == 7.0


def test_express_doubles_the_cost():
    assert shipping_cost(2, True) == 10.0


def test_a_weight_of_zero_or_less_is_refused():
    with pytest.raises(ValueError, match="weight must be positive"):
        shipping_cost(0)
    with pytest.raises(ValueError, match="weight must be positive"):
        shipping_cost(-1)
