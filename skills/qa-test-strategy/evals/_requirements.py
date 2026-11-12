"""The requirement and acceptance-criteria set every qa-test-strategy case starts from (scenario "F5 with REQ/AC recorded").

Read by ``setup_requirements.py`` (which records it in the working copy's ``.qa/`` through the kernel, as ``qa-requirements`` would
have) and by ``grade.py`` (which knows the IDs the kernel gives them). It restates ``fixtures/seeded-defects/requirements.md``
line by line and adds nothing that file does not say: each criterion uses the requirement's own words or a value well inside
the stated rule, never a value on a limit, so the records point at no defect in the code. Never copied into a working copy.
"""
from __future__ import annotations

SOURCE = "requirements.md"
SKILL = "qa-requirements"  # the skill whose work the setup reproduces

# (line, statement, type, priority, quality issues, criteria as (given, when, then, verifiable))
REQUIREMENTS = (
    (7, "An order of 100.00 or more receives a 10 percent bulk discount.", "functional", "must", (),
     (("an order with a subtotal of 250.00", "the order is priced", "a 10 percent bulk discount is applied", True),
      ("an order with a subtotal of 40.00", "the order is priced", "no bulk discount is applied", True))),
    (9, "A coupon takes a fixed amount off the order; percentage discounts are applied first, then the coupon.", "functional", "must", (),
     (("an order that receives a percentage discount and carries a coupon", "the order is priced",
       "the percentage discount is taken off first and the coupon's fixed amount after it", True),)),
    (11, "The receipt must be produced quickly and should be easy to read.", "performance-efficiency", "should",
     ("not verifiable as written: no time limit and no readability measure",),
     (("a completed order", "the receipt is requested", "the receipt is produced within a time limit the source does not give", False),)),
    (13, "Sales tax is charged at an appropriate rate, rounded as needed.", "functional", "must",
     ("not verifiable as written: the rate and the rounding rule are not given",),
     (("a taxable order", "the order is priced", "sales tax is charged at a rate and rounding the source does not give", False),)),
    (15, "Totals are sent to the payment gateway in whole cents, exactly as shown on the receipt.", "functional", "must", (),
     (("a priced order", "its total is sent to the payment gateway", "the amount sent, in whole cents, equals the total shown on the receipt", True),)),
    (19, "The year-end promotion ends at 23:59:59 UTC on 31 December 2026.", "functional", "must", (),
     (("an order placed on 15 June 2026", "the promotion is checked", "the promotion applies", True),
      ("an order placed on 10 March 2027", "the promotion is checked", "the promotion does not apply", True))),
    (23, "A customer may use at most one coupon per order.", "functional", "must", (),
     (("an order with one coupon", "the order is priced", "the coupon is accepted", True),)),
    (25, "Customers may combine up to three coupons on one order.", "functional", "must", (),
     (("an order with two coupons", "the order is priced", "both coupons are accepted", True),)),
    (29, "El sistema debe responder de forma rápida y ofrecer una experiencia amigable al cliente.", "interaction-capability", "should",
     ("not verifiable as written: no response time and no usability measure",),
     (("a customer using the shop", "the customer acts", "the system responds within a time the source does not give", False),)),
    (31, "El cupón se aplicará cuando sea posible, o según sea necesario.", "functional", "should",
     ("not verifiable as written: the conditions for applying the coupon are not given",),
     (("an order with a coupon", "the order is priced", "the coupon is applied under conditions the source does not give", False),)),
)


def requirement_records():
    """The ``req import`` list, in the order of the file."""
    return [{"statement": statement, "type": kind, "source": {"path": SOURCE, "line": line}, "provenance": "documented",
             "priority": priority, "quality_issues": list(issues)}
            for line, statement, kind, priority, issues, _criteria in REQUIREMENTS]


def criterion_records(req_ids):
    """The ``ac add`` inputs for the requirement IDs the kernel returned (same order as ``REQUIREMENTS``)."""
    out = []
    for req_id, (_line, _statement, _kind, _priority, _issues, criteria) in zip(req_ids, REQUIREMENTS):
        for given, when, then, verifiable in criteria:
            out.append({"requirement": req_id, "given": given, "when": when, "then": then, "verifiable": verifiable})
    return out


def expected_ids():
    """``(requirement IDs, criterion IDs)`` the kernel gives a fresh workspace: REQ-001.. in file order, AC-<req>.<k>."""
    reqs, acs = [], []
    for n, entry in enumerate(REQUIREMENTS, 1):
        req = "REQ-%03d" % n
        reqs.append(req)
        acs += ["AC-%03d.%d" % (n, k) for k in range(1, len(entry[5]) + 1)]
    return reqs, acs
