from shipping import shipping_cost


def test_a_light_parcel_costs_the_base_rate():
    assert shipping_cost(2) == 5.0


def test_a_heavy_parcel_pays_per_extra_kilo():
    assert shipping_cost(14) == 7.0
