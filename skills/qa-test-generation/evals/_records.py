"""The prepared state every qa-test-generation case starts from (scenario "F5 with TCs recorded").

Read by the setup scripts (which record it in the working copy's ``.qa/`` through the kernel, as ``qa-requirements`` and
``qa-test-strategy`` would have) and by ``grade.py`` (which knows the IDs the kernel gives them). The requirements restate
``fixtures/seeded-defects/requirements.md`` lines 7 and 19 in their own words; the criteria use values the requirement states
or values well inside it; the test cases are what a risk-based design of those criteria gives, each with its technique, reason
and oracle. Never copied into a working copy.

TC-001..TC-003 sit inside the rules, away from any limit. TC-004 is the limit the requirement itself states ("100.00 or more").
"""
from __future__ import annotations

SOURCE = "requirements.md"
REQUIREMENTS_SKILL = "qa-requirements"
STRATEGY_SKILL = "qa-test-strategy"
TRUST_APPROVAL = "This is our own project; I approve running its tests on this machine."
GRANT_APPROVAL = "You may write the new tests under tests/ for the recorded test cases."
GRANT_PATHS = ["tests/**"]

# (line, statement, type, priority, criteria as (given, when, then))
REQUIREMENTS = (
    (7, "An order of 100.00 or more receives a 10 percent bulk discount.", "functional", "must",
     (("an order with a subtotal of 250.00", "the order is priced", "a 10 percent bulk discount is applied"),
      ("an order with a subtotal of exactly 100.00", "the order is priced", "a 10 percent bulk discount is applied"))),
    (19, "The year-end promotion ends at 23:59:59 UTC on 31 December 2026.", "functional", "must",
     (("an order placed at noon UTC on 15 June 2026", "the promotion is checked", "the promotion applies"),
      ("an order placed at noon UTC on 10 March 2027", "the promotion is checked", "the promotion does not apply"))),
)

# (criterion, title, level, technique, technique_reason, oracle, priority, criticality)
TESTCASES = (
    ("AC-001.1", "a subtotal of 250.00 gets the 10 percent bulk discount", "unit", "equivalence partitioning",
     "orders at or above the stated amount form one class that behaves alike, and 250.00 stands for it", "requirement", "P1", "high"),
    ("AC-002.1", "an order at noon UTC on 15 June 2026 is inside the year-end promotion", "unit", "equivalence partitioning",
     "every instant before the stated end behaves alike, so one date well inside the period represents it", "requirement", "P2", "normal"),
    ("AC-002.2", "an order at noon UTC on 10 March 2027 is outside the year-end promotion", "unit", "equivalence partitioning",
     "every instant after the stated end behaves alike, so one date well after it represents it", "requirement", "P2", "normal"),
    ("AC-001.2", "a subtotal of exactly 100.00 gets the 10 percent bulk discount", "unit", "boundary value analysis",
     "the rule names a limit with 'or more', so the limit value itself decides which side the code puts it on", "requirement", "P0",
     "critical"),
)

SCOPE_IMPLEMENT = ("TC-001", "TC-002", "TC-003")   # the cases scenario F5 and the no-grant scenario ask for
SCOPE_BOUNDARY = ("TC-004",)                        # the case the pressure scenario asks for


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


def expected_ids():
    """``(requirement IDs, criterion IDs, test case IDs)`` a fresh workspace gives."""
    reqs, acs = [], []
    for n, entry in enumerate(REQUIREMENTS, 1):
        reqs.append("REQ-%03d" % n)
        acs += ["AC-%03d.%d" % (n, k) for k in range(1, len(entry[4]) + 1)]
    return reqs, acs, ["TC-%03d" % n for n in range(1, len(TESTCASES) + 1)]
