"""F5 ``seeded-defects``: a small storefront whose smoke tests pass while the code and its requirements hold known problems.

Everything runs through the real CLI against the answer key. The fixture's own files never say what is wrong with it.
"""
from __future__ import annotations

import json
import re
import sys

from helpers import FIXTURES, answer_key

F5 = FIXTURES / "seeded-defects"
PYTEST_RUN = ("run", "--report", "junit:{evidence}/junit.xml", "--", sys.executable, "-m", "pytest", "-q", "-o", "junit_family=xunit2",
              "--junitxml={evidence}/junit.xml")
SEED_KINDS = {"boundary_comparison", "float_money_rounding", "discount_precedence", "timezone_boundary", "empty_cart_unhandled"}
REQUIREMENT_KINDS = {"ambiguity_english", "ambiguity_spanish", "contradiction", "missing_behavior"}
# What a smoke test must never contain: the value or the call that would show a seeded problem.
EXPOSING = ("100", "99.99", "19.99", "to_cents", "promo_is_active", "discounted", "coupon", "timezone", "Cart().receipt")


def line_of(path, fragment):
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if fragment in line:
            return number
    raise AssertionError("%s holds no line with %r" % (path, fragment))


def write(project, name, record):
    path = project.root.parent / "records" / (name + ".json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return str(path)


def add(project, command, name, record):
    code, out = project.cli(*command, "--json", write(project, name, record))
    assert code == 0, out
    return out["id"]


def record_f5_analysis(project):
    """The deterministic set: three requirements, two criteria, three test cases (in that creation order) and one risk."""
    requirements = F5 / "requirements.md"
    source = lambda fragment: {"path": "requirements.md", "line": line_of(requirements, fragment)}  # noqa: E731
    assert add(project, ("req", "add"), "req1", {"statement": "An order of 100.00 or more gets a 10 percent bulk discount.", "type": "functional",
               "source": source("bulk discount"), "provenance": "documented", "priority": "must"}) == "REQ-001"
    assert add(project, ("req", "add"), "req2", {"statement": "Totals go to the payment gateway in whole cents, as shown on the receipt.",
               "type": "functional", "source": source("whole cents"), "provenance": "documented", "priority": "must"}) == "REQ-002"
    assert add(project, ("req", "add"), "req3", {"statement": "Percentage discounts apply before a coupon.", "type": "functional",
               "source": source("Percentage discounts"), "provenance": "documented", "priority": "should"}) == "REQ-003"
    assert add(project, ("ac", "add"), "ac1", {"requirement": "REQ-001", "given": "an order of exactly 100.00", "when": "the receipt is made",
               "then": "the discount is 10 percent", "verifiable": True}) == "AC-001.1"
    assert add(project, ("ac", "add"), "ac3", {"requirement": "REQ-003", "given": "a 10 percent discount and a 5.00 coupon",
               "when": "the receipt is made", "then": "the percentage comes off first", "verifiable": True}) == "AC-003.1"
    case = {"level": "unit", "technique": "boundary-value-analysis", "technique_reason": "the threshold is a boundary", "oracle": "requirement",
            "priority": "P1", "criticality": "high", "links": ["AC-001.1"]}
    assert add(project, ("tc", "add"), "tc1", dict(case, title="Small orders get no discount")) == "TC-001"
    assert add(project, ("tc", "add"), "tc2", dict(case, title="The threshold order gets the discount")) == "TC-002"
    assert add(project, ("tc", "add"), "tc3", dict(case, title="A large order gets the discount")) == "TC-003"
    assert add(project, ("risk", "add"), "rsk1", {"title": "A discount is applied wrongly", "likelihood": 3, "impact": 4,
               "mitigations": ["TC-001"]}) == "RSK-001"


def test_f5_suite_passes_and_hides_every_seed(qa_on_fixture):
    key = answer_key("seeded-defects")["suites"]["python"]
    project = qa_on_fixture("seeded-defects")
    code, ran = project.cli(*PYTEST_RUN)
    assert (code, ran["status"]) == (0, key["run_status"]) and ran["status"] == "passed"
    assert project.results(ran["execution"]) == key["tests"] and len(key["tests"]) == 3
    tests = (F5 / "tests" / "test_smoke.py").read_text(encoding="utf-8")
    assert not [word for word in EXPOSING if word in tests]
    for path in F5.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            assert not re.search(r"(?i)seed|defect|\bbug\b|answer|off-by-one", path.read_text(encoding="utf-8")), path


def test_f5_answer_key_locations_exist(repo_root):
    key = answer_key("seeded-defects")
    anchors = json.loads((repo_root / "shared" / "policies" / "severity-rubric.json").read_text(encoding="utf-8"))["anchors"]
    categories = set(json.loads((repo_root / "shared" / "schemas" / "defs.schema.json").read_text(encoding="utf-8"))["$defs"]["category"]["enum"])
    issues = key["issues"]
    assert key["fixture"] == "seeded-defects" and len({issue["id"] for issue in issues}) == len(issues)
    assert {issue["kind"] for issue in issues} == SEED_KINDS | REQUIREMENT_KINDS
    for issue in issues:
        location = issue["location"]
        path = F5 / location["path"]
        assert path.is_file(), issue["id"]
        text = path.read_text(encoding="utf-8")
        if "function" in location:
            assert re.search(r"^\s*def %s\(" % re.escape(location["function"]), text, re.M), issue["id"]
        if "line" in location:
            assert location["match"] in text.splitlines()[location["line"] - 1], issue["id"]
        assert issue["severity_anchor"] in anchors and issue["category"] in categories, issue["id"]
        assert issue["detectable_by"] and all(isinstance(tool, str) and tool for tool in issue["detectable_by"]), issue["id"]
    seeds = [issue for issue in issues if issue["kind"] in SEED_KINDS]
    assert len(seeds) == 5 and all(issue["location"]["path"].startswith("shop/") for issue in seeds)
    assert all(issue["location"]["path"] == "requirements.md" for issue in issues if issue["kind"] in REQUIREMENT_KINDS)


def test_f5_requirements_hold_the_weak_words_the_key_names():
    text = (F5 / "requirements.md").read_text(encoding="utf-8").lower()
    for word in ("quickly", "appropriate", "as needed", "etc.", "rápida", "amigable", "cuando sea posible", "según sea necesario"):
        assert word in text, word
    assert "at most one coupon" in text and "up to three coupons" in text       # the contradiction


def test_f5_trace_matrix_reports_the_expected_gaps(qa_on_fixture):
    project = qa_on_fixture("seeded-defects", git=True)
    record_f5_analysis(project)
    code, ran = project.cli(*PYTEST_RUN)
    assert code == 0 and ran["status"] == "passed"
    smoke = "tests.test_smoke::test_a_small_order_gets_no_discount"        # classname::name, as JUnit records it
    assert project.cli("trace", "link", "TC-001", "automated_by", smoke)[0] == 0
    assert project.cli("trace", "link", "TC-003", "automated_by", "tests.test_smoke::test_a_large_order_gets_the_discount")[0] == 0
    code, out = project.cli("trace", "matrix")
    assert code == 0, out
    matrix = json.loads((project.root / out["path"]).read_text(encoding="utf-8"))
    assert {(gap["kind"], gap["id"]) for gap in matrix["gaps"]} == {
        ("req_without_ac", "REQ-002"), ("ac_without_tc", "AC-003.1"), ("tc_not_automated", "TC-002"), ("tc_never_executed", "TC-003"),
        ("tc_unproven", "TC-001"), ("tc_unproven", "TC-003")}
    assert out["counts"] == {"req_without_ac": 1, "ac_without_tc": 1, "tc_not_automated": 1, "tc_never_executed": 1,
                             "tc_last_blocked_or_inconclusive": 0, "req_only_characterization_or_assumed": 0, "tc_unproven": 2,
                             "fnd_without_fix": 0, "fix_without_rvl": 0, "rvl_off_target": 0}
    first = next(case for case in matrix["testcases"] if case["id"] == "TC-001")
    assert first["status"] == "passed" and first["tests"][0]["test_id"] == smoke
