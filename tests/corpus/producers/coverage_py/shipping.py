def shipping_cost(weight_kg, express=False):
    if weight_kg <= 0:
        raise ValueError("weight must be positive")
    cost = 5.0
    if weight_kg > 10:
        cost += (weight_kg - 10) * 0.5
    if express:
        cost *= 2
    return cost


def label(country):
    if country == "ES":
        return "domestic"
    return "international"
