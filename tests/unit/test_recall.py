"""``benchmark/graders/recall.py``: answer-key recall over the entries that the tools present could find."""
from __future__ import annotations

import importlib.util

import pytest

from helpers import REPO, answer_key

KEY = {"fixture": "example", "issues": [
    {"id": "X-01", "location": "GET /search", "file": "lab/app.py", "lines": [10, 14], "detectable_by": ["zap", "static-review"]},
    {"id": "X-02", "location": "lab/settings.py", "file": "lab/settings.py", "lines": [4, 4], "detectable_by": ["gitleaks", "static-review"]},
    {"id": "X-03", "location": "requirements.txt", "file": "requirements.txt", "lines": [1, 1], "detectable_by": ["osv-scanner"]},
    {"id": "X-04", "location": "GET /orders/{id}", "file": "lab/orders.py", "lines": [20, 22], "detectable_by": ["semgrep", "zap"]},
]}


def load_recall():
    spec = importlib.util.spec_from_file_location("benchmark_graders_recall", str(REPO / "benchmark" / "graders" / "recall.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.recall


def found(tool, path=None, line=None, endpoint=None, cwe=None):
    detection = {"tool": tool, "path": path, "line": line, "endpoint": endpoint}
    if cwe is not None:
        detection["cwe"] = cwe
    return detection


def test_recall_counts_only_detectable_entries():
    recall = load_recall()
    detections = [found("gitleaks", "/work/.qa-input/lab/settings.py", 4), found("zap", endpoint="GET /search")]
    assert recall(KEY, detections, {"gitleaks", "zap"}) == {"detectable": 3, "detected": 2, "recall": 2 / 3, "missed": ["X-04"],
                                                                     "detected_unclassified": ["X-01", "X-02"]}
    # osv-scanner present but silent: X-03 becomes detectable and is missed; semgrep is not present, so X-04 needs zap
    assert recall(KEY, detections[:1], {"gitleaks", "osv-scanner"}) == {"detectable": 2, "detected": 1, "recall": 0.5, "missed": ["X-03"],
                                                                                "detected_unclassified": ["X-02"]}
    assert recall(KEY, [found("static-review", "lab/app.py", 12)], {"static-review"}) == {
        "detectable": 2, "detected": 1, "recall": 0.5, "missed": ["X-02"], "detected_unclassified": ["X-01"]}


def test_recall_is_none_when_no_tool_is_present():
    recall = load_recall()
    assert recall(KEY, [], set()) == {"detectable": 0, "detected": 0, "recall": None, "missed": [], "detected_unclassified": []}
    # a detection whose tool is not present counts for nothing, and a tool no entry names makes nothing detectable
    nothing = {"detectable": 0, "detected": 0, "recall": None, "missed": [], "detected_unclassified": []}
    assert recall(KEY, [found("zap", endpoint="GET /search")], {"trivy"}) == nothing
    assert recall({"issues": []}, [], {"zap"}) == nothing


@pytest.mark.parametrize("detection, hit", [
    (found("zap", path="lab/app.py", line=12), True),                       # file and a line within the entry's lines
    (found("zap", path="./lab/app.py", line=10), True),
    (found("zap", path="lab\\app.py", line=14), True),
    (found("zap", path="/work/.qa-input/lab/app.py", line=14), True),        # a container path ending in the key's file
    (found("zap", path="file:///work/lab/app.py", line=11), True),
    (found("zap", path="lab/app.py", line=15), False),                       # past the last line
    (found("zap", path="lab/app.py", line=9), False),
    (found("zap", path="lab/app.py", line=None), False),                     # a file without a line locates nothing
    (found("zap", path="otherlab/app.py", line=12), False),                  # a suffix only at a directory boundary
    (found("zap", path="lab/app.py", line=True), False),
    (found("zap", endpoint="GET /search"), True),
    (found("zap", endpoint="get /search?q=pen"), True),                       # the query string is not part of the operation
    (found("zap", endpoint="GET http://127.0.0.1:8080/search?q=pen"), True),
    (found("zap", endpoint="GET /%73earch"), True),                           # percent-decoded before matching
    (found("zap", endpoint="GET http://127.0.0.1:8080/%73earch"), True),
    (found("zap", path="/work/lab%2Fapp.py", line=12), True),
    (found("zap", path="file:///work/la%62/app.py", line=12), True),
    (found("zap", endpoint="POST /search"), False),
    (found("zap", endpoint="GET /search/more"), False),
    (found("zap", endpoint="GET"), False),
    (found("gitleaks", path="lab/app.py", line=12), False),                   # a tool the entry does not name
])
def test_a_detection_matches_by_file_and_line_or_by_operation(detection, hit):
    result = load_recall()({"issues": KEY["issues"][:1]}, [detection], {"zap", "gitleaks"})
    assert result["detected"] == (1 if hit else 0) and result["detectable"] == 1


def test_an_operation_with_a_path_parameter_matches_one_segment():
    recall = load_recall()
    entry = {"issues": [KEY["issues"][3]]}
    assert recall(entry, [found("zap", endpoint="GET /orders/7")], {"zap"})["detected"] == 1
    assert recall(entry, [found("zap", endpoint="GET /orders/7/items")], {"zap"})["detected"] == 0
    assert recall(entry, [found("zap", endpoint="GET /orders/")], {"zap"})["detected"] == 0
    assert recall({"issues": [KEY["issues"][1]]}, [found("gitleaks", endpoint="GET /lab/settings.py")], {"gitleaks"})["detected"] == 0


def test_every_f6_entry_is_found_by_a_review_that_cites_its_first_line():
    key = answer_key("vuln-lab")
    recall = load_recall()
    cited = [found("static-review", issue["file"], issue["lines"][0]) for issue in key["issues"]]
    everything = recall(key, cited, {"static-review"})
    assert everything == {"detectable": len(key["issues"]), "detected": len(key["issues"]), "recall": 1.0, "missed": [],
                          "detected_unclassified": [issue["id"] for issue in key["issues"]]}
    assert recall(key, cited[1:], {"static-review"})["missed"] == [key["issues"][0]["id"]]


KEY_WITH_CLASSES = {"issues": [
    {"id": "C-01", "location": "GET /", "file": "lab/app.py", "lines": [41, 44], "class": "CWE-942 permissive cross-origin policy",
     "detectable_by": ["zap"]},
    {"id": "C-02", "location": "GET /", "file": "lab/app.py", "lines": [40, 40], "class": "CWE-693 missing security headers",
     "detectable_by": ["zap"]},
    {"id": "C-03", "location": "GET /products", "file": "lab/catalog.py", "lines": [21, 22], "class": "CWE-89 SQL injection",
     "detectable_by": ["zap", "semgrep"]},
    {"id": "C-04", "location": "requirements.txt", "file": "requirements.txt", "lines": [1, 1], "file_level": True,
     "class": "CWE-1395 dependency with a published advisory", "detectable_by": ["osv-scanner"]},
]}


def test_a_detection_of_another_class_at_the_same_location_credits_nothing():
    recall = load_recall()
    headers = found("zap", endpoint="GET /products", cwe="CWE-693")
    assert recall(KEY_WITH_CLASSES, [headers], {"zap"})["missed"] == ["C-01", "C-02", "C-03"]
    assert recall(KEY_WITH_CLASSES, [found("zap", endpoint="GET /", cwe="CWE-693")], {"zap"}) == {
        "detectable": 3, "detected": 1, "recall": 1 / 3, "missed": ["C-01", "C-03"], "detected_unclassified": []}
    assert recall(KEY_WITH_CLASSES, [found("semgrep", "lab/catalog.py", 21, cwe="CWE-79")], {"semgrep"})["detected"] == 0


def test_a_detection_of_the_same_class_at_the_same_location_is_detected():
    recall = load_recall()
    for cwe in ("CWE-89", "cwe-89", "89", 89):
        result = recall(KEY_WITH_CLASSES, [found("semgrep", "/work/lab/catalog.py", 22, cwe=cwe)], {"semgrep"})
        assert result == {"detectable": 1, "detected": 1, "recall": 1.0, "missed": [], "detected_unclassified": []}, cwe
    both = [found("zap", endpoint="GET /", cwe="CWE-942"), found("zap", endpoint="GET /", cwe="CWE-693")]
    assert recall(KEY_WITH_CLASSES, both, {"zap"})["missed"] == ["C-03"]          # each class credits its own entry only


def test_a_detection_without_a_cwe_is_detected_and_listed_as_unclassified():
    recall = load_recall()
    plain = found("zap", endpoint="GET /")
    assert recall(KEY_WITH_CLASSES, [plain], {"zap"}) == {
        "detectable": 3, "detected": 2, "recall": 2 / 3, "missed": ["C-03"], "detected_unclassified": ["C-01", "C-02"]}
    classified = recall(KEY_WITH_CLASSES, [plain, found("zap", endpoint="GET /", cwe="CWE-942")], {"zap"})
    assert classified["detected_unclassified"] == ["C-02"]                         # C-01 also has a classified credit
    entry_without_class = {"issues": [dict(KEY["issues"][0])]}
    assert recall(entry_without_class, [found("zap", endpoint="GET /search", cwe="CWE-79")], {"zap"})["detected"] == 1


def test_a_file_level_detection_locates_only_a_file_level_entry():
    recall = load_recall()
    manifest = found("osv-scanner", "/work/requirements.txt", cwe="CWE-1395")
    assert recall(KEY_WITH_CLASSES, [manifest], {"osv-scanner"}) == {
        "detectable": 1, "detected": 1, "recall": 1.0, "missed": [], "detected_unclassified": []}
    assert recall(KEY_WITH_CLASSES, [found("zap", "lab/app.py", None)], {"zap"})["detected"] == 0
    key = answer_key("vuln-lab")
    assert [issue["id"] for issue in key["issues"] if issue.get("file_level") is True] == ["F6-05"]
    assert recall(key, [found("static-review", "requirements.txt")], {"static-review"})["missed"] == [
        issue["id"] for issue in key["issues"] if issue["id"] != "F6-05"]
