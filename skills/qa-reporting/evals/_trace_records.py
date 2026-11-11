"""The prepared state of the qa-reporting traceability case (scenario "F5 with requirements, test cases, a run and two gaps").

Read by ``setup_f5_trace.py`` (which records it in the working copy's ``.qa/`` through the kernel, as qa-requirements,
qa-test-strategy, qa-test-generation and qa-test-execution would have) and by ``grade.py``. Never copied into a working copy.

Two requirements restate ``fixtures/seeded-defects/requirements.md`` lines 7 and 19, each with one acceptance criterion. One test
case verifies the first criterion and is automated by an existing test of the fixture, which a recorded full run passes. That
leaves exactly two traceability gaps: the second criterion has no test case (``ac_without_tc``), and the test case has no red
check, so nothing shows its test can fail (``tc_unproven``). The gate's ``requirements_verified`` is then ``NO_EVIDENCE``: the
second requirement's criterion is not verified by a passing structured test.
"""
from __future__ import annotations

SOURCE = "requirements.md"
REQUIREMENTS_SKILL = "qa-requirements"
STRATEGY_SKILL = "qa-test-strategy"
GENERATION_SKILL = "qa-test-generation"
EXECUTION_SKILL = "qa-test-execution"
TRUST_APPROVAL = "This is our own project; I approve running its tests on this machine."

# (line, statement, type, priority, criteria as (given, when, then))
REQUIREMENTS = (
    (7, "An order of 100.00 or more receives a 10 percent bulk discount.", "functional", "must",
     (("an order with a subtotal of 40.00", "the order is priced", "no bulk discount is applied"),)),
    (19, "The year-end promotion ends at 23:59:59 UTC on 31 December 2026.", "functional", "must",
     (("an order placed at noon UTC on 15 June 2026", "the promotion is checked", "the promotion applies"),)),
)

# (criterion, title, level, technique, technique_reason, oracle, priority, criticality)
TESTCASES = (
    ("AC-001.1", "a subtotal of 40.00 gets no bulk discount", "unit", "equivalence partitioning",
     "every subtotal below the stated amount behaves alike, and 40.00 stands for it", "requirement", "P1", "high"),
)
AUTOMATED = (("TC-001", "tests.test_smoke::test_a_small_order_gets_no_discount"),)
EXPECTED_GAPS = (("ac_without_tc", "AC-002.1"), ("tc_unproven", "TC-001"))


def requirement_records():
    return [{"statement": statement, "type": kind, "source": {"path": SOURCE, "line": line}, "provenance": "documented",
             "priority": priority, "quality_issues": []} for line, statement, kind, priority, _criteria in REQUIREMENTS]


def criterion_records(req_ids):
    out = []
    for req_id, (_line, _statement, _kind, _priority, criteria) in zip(req_ids, REQUIREMENTS):
        for given, when, then in criteria:
            out.append({"requirement": req_id, "given": given, "when": when, "then": then, "verifiable": True})
    return out


def testcase_records():
    return [{"title": title, "level": level, "technique": technique, "technique_reason": reason, "oracle": oracle,
             "priority": priority, "criticality": criticality, "automation": [], "links": [criterion]}
            for criterion, title, level, technique, reason, oracle, priority, criticality in TESTCASES]
