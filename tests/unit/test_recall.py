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


def found(tool, path=None, line=None, endpoint=None):
    return {"tool": tool, "path": path, "line": line, "endpoint": endpoint}


def test_recall_counts_only_detectable_entries():
    recall = load_recall()
    detections = [found("gitleaks", "/work/.qa-input/lab/settings.py", 4), found("zap", endpoint="GET /search")]
    assert recall(KEY, detections, {"gitleaks", "zap"}) == {"detectable": 3, "detected": 2, "recall": 2 / 3, "missed": ["X-04"]}
    # osv-scanner present but silent: X-03 becomes detectable and is missed; semgrep is not present, so X-04 needs zap
    assert recall(KEY, detections[:1], {"gitleaks", "osv-scanner"}) == {"detectable": 2, "detected": 1, "recall": 0.5, "missed": ["X-03"]}
    assert recall(KEY, [found("static-review", "lab/app.py", 12)], {"static-review"}) == {
        "detectable": 2, "detected": 1, "recall": 0.5, "missed": ["X-02"]}


def test_recall_is_none_when_no_tool_is_present():
    recall = load_recall()
    assert recall(KEY, [], set()) == {"detectable": 0, "detected": 0, "recall": None, "missed": []}
    # a detection whose tool is not present counts for nothing, and a tool no entry names makes nothing detectable
    assert recall(KEY, [found("zap", endpoint="GET /search")], {"trivy"}) == {"detectable": 0, "detected": 0, "recall": None, "missed": []}
    assert recall({"issues": []}, [], {"zap"}) == {"detectable": 0, "detected": 0, "recall": None, "missed": []}


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
    assert everything == {"detectable": len(key["issues"]), "detected": len(key["issues"]), "recall": 1.0, "missed": []}
    assert recall(key, cited[1:], {"static-review"})["missed"] == [key["issues"][0]["id"]]
