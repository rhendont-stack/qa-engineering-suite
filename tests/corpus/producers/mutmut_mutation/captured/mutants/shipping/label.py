

from mutmut.mutation.trampoline import wrap_in_trampoline as _mutmut_mutated, MutantDict
mutants_x_label__mutmut: MutantDict = {}  # type: ignore
@_mutmut_mutated(mutants_x_label__mutmut)
def label(country):
    if country == "ES":
        return "domestic"
    return "international"
def x_label__mutmut_orig(country):
    if country == "ES":
        return "domestic"
    return "international"
def x_label__mutmut_1(country):
    if country != "ES":
        return "domestic"
    return "international"
def x_label__mutmut_2(country):
    if country == "XXESXX":
        return "domestic"
    return "international"
def x_label__mutmut_3(country):
    if country == "es":
        return "domestic"
    return "international"
def x_label__mutmut_4(country):
    if country == "ES":
        return "XXdomesticXX"
    return "international"
def x_label__mutmut_5(country):
    if country == "ES":
        return "DOMESTIC"
    return "international"
def x_label__mutmut_6(country):
    if country == "ES":
        return "domestic"
    return "XXinternationalXX"
def x_label__mutmut_7(country):
    if country == "ES":
        return "domestic"
    return "INTERNATIONAL"

mutants_x_label__mutmut['_mutmut_orig'] = x_label__mutmut_orig # type: ignore # mutmut generated
mutants_x_label__mutmut['x_label__mutmut_1'] = x_label__mutmut_1 # type: ignore # mutmut generated
mutants_x_label__mutmut['x_label__mutmut_2'] = x_label__mutmut_2 # type: ignore # mutmut generated
mutants_x_label__mutmut['x_label__mutmut_3'] = x_label__mutmut_3 # type: ignore # mutmut generated
mutants_x_label__mutmut['x_label__mutmut_4'] = x_label__mutmut_4 # type: ignore # mutmut generated
mutants_x_label__mutmut['x_label__mutmut_5'] = x_label__mutmut_5 # type: ignore # mutmut generated
mutants_x_label__mutmut['x_label__mutmut_6'] = x_label__mutmut_6 # type: ignore # mutmut generated
mutants_x_label__mutmut['x_label__mutmut_7'] = x_label__mutmut_7 # type: ignore # mutmut generated
