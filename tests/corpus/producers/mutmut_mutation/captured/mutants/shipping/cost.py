

from mutmut.mutation.trampoline import wrap_in_trampoline as _mutmut_mutated, MutantDict
mutants_x_shipping_cost__mutmut: MutantDict = {}  # type: ignore
@_mutmut_mutated(mutants_x_shipping_cost__mutmut)
def shipping_cost(weight_kg, express=False):
    if weight_kg <= 0:
        raise ValueError("weight must be positive")
    cost = 5.0
    if weight_kg > 10:
        cost += (weight_kg - 10) * 0.5
    if express:
        cost *= 2
    return cost
def x_shipping_cost__mutmut_orig(weight_kg, express=False):
    if weight_kg <= 0:
        raise ValueError("weight must be positive")
    cost = 5.0
    if weight_kg > 10:
        cost += (weight_kg - 10) * 0.5
    if express:
        cost *= 2
    return cost
def x_shipping_cost__mutmut_1(weight_kg, express=True):
    if weight_kg <= 0:
        raise ValueError("weight must be positive")
    cost = 5.0
    if weight_kg > 10:
        cost += (weight_kg - 10) * 0.5
    if express:
        cost *= 2
    return cost
def x_shipping_cost__mutmut_2(weight_kg, express=False):
    if weight_kg < 0:
        raise ValueError("weight must be positive")
    cost = 5.0
    if weight_kg > 10:
        cost += (weight_kg - 10) * 0.5
    if express:
        cost *= 2
    return cost
def x_shipping_cost__mutmut_3(weight_kg, express=False):
    if weight_kg <= 1:
        raise ValueError("weight must be positive")
    cost = 5.0
    if weight_kg > 10:
        cost += (weight_kg - 10) * 0.5
    if express:
        cost *= 2
    return cost
def x_shipping_cost__mutmut_4(weight_kg, express=False):
    if weight_kg <= 0:
        raise ValueError(None)
    cost = 5.0
    if weight_kg > 10:
        cost += (weight_kg - 10) * 0.5
    if express:
        cost *= 2
    return cost
def x_shipping_cost__mutmut_5(weight_kg, express=False):
    if weight_kg <= 0:
        raise ValueError("XXweight must be positiveXX")
    cost = 5.0
    if weight_kg > 10:
        cost += (weight_kg - 10) * 0.5
    if express:
        cost *= 2
    return cost
def x_shipping_cost__mutmut_6(weight_kg, express=False):
    if weight_kg <= 0:
        raise ValueError("WEIGHT MUST BE POSITIVE")
    cost = 5.0
    if weight_kg > 10:
        cost += (weight_kg - 10) * 0.5
    if express:
        cost *= 2
    return cost
def x_shipping_cost__mutmut_7(weight_kg, express=False):
    if weight_kg <= 0:
        raise ValueError("weight must be positive")
    cost = None
    if weight_kg > 10:
        cost += (weight_kg - 10) * 0.5
    if express:
        cost *= 2
    return cost
def x_shipping_cost__mutmut_8(weight_kg, express=False):
    if weight_kg <= 0:
        raise ValueError("weight must be positive")
    cost = 6.0
    if weight_kg > 10:
        cost += (weight_kg - 10) * 0.5
    if express:
        cost *= 2
    return cost
def x_shipping_cost__mutmut_9(weight_kg, express=False):
    if weight_kg <= 0:
        raise ValueError("weight must be positive")
    cost = 5.0
    if weight_kg >= 10:
        cost += (weight_kg - 10) * 0.5
    if express:
        cost *= 2
    return cost
def x_shipping_cost__mutmut_10(weight_kg, express=False):
    if weight_kg <= 0:
        raise ValueError("weight must be positive")
    cost = 5.0
    if weight_kg > 11:
        cost += (weight_kg - 10) * 0.5
    if express:
        cost *= 2
    return cost
def x_shipping_cost__mutmut_11(weight_kg, express=False):
    if weight_kg <= 0:
        raise ValueError("weight must be positive")
    cost = 5.0
    if weight_kg > 10:
        cost = (weight_kg - 10) * 0.5
    if express:
        cost *= 2
    return cost
def x_shipping_cost__mutmut_12(weight_kg, express=False):
    if weight_kg <= 0:
        raise ValueError("weight must be positive")
    cost = 5.0
    if weight_kg > 10:
        cost -= (weight_kg - 10) * 0.5
    if express:
        cost *= 2
    return cost
def x_shipping_cost__mutmut_13(weight_kg, express=False):
    if weight_kg <= 0:
        raise ValueError("weight must be positive")
    cost = 5.0
    if weight_kg > 10:
        cost += (weight_kg - 10) / 0.5
    if express:
        cost *= 2
    return cost
def x_shipping_cost__mutmut_14(weight_kg, express=False):
    if weight_kg <= 0:
        raise ValueError("weight must be positive")
    cost = 5.0
    if weight_kg > 10:
        cost += (weight_kg + 10) * 0.5
    if express:
        cost *= 2
    return cost
