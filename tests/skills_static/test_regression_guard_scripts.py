"""The bundled scripts of qa-regression-guard: the change's impact, the test-weakening scan and the FAIL_TO_PASS selection.

Each reads git through the kernel's neutralised session, writes nothing, and is deterministic. The scan's rules are checked on
small diffs per language, and all three on the F7 history, which the fixture's generator makes at test time.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap

import pytest

from helpers import FIXTURES, REPO, base

SCRIPTS = REPO / "skills" / "qa-regression-guard" / "scripts"
GENERATOR = FIXTURES / "regression-repo" / "generate.py"
ORDER_TOTAL = "tests.test_service.ServiceTests::test_payable_amount_after_ten_percent"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="tool missing: git")


def load(name):
    spec = importlib.util.spec_from_file_location("regression_guard_" + name, str(SCRIPTS / (name + ".py")))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scan = load("test_weakening_scan")
impact = load("diff_impact")
f2p = load("fail_to_pass")


def run(script, *args, cwd):
    done = subprocess.run([sys.executable, "-I", str(SCRIPTS / script)] + [str(a) for a in args], capture_output=True, text=True,
                          timeout=120, cwd=str(cwd), env={"PATH": os.environ["PATH"], "HOME": str(cwd)})
    return done.returncode, (json.loads(done.stdout) if done.stdout.strip() else None), done.stderr


def history(tmp_path, checkout=None):
    root = tmp_path / "f7"
    spec = importlib.util.spec_from_file_location("f7_generate_for_script_tests", str(GENERATOR))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.generate_history(root, checkout=checkout)
    done = subprocess.run(["git", "-C", str(root), "rev-list", "--reverse", "HEAD"], capture_output=True, text=True, check=True)
    return root, done.stdout.split()


def diff_of(files):
    """A unified diff from ``{path: (old text or None, new text or None)}``, as ``git diff`` writes one."""
    import difflib
    out = []
    for path, (old, new) in files.items():
        a = old.splitlines(keepends=True) if old is not None else []
        b = new.splitlines(keepends=True) if new is not None else []
        out.append("diff --git a/%s b/%s\n" % (path, path))
        body = list(difflib.unified_diff(a, b, "a/" + path if old is not None else "/dev/null", "b/" + path if new is not None else "/dev/null", n=3))
        out.extend(line if line.endswith("\n") else line + "\n" for line in body)
    return "".join(out)


def rules(hits):
    return sorted({(h["rule"], h["path"]) for h in hits})


# --------------------------------------------------------------------------- the diff parser

def test_the_parser_numbers_lines_on_both_sides_and_keeps_the_hunk_header():
    old = "a\nb\nc\nd\n"
    new = "a\nB\nc\nd\ne\n"
    files = scan.parse_diff(diff_of({"m.py": (old, new)}))
    assert [f["path"] for f in files] == ["m.py"]
    hunk = files[0]["hunks"][0]
    assert hunk["header"].startswith("@@ -1,4 +1,5 @@")
    assert [(l["kind"], l["old"], l["new"], l["text"]) for l in hunk["lines"] if l["kind"] != " "] == [
        ("-", 2, None, "b"), ("+", None, 2, "B"), ("+", None, 5, "e")]


def test_the_parser_reads_added_and_deleted_files_and_quoted_paths():
    files = scan.parse_diff(diff_of({"new.py": (None, "x\n"), "gone.py": ("y\n", None)}))
    assert [(f["path"], f["status"]) for f in files] == [("new.py", "A"), ("gone.py", "D")]
    quoted = 'diff --git "a/sp ace.py" "b/sp ace.py"\n--- "a/sp ace.py"\n+++ "b/sp ace.py"\n@@ -1 +1 @@\n-a\n+b\n'
    assert scan.parse_diff(quoted)[0]["path"] == "sp ace.py"


# --------------------------------------------------------------------------- the rules, per language

def hits_for(files):
    return scan.scan_diff(diff_of(files))


def test_loosened_assertion_python_unittest_and_bare_assert():
    old = textwrap.dedent("""\
        class T(unittest.TestCase):
            def test_total(self):
                self.assertEqual(total(3), 12)
        """)
    found = hits_for({"tests/test_cart.py": (old, old.replace("self.assertEqual(total(3), 12)", "self.assertTrue(total(3))"))})
    hit = next(h for h in found if h["rule"] == "loosened-assertion")
    assert hit["path"] == "tests/test_cart.py" and hit["line"] == 3 and hit["language"] == "python"
    assert hit["removed"] == ["self.assertEqual(total(3), 12)"] and hit["added"] == ["self.assertTrue(total(3))"]
    assert hit["hunk"].startswith("@@ ")
    bare = "def test_total():\n    assert total(3) == 12\n"
    found = hits_for({"tests/test_cart.py": (bare, bare.replace("assert total(3) == 12", "assert total(3)"))})
    assert ("loosened-assertion", "tests/test_cart.py") in rules(found)


@pytest.mark.parametrize("old,new,path,lang", [
    ("  expect(sum(1, 2)).toBe(3);\n", "  expect(sum(1, 2)).toBeTruthy();\n", "src/sum.test.js", "javascript"),
    ("  expect(user).toEqual({id: 1});\n", "  expect(user).toBeDefined();\n", "src/user.spec.ts", "typescript"),
    ("        assertEquals(12, cart.total());\n", "        assertNotNull(cart.total());\n", "src/test/java/CartTest.java", "java"),
])
def test_loosened_assertion_other_languages(old, new, path, lang):
    found = hits_for({path: (old, new)})
    hit = next(h for h in found if h["rule"] == "loosened-assertion")
    assert hit["language"] == lang and hit["path"] == path


def test_a_strengthened_assertion_is_not_a_hit():
    old = "def test_total():\n    assert total(3)\n"
    assert not [h for h in hits_for({"tests/test_cart.py": (old, old.replace("assert total(3)", "assert total(3) == 12"))})
                if h["rule"] == "loosened-assertion"]


def test_an_assertion_removed_without_a_replacement():
    old = "def test_total():\n    value = total(3)\n    assert value == 12\n    assert value > 0\n"
    found = hits_for({"tests/test_cart.py": (old, "def test_total():\n    value = total(3)\n    assert value > 0\n")})
    assert ("assertion-removed", "tests/test_cart.py") in rules(found)


@pytest.mark.parametrize("path,old,new", [
    ("tests/test_a.py", "def test_x():\n    assert f() == 1\n\n\ndef test_y():\n    assert g() == 2\n", "def test_x():\n    assert f() == 1\n"),
    ("src/a.test.js", "it('adds', () => { expect(f()).toBe(1); });\nit('subs', () => { expect(g()).toBe(2); });\n",
     "it('adds', () => { expect(f()).toBe(1); });\n"),
    ("pkg/a_test.go", "func TestAdd(t *testing.T) {}\nfunc TestSub(t *testing.T) {}\n", "func TestAdd(t *testing.T) {}\n"),
])
def test_deleted_test(path, old, new):
    assert ("deleted-test", path) in rules(hits_for({path: (old, new)}))


def test_a_renamed_test_is_not_a_deleted_one():
    old = "def test_x():\n    assert f() == 1\n"
    assert not [h for h in hits_for({"tests/test_a.py": (old, old.replace("1\n", "1  # same\n"))}) if h["rule"] == "deleted-test"]


@pytest.mark.parametrize("path,line", [
    ("tests/test_a.py", "@pytest.mark.skip(reason='later')"), ("tests/test_a.py", "    pytest.skip('flaky')"),
    ("tests/test_a.py", "@pytest.mark.xfail"), ("tests/test_a.py", "@unittest.skip('later')"),
    ("tests/test_a.py", "        self.skipTest('later')"), ("src/a.test.js", "it.skip('adds', () => {});"),
    ("src/a.test.js", "describe.only('cart', () => {});"), ("src/a.spec.ts", "xit('adds', () => {});"),
    ("src/test/java/ATest.java", "    @Disabled"), ("src/test/java/ATest.java", "    @Ignore"), ("pkg/a_test.go", "\tt.Skip(\"later\")"),
])
def test_skip_only_xfail_added(path, line):
    old = "x = 1\n"
    assert ("skip-added", path) in rules(hits_for({path: (old, old + line + "\n")}))


def test_tolerance_widened_and_narrowed():
    old = "def test_ratio():\n    assert ratio() == pytest.approx(0.5, abs=0.001)\n"
    wide = hits_for({"tests/test_r.py": (old, old.replace("abs=0.001", "abs=0.1"))})
    assert ("tolerance-widened", "tests/test_r.py") in rules(wide)
    narrow = hits_for({"tests/test_r.py": (old, old.replace("abs=0.001", "abs=0.0001"))})
    assert not [h for h in narrow if h["rule"] == "tolerance-widened"]
    places = "        self.assertAlmostEqual(ratio(), 0.5, places=4)\n"
    assert ("tolerance-widened", "tests/test_r.py") in rules(hits_for({"tests/test_r.py": (places, places.replace("places=4", "places=1"))}))


def test_snapshot_rewritten():
    assert ("snapshot-rewritten", "src/__snapshots__/card.test.js.snap") in rules(
        hits_for({"src/__snapshots__/card.test.js.snap": ("exports[`a`] = `1`;\n", "exports[`a`] = `2`;\n")}))


@pytest.mark.parametrize("path,old,new", [
    (".coveragerc", "[report]\nfail_under = 90\n", "[report]\nfail_under = 60\n"),
    ("jest.config.js", "  coverageThreshold: { global: { lines: 85 } },\n", "  coverageThreshold: { global: { lines: 40 } },\n"),
    ("setup.cfg", "addopts = --cov-fail-under=80\n", "addopts = --cov-fail-under=50\n"),
])
def test_coverage_threshold_lowered(path, old, new):
    assert ("coverage-threshold-lowered", path) in rules(hits_for({path: (old, new)}))
    assert not [h for h in hits_for({path: (new, old)}) if h["rule"] == "coverage-threshold-lowered"]


@pytest.mark.parametrize("path,line", [
    (".github/workflows/ci.yml", "        continue-on-error: true"), (".github/workflows/ci.yml", "      - run: npm test || true"),
    (".gitlab-ci.yml", "  allow_failure: true"),
])
def test_ci_failure_ignored(path, line):
    old = "jobs:\n"
    assert ("ci-failure-ignored", path) in rules(hits_for({path: (old, old + line + "\n")}))


@pytest.mark.parametrize("path,line", [
    ("pytest.ini", "addopts = --ignore=tests/test_slow.py"), ("pytest.ini", "addopts = --deselect tests/test_a.py::test_x"),
    ("conftest.py", "collect_ignore = ['tests/test_slow.py']"), ("jest.config.js", "  testPathIgnorePatterns: ['/slow/'],"),
    ("pyproject.toml", "addopts = \"-k 'not slow'\""),
])
def test_tests_excluded_in_configuration(path, line):
    old = "[pytest]\n"
    assert ("tests-excluded", path) in rules(hits_for({path: (old, old + line + "\n")}))


def test_runner_hook_that_rewrites_outcomes():
    new = textwrap.dedent("""\
        import pytest


        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(item, call):
            res = yield
            rep = res.get_result()
            rep.outcome = "passed"
        """)
    found = hits_for({"tests/conftest.py": (None, new)})
    hooks = [h for h in found if h["rule"] == "runner-hook"]
    assert hooks and hooks[0]["path"] == "tests/conftest.py" and {h["line"] for h in hooks} >= {5, 8}
    reporter = hits_for({"jest.config.js": ("module.exports = {\n};\n", "module.exports = {\n  reporters: ['./quiet-reporter.js'],\n};\n")})
    assert ("runner-hook", "jest.config.js") in rules(reporter)


@pytest.mark.parametrize("path,line", [
    ("tests/conftest.py", "    os._exit(0)"), ("tests/test_a.py", "sys.exit(0)"), ("test/setup.js", "process.exit(0);"),
    ("src/test/java/ATest.java", "        System.exit(0);"),
])
def test_forced_exit(path, line):
    old = "import os\n"
    assert ("forced-exit", path) in rules(hits_for({path: (old, old + line + "\n")}))


def test_forced_exit_in_product_code_is_not_this_rule():
    assert not [h for h in hits_for({"app/cli.py": ("import sys\n", "import sys\nsys.exit(0)\n")}) if h["rule"] == "forced-exit"]


def test_equality_overridden():
    new = "class Money:\n    def __eq__(self, other):\n        return True\n"
    hit = next(h for h in hits_for({"app/money.py": ("class Money:\n    pass\n", new)}) if h["rule"] == "equality-overridden")
    assert hit["line"] == 2 and "always true" in hit["detail"]
    java = "    @Override\n    public boolean equals(Object o) { return true; }\n"
    assert ("equality-overridden", "src/main/java/Money.java") in rules(hits_for({"src/main/java/Money.java": ("class Money {\n", "class Money {\n" + java)}))


@pytest.mark.parametrize("path,line", [
    ("app/rates.py", "    if 'PYTEST_CURRENT_TEST' in os.environ:"), ("app/rates.py", "    if \"pytest\" in sys.modules:"),
    ("src/rates.js", "  if (process.env.NODE_ENV === 'test') return 0;"), ("src/rates.ts", "  if (process.env.JEST_WORKER_ID) {"),
])
def test_product_code_that_detects_tests(path, line):
    old = "def rate():\n"
    assert ("test-detection-in-product", path) in rules(hits_for({path: (old, old + line + "\n")}))
    assert not [h for h in hits_for({"tests/test_x.py": (old, old + line + "\n")}) if h["rule"] == "test-detection-in-product"]


def test_special_cased_input_needs_the_literal_in_an_added_test():
    product = ("def shipping(weight):\n    if weight > 50:\n        return 9\n    return 4\n",
               "def shipping(weight):\n    if weight == 37:\n        return 9\n    if weight > 50:\n        return 9\n    return 4\n")
    test = ("def test_light():\n    assert shipping(1) == 4\n",
            "def test_light():\n    assert shipping(1) == 4\n\n\ndef test_37_is_heavy():\n    assert shipping(37) == 9\n")
    hit = next(h for h in hits_for({"app/shipping.py": product, "tests/test_shipping.py": test}) if h["rule"] == "special-cased-input")
    assert hit["path"] == "app/shipping.py" and hit["line"] == 2 and "37" in hit["detail"] and "tests/test_shipping.py" in hit["detail"]
    assert not [h for h in hits_for({"app/shipping.py": product}) if h["rule"] == "special-cased-input"]


def test_the_output_is_sorted_and_the_same_bytes_every_time():
    files = {"tests/test_b.py": ("def test_x():\n    assert f() == 1\n", "def test_x():\n    assert f()\n"),
             "tests/test_a.py": ("x = 1\n", "x = 1\n" + "@pytest.mark.skip\n")}
    first, second = hits_for(files), hits_for(files)
    assert first == second and [h["path"] for h in first] == sorted(h["path"] for h in first)


# --------------------------------------------------------------------------- the scan on F7

def test_the_scan_finds_each_f7_integrity_change_and_nothing_in_the_others(tmp_path):
    root, c = history(tmp_path)
    found = {}
    for n in range(1, 6):
        code, out, err = run("test_weakening_scan.py", "--base", c[n - 1], "--head", c[n], cwd=root)
        assert code == 0 and out["ok"] is True, err
        assert out["base"] == c[n - 1] and out["head"] == c[n]
        found[n] = out["hits"]
    weak = [h for h in found[1] if h["rule"] == "loosened-assertion"]
    assert [(h["path"], h["line"]) for h in weak] == [("tests/test_pricing.py", 17)]
    assert "assertTrue" in weak[0]["added"][0] and "assertEqual" in weak[0]["removed"][0]
    assert found[2] == [] and found[3] == []                      # a rename, then a strengthened assertion
    special = [h for h in found[4] if h["rule"] == "special-cased-input"]
    assert [(h["path"], h["line"]) for h in special] == [("orders/pricing.py", 11)] and "22" in special[0]["detail"]
    hooks = [h for h in found[5] if h["rule"] == "runner-hook"]
    assert hooks and {h["path"] for h in hooks} == {"conftest.py"}


def test_the_scan_refuses_an_unknown_revision_and_reads_a_saved_diff(tmp_path):
    root, c = history(tmp_path, checkout=1)
    code, out, _ = run("test_weakening_scan.py", "--base", "nosuchrev", "--head", "HEAD", cwd=root)
    assert code == 2 and out["ok"] is False and out["error"]["code"] == "unknown_revision"
    saved = tmp_path / "change.diff"
    saved.write_text(subprocess.run(["git", "-C", str(root), "diff", c[0], c[1]], capture_output=True, text=True, check=True).stdout)
    code, out, _ = run("test_weakening_scan.py", "--diff", saved, cwd=tmp_path)
    assert code == 0 and ("loosened-assertion", "tests/test_pricing.py") in rules(out["hits"])


# --------------------------------------------------------------------------- the impact of a change

def test_impact_maps_a_product_change_to_the_tests_that_reach_it(tmp_path):
    root, c = history(tmp_path, checkout=1)
    code, out, err = run("diff_impact.py", "--base", c[0], "--head", c[1], cwd=root)
    assert code == 0 and out["ok"] is True, err
    kinds = {f["path"]: f["kind"] for f in out["changed"]}
    assert kinds == {"orders/pricing.py": "product", "orders/service.py": "product", "tests/test_pricing.py": "test",
                     "tests/test_service.py": "test"}
    impacted = {t["path"]: t["why"] for t in out["impacted_tests"]}
    assert set(impacted) == {"tests/test_pricing.py", "tests/test_service.py"}
    assert any("orders.pricing" in why for why in impacted["tests/test_service.py"])     # through orders.service
    assert out["contracts"] == [] and out["selection"]["full_suite_recommended"] in (True, False)


def test_impact_names_a_changed_contract_and_its_tool(tmp_path):
    root, c = history(tmp_path, checkout=2)
    code, out, _ = run("diff_impact.py", "--base", c[1], "--head", c[2], cwd=root)
    assert code == 0 and [(x["path"], x["kind"], x["tool"]) for x in out["contracts"]] == [("openapi.yaml", "openapi", "oasdiff")]


def test_impact_recommends_the_full_suite_when_test_configuration_changes(tmp_path):
    root, c = history(tmp_path)
    code, out, _ = run("diff_impact.py", "--base", c[4], "--head", c[5], cwd=root)
    assert code == 0 and out["selection"]["full_suite_recommended"] is True and "conftest.py" in out["selection"]["reason"]


def test_impact_reverse_dependencies_on_a_small_javascript_tree():
    graph = impact.reverse_closure({"src/a.js": {"src/b.js"}, "src/b.js": {"src/c.js"}, "src/c.js": set()}, {"src/c.js"})
    assert graph == {"src/a.js", "src/b.js", "src/c.js"}
    assert impact.js_imports("import x from './b';\nconst y = require('../lib/c.js');\n", "src/a.js",
                             {"src/b.js", "lib/c.js"}) == {"src/b.js", "lib/c.js"}


# --------------------------------------------------------------------------- FAIL_TO_PASS selection

def selection_state():
    results = {}
    rows = [("EXE-B", ORDER_TOTAL, "failed"), ("EXE-B", "t.a::ok", "passed"), ("EXE-B", "t.a::other_bug", "failed"),
            ("EXE-H", ORDER_TOTAL, "passed"), ("EXE-H", "t.a::ok", "passed"), ("EXE-H", "t.a::other_bug", "passed"),
            ("EXE-H", "t.a::new", "passed")]
    for n, (exe, test, status) in enumerate(rows):
        results["RES-%d" % n] = {"execution": exe, "test_id": test, "status": status, "failure_kind": "assertion" if status == "failed" else None}
    return {"results": results, "executions": {"EXE-B": {"git": {"commit": "b" * 40}}, "EXE-H": {"git": {"commit": "h" * 40}}},
            "fixes": {"FIX-001": {"findings": ["FND-001"], "base_commit": "b" * 40, "fix_commit": "h" * 40}},
            "findings": {"FND-001": {"key_attributes": {"test": ORDER_TOTAL}, "evidence": [], "description": "d"}}}


def test_the_selection_prefers_the_tests_the_finding_names():
    out = f2p.select(selection_state(), "FIX-001", "EXE-B", "EXE-H", changed_tests={"t.a::other_bug"})
    assert out["defect_tests"] == [ORDER_TOTAL] and out["source"] == "named by the fix's findings" and out["status"] == "proposed"
    assert {"test": "t.a::other_bug", "base": "failed", "head": "passed"} in out["other_base_failures"]


@pytest.mark.parametrize("changed", [{"t.a::other_bug", "t.a::new"}, set()])
def test_without_a_named_test_nothing_is_proposed_and_the_fix_is_unverified(changed):
    """A base failure the fix changed, or any base failure, is not evidence about the finding: no fallback picks one."""
    st = selection_state()
    st["findings"]["FND-001"]["key_attributes"] = {}
    out = f2p.select(st, "FIX-001", "EXE-B", "EXE-H", changed_tests=changed)
    assert out["status"] == "unverified" and out["defect_tests"] == [] and out["command"] is None and out["source"] is None
    assert "unverified" in out["note"] and "key_attributes.test" in out["note"]
    assert any("name no recorded test" in w for w in out["warnings"])
    assert {o["test"] for o in out["other_base_failures"]} == {ORDER_TOTAL, "t.a::other_bug"}
    if changed:
        assert {"test": "t.a::new", "why": "absent from the base run"} in out["not_proposed"]


def test_a_named_test_that_did_not_fail_at_the_base_leaves_the_fix_unverified_even_with_other_base_failures():
    st = selection_state()
    st["findings"]["FND-001"]["key_attributes"] = {"test": "t.a::ok"}
    out = f2p.select(st, "FIX-001", "EXE-B", "EXE-H", changed_tests={"t.a::other_bug"})
    assert out["status"] == "unverified" and out["command"] is None and out["defect_tests"] == []
    assert {"test": "t.a::ok", "why": "did not fail on the base run (passed), so it cannot show the defect"} in out["not_proposed"]


def test_the_selection_prints_the_kernels_command_and_no_outcome():
    out = f2p.select(selection_state(), "FIX-001", "EXE-B", "EXE-H", changed_tests=set(), launcher=".claude/skills/qa-regression-guard/scripts/qa.py")
    assert out["command"] == ("python3 .claude/skills/qa-regression-guard/scripts/qa.py --role verifier --skill qa-regression-guard "
                              "revalidate FIX-001 --f2p EXE-B:EXE-H --p2p EXE-B:EXE-H --defect-test " + ORDER_TOTAL)
    assert "outcome" not in out and "passed" not in json.dumps({k: v for k, v in out.items() if k not in ("other_base_failures",)})


def test_the_selection_warns_about_commits_the_kernel_will_refuse_and_proposes_nothing_without_a_failing_test():
    st = selection_state()
    st["executions"]["EXE-B"]["git"]["commit"] = "x" * 40
    assert any("base commit" in w for w in f2p.select(st, "FIX-001", "EXE-B", "EXE-H", changed_tests=set())["warnings"])
    st = selection_state()
    for r in st["results"].values():
        r["status"], r["failure_kind"] = "passed", None
    st["findings"]["FND-001"]["key_attributes"] = {}
    out = f2p.select(st, "FIX-001", "EXE-B", "EXE-H", changed_tests=set())
    assert out["defect_tests"] == [] and out["command"] is None and out["status"] == "unverified"


@pytest.mark.parametrize("named, code", [(ORDER_TOTAL, 0), (None, 1)])
def test_the_command_line_exits_one_and_prints_no_command_when_the_fix_is_unverified(monkeypatch, capsys, named, code):
    st = selection_state()
    st["findings"]["FND-001"]["key_attributes"] = {"test": named} if named else {}

    def no_repo(_root):
        raise f2p.ChangeError("git_unavailable", "not here")

    monkeypatch.setattr(f2p, "read_state", lambda _launcher: st)
    monkeypatch.setattr(f2p._change, "Repo", no_repo)
    assert f2p.main(["--fix", "FIX-001", "--base-run", "EXE-B", "--head-run", "EXE-H"]) == code
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["status"] == ("proposed" if named else "unverified")
    assert (out["command"] is None) is (named is None)
    assert any(isinstance(v, str) and v.startswith("python3 ") for v in out.values()) is (named is not None)


def _f7_two_runs(tmp_path):
    """The f7 history with its finding and fix recorded and a run at the base and at the fix commit: ``(root, commits, runs, tree, qa)``."""
    root, c = history(tmp_path, checkout=3)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    launcher = SCRIPTS / "qa.py" if (SCRIPTS / "qa.py").is_file() else REPO / "shared" / "launcher" / "qa.py"

    def qa(*args, cwd=root):
        done = subprocess.run([sys.executable, "-I", str(launcher), "--role", "verifier", "--skill", "qa-regression-guard"] + list(args),
                              cwd=str(cwd), capture_output=True, text=True, timeout=300, env=env)
        return json.loads(done.stdout)

    qa("init")
    qa("config", "set", "project.trust", "trusted", "--approval", "test")
    (tmp_path / "f.json").write_text(json.dumps(base(title="Order total one cent short", category="functional",
                                                     severity_rationale={"anchor": "FUNC-C3", "modifiers": []},
                                                     component={"path": "orders/pricing.py", "criticality": "high"},
                                                     refs={"rule": "manual", "location": "orders/pricing.py"},
                                                     key_attributes={"test": ORDER_TOTAL})))
    assert qa("finding", "add", "--json", str(tmp_path / "f.json"))["id"] == "FND-001"
    (tmp_path / "fix.json").write_text(json.dumps({"findings": ["FND-001"], "base_commit": c[2], "fix_commit": c[3], "description": "d"}))
    assert qa("fix", "add", "--json", str(tmp_path / "fix.json"))["id"] == "FIX-001"
    runs = []
    for commit in (c[2], c[3]):
        tree = qa("worktree", "add", commit)["path"]
        runs.append(qa("run", "--report", "junit:{evidence}/junit.xml", "--", sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                       "-o", "junit_family=xunit2", "--junitxml={evidence}/junit.xml", cwd=root / tree)["execution"])
    return root, c, runs, tree, qa


def test_fail_to_pass_end_to_end_on_the_f7_fix(tmp_path):
    root, c, runs, _tree, qa = _f7_two_runs(tmp_path)
    code, out, err = run("fail_to_pass.py", "--fix", "FIX-001", "--base-run", runs[0], "--head-run", runs[1], cwd=root)
    assert code == 0 and out["ok"] is True and out["status"] == "proposed", err
    assert out["defect_tests"] == [ORDER_TOTAL] and out["fix_commits"] == {"base": c[2], "fix": c[3]}
    assert "tests.test_pricing.PricingTests::test_discount_on_a_larger_odd_total" in [n["test"] for n in out["not_proposed"]]
    words = out["command"].split()
    assert words[0] == "python3" and words[1].endswith("qa.py") and words[2:6] == ["--role", "verifier", "--skill", "qa-regression-guard"]
    reply = qa(*words[words.index("revalidate"):])
    assert reply["outcome"] == "passed", reply                 # the kernel decides; the script only proposed


def test_printed_command_is_root_relative_from_a_worktree(tmp_path):
    root, _c, runs, tree, _qa = _f7_two_runs(tmp_path)
    code, out, err = run("fail_to_pass.py", "--fix", "FIX-001", "--base-run", runs[0], "--head-run", runs[1], cwd=root / tree)
    assert code == 0 and out["status"] == "proposed", err
    assert out["run_from"] == "Run from the project root:"
    assert out["command"].startswith("python3 .claude/skills/qa-regression-guard/scripts/qa.py ")
    printed = json.dumps(out)
    assert str(root) not in printed and str(SCRIPTS) not in printed
