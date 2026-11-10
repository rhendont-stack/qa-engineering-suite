from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from qa import ingest

CORPUS = Path(__file__).resolve().parent
ENTRIES = json.loads((CORPUS / "MANIFEST.json").read_text())


@pytest.mark.parametrize("entry", ENTRIES, ids=[e["file"] for e in ENTRIES])
def test_corpus_matches_tool_console(entry):
    s = ingest.parse_report(entry["format"], CORPUS / entry["file"], 50 * 2**20)
    if s.kind == "coverage":
        check_coverage(s, entry)
        return
    got = {"total": s.total, "passed": s.passed, "failed": s.failed, "errors": s.errors, "skipped": s.skipped} if s.kind == "tests" else {"alerts": len(s.alerts), "runs": s.runs}
    assert got == entry["expected"]


def check_coverage(s, entry):
    """The totals the tool printed for itself (its own console or its own CSV) are the totals the parser finds.

    A tool prints counts, percentages, or both. A count it printed must equal ours exactly. A percentage is compared at the
    precision the tool printed it: ours is computed from the same file, never taken from the tool.
    """
    expected = dict(entry["expected"])
    percent = expected.pop("percent", {})
    ours = {"files": len(s.files), "lines_total": s.lines_total, "lines_covered": s.lines_covered,
            "branches_total": s.branches_total, "branches_covered": s.branches_covered}
    assert {key: ours[key] for key in expected} == expected
    assert entry["console"] and (expected or percent)
    computed = {"lines": 100.0 * s.lines_covered / s.lines_total, "branches": 100.0 * s.branches_covered / s.branches_total,
                "combined": 100.0 * (s.lines_covered + s.branches_covered) / (s.lines_total + s.branches_total)}
    for name, printed in percent.items():
        places = len(repr(printed).split(".")[1]) if isinstance(printed, float) else 0
        assert round(computed[name], places) == printed, name
    assert s.anomalies == ()  # a sample project has no path outside itself


@pytest.mark.parametrize("entry", [e for e in ENTRIES if e["kind"] == "coverage"], ids=lambda e: e["file"])
def test_coverage_corpus_files_are_normalized_and_name_their_tool(entry):
    normalize = load_producer_module("normalize.py")
    text = (CORPUS / entry["file"]).read_text(encoding="utf-8")
    assert normalize.normalize_text(text) == text
    assert entry["format"] in ("lcov", "cobertura", "jacoco") and entry["producer"] and entry["version"]


def corpus_files():
    return [p for p in CORPUS.rglob("*") if p.is_file() and "producers" not in p.relative_to(CORPUS).parts
            and p.name != "test_corpus.py" and "__pycache__" not in p.parts and p.relative_to(CORPUS).parts[0] != "guard"]


def sample_key():
    spec = importlib.util.spec_from_file_location("make_sample", CORPUS / "producers" / "gitleaks_sample" / "make_sample.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build_key()


def test_corpus_files_are_the_manifest_files():
    listed = {e["file"] for e in ENTRIES}
    on_disk = {p.relative_to(CORPUS).as_posix() for p in corpus_files() if p.name != "MANIFEST.json"}
    assert listed == on_disk


def load_producer_module(*parts):
    path = CORPUS.joinpath("producers", *parts)
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_corpus_has_no_secret_and_no_trace_of_this_machine():
    key = sample_key()
    assert key.startswith("AKIA") and len(key) == 20
    normalize = load_producer_module("normalize.py")
    files = corpus_files() + [CORPUS / "MANIFEST.json"]
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert key not in text and key[4:] not in text, "key found in %s" % path.relative_to(CORPUS)
        for label, pattern, _token in normalize.sensitive_patterns():
            assert not pattern.search(text), "%s found in %s" % (label, path.relative_to(CORPUS))


def test_trace_rule_matches_whole_tokens_only():
    n = load_producer_module("normalize.py")
    user = n.name_pattern("alice")
    assert user.search("user=alice") and user.search("/home/alice/x") and user.search("alice")
    assert not user.search("gitlab-alice-token") and not user.search("org.alice.Foo") and not user.search("alice-x")
    home = n.home_pattern("/home/alice")
    assert home.search("/home/alice/x") and home.search("at /home/alice") and not home.search("/home/alice2/x")
    assert n.home_pattern("/") is None and n.home_pattern("") is None


@pytest.mark.parametrize("name", ["root", "runner", "localhost", "Runner"])
def test_exempt_names_are_never_checked_or_replaced(name):
    n = load_producer_module("normalize.py")
    assert n.name_pattern(name) is None


@pytest.mark.parametrize("user", ["runner", "root"])
def test_trace_check_passes_under_ci_account_names(user):
    import os
    import subprocess
    import sys
    env = dict(os.environ, LOGNAME=user, USER=user)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(CORPUS / "test_corpus.py"),
                        "-k", "no_secret"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True, cwd=str(CORPUS.parents[1]))
    assert r.returncode == 0, r.stdout


def test_normalize_is_idempotent_on_committed_sarif():
    n = load_producer_module("normalize.py")
    text = (CORPUS / "sarif" / "gitleaks-8.30.1.sarif").read_text(encoding="utf-8")
    assert n.normalize_text(text) == text