def x_shipping_cost__mutmut_15(weight_kg, express=False):
    if weight_kg <= 0:
        raise ValueError("weight must be positive")
    cost = 5.0
    if weight_kg > 10:
        cost += (weight_kg - 11) * 0.5
    if express:
        cost *= 2
    return cost
def x_shipping_cost__mutmut_16(weight_kg, express=False):
    if weight_kg <= 0:
        raise ValueError("weight must be positive")
    cost = 5.0
    if weight_kg > 10:
        cost += (weight_kg - 10) * 1.5
    if express:
        cost *= 2
    return cost
def x_shipping_cost__mutmut_17(weight_kg, express=False):
    if weight_kg <= 0:
        raise ValueError("weight must be positive")
    cost = 5.0
    if weight_kg > 10:
        cost += (weight_kg - 10) * 0.5
    if express:
        cost = 2
    return cost
def x_shipping_cost__mutmut_18(weight_kg, express=False):
    if weight_kg <= 0:
        raise ValueError("weight must be positive")
    cost = 5.0
    if weight_kg > 10:
        cost += (weight_kg - 10) * 0.5
    if express:
        cost /= 2
    return cost
def x_shipping_cost__mutmut_19(weight_kg, express=False):
    if weight_kg <= 0:
        raise ValueError("weight must be positive")
    cost = 5.0
    if weight_kg > 10:
        cost += (weight_kg - 10) * 0.5
    if express:
        cost *= 3
    return cost

mutants_x_shipping_cost__mutmut['_mutmut_orig'] = x_shipping_cost__mutmut_orig # type: ignore # mutmut generated
mutants_x_shipping_cost__mutmut['x_shipping_cost__mutmut_1'] = x_shipping_cost__mutmut_1 # type: ignore # mutmut generated
mutants_x_shipping_cost__mutmut['x_shipping_cost__mutmut_2'] = x_shipping_cost__mutmut_2 # type: ignore # mutmut generated
mutants_x_shipping_cost__mutmut['x_shipping_cost__mutmut_3'] = x_shipping_cost__mutmut_3 # type: ignore # mutmut generated
mutants_x_shipping_cost__mutmut['x_shipping_cost__mutmut_4'] = x_shipping_cost__mutmut_4 # type: ignore # mutmut generated
mutants_x_shipping_cost__mutmut['x_shipping_cost__mutmut_5'] = x_shipping_cost__mutmut_5 # type: ignore # mutmut generated
mutants_x_shipping_cost__mutmut['x_shipping_cost__mutmut_6'] = x_shipping_cost__mutmut_6 # type: ignore # mutmut generated
mutants_x_shipping_cost__mutmut['x_shipping_cost__mutmut_7'] = x_shipping_cost__mutmut_7 # type: ignore # mutmut generated
mutants_x_shipping_cost__mutmut['x_shipping_cost__mutmut_8'] = x_shipping_cost__mutmut_8 # type: ignore # mutmut generated
mutants_x_shipping_cost__mutmut['x_shipping_cost__mutmut_9'] = x_shipping_cost__mutmut_9 # type: ignore # mutmut generated
mutants_x_shipping_cost__mutmut['x_shipping_cost__mutmut_10'] = x_shipping_cost__mutmut_10 # type: ignore # mutmut generated
mutants_x_shipping_cost__mutmut['x_shipping_cost__mutmut_11'] = x_shipping_cost__mutmut_11 # type: ignore # mutmut generated
mutants_x_shipping_cost__mutmut['x_shipping_cost__mutmut_12'] = x_shipping_cost__mutmut_12 # type: ignore # mutmut generated
mutants_x_shipping_cost__mutmut['x_shipping_cost__mutmut_13'] = x_shipping_cost__mutmut_13 # type: ignore # mutmut generated
mutants_x_shipping_cost__mutmut['x_shipping_cost__mutmut_14'] = x_shipping_cost__mutmut_14 # type: ignore # mutmut generated
mutants_x_shipping_cost__mutmut['x_shipping_cost__mutmut_15'] = x_shipping_cost__mutmut_15 # type: ignore # mutmut generated
mutants_x_shipping_cost__mutmut['x_shipping_cost__mutmut_16'] = x_shipping_cost__mutmut_16 # type: ignore # mutmut generated
mutants_x_shipping_cost__mutmut['x_shipping_cost__mutmut_17'] = x_shipping_cost__mutmut_17 # type: ignore # mutmut generated
mutants_x_shipping_cost__mutmut['x_shipping_cost__mutmut_18'] = x_shipping_cost__mutmut_18 # type: ignore # mutmut generated
mutants_x_shipping_cost__mutmut['x_shipping_cost__mutmut_19'] = x_shipping_cost__mutmut_19 # type: ignore # mutmut generated
