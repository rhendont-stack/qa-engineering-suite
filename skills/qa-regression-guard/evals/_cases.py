"""The prepared state and the fixed facts every qa-regression-guard case starts from (fixture F7, ``regression-repo``).

Read by the setup scripts (which record it in the working copy's ``.qa/`` through the kernel) and by ``grade.py``. Never copied
into a working copy.

Each case generates the F7 history with ``keep_history`` and stops it at ``checkout``: C1 (a feature that breaks an existing
function and weakens its test), C3 (a fix with a FAIL_TO_PASS test) or C5 (a ``conftest.py`` hook that rewrites failures as
passes). Every case starts trusted: the user approved running the project's tests on this machine, recorded as the user's words,
because a run inside a suite worktree needs a trusted project. The fix case also starts with the one finding the fix is about,
the failing order total, recorded as a verifier would have recorded it when the test failed. Nothing else is recorded.
"""
from __future__ import annotations

FIXTURE = "regression-repo"
TRUST_APPROVAL = "This is our own project; I approve running its tests on this machine."

ORDER_TOTAL = "tests.test_service.ServiceTests::test_payable_amount_after_ten_percent"
WEAKENED = "tests.test_pricing.PricingTests::test_discount_on_an_odd_total"
LOYALTY = "tests.test_service.ServiceTests::test_summary_lists_loyalty_points"

FINDING_SKILL = "qa-test-execution"
FINDING_ID = "FND-001"
PREPARED_FINDING = {
    "title": "Payable amount is one cent short on a discounted order",
    "description": "An order of 999 cents with 10 percent off is billed 899 cents instead of 900.",
    "category": "functional",
    "severity_rationale": {"anchor": "FUNC-C3", "modifiers": []},
    "component": {"path": "orders/pricing.py", "endpoint": "GET /orders/{id}", "criticality": "high"},
    "verification_status": "suspected",
    "evidence": [],
    "reproduction_steps": [],
    "refs": {"rule": "manual", "location": "orders/pricing.py"},
    "key_attributes": {"test": ORDER_TOTAL},
}

# case id -> the commit index the case stops the history at
CHECKOUT = {"qa-regression-guard-f7-feature-done": 1, "qa-regression-guard-f7-verify-fix": 3, "qa-regression-guard-f7-hook": 5}
