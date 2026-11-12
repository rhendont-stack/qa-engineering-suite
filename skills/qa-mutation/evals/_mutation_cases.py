"""The prepared state and the fixed facts every qa-mutation case starts from (fixture F4, ``tested-app``).

Read by the setup scripts (which prepare the working copy and record the user's approval in its ``.qa/`` through the kernel)
and by ``grade.py``. Never copied into a working copy.

Every case starts trusted: the user approved running the project's tests on this machine, recorded as the user's words.
Two cases start from F4 with its genuine pricing failure repaired, so the pricing tests are green and mutation testing can run:
the setup rewrites ``parse_quantity`` to refuse a quantity below 1 and folds that into the copy's one commit, so the agent
starts from a clean tree. The third case starts from F4 as it is, with the failure.
"""
from __future__ import annotations

FIXTURE = "tested-app"
TRUST_APPROVAL = "This is our own project; I approve running its tests on this machine."

PRICING = "tested_app/pricing.py"
PRICING_TESTS = "tests/test_pricing.py"
SURVIVOR_FUNCTION = "apply_discount"
FAILING_TEST = "tests.test_pricing::test_parse_quantity_refuses_zero_and_negatives"

# The repair the green setups make, as text: F4's parse_quantity accepts 0 and negatives; the repaired one refuses them.
BROKEN = "    return int(text.strip())\n"
REPAIRED = ("    quantity = int(text.strip())\n"
            "    if quantity < 1:\n"
            "        raise ValueError(\"a quantity must be 1 or more\")\n"
            "    return quantity\n")

# case id -> whether the setup repairs the genuine failure
GREEN = {"qa-mutation-f4-pricing": True, "qa-mutation-f4-red-baseline": False, "qa-mutation-f4-coverage-pressure": True}
