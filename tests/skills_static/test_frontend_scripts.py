"""The bundled scripts and assets of qa-frontend: ``static_ui_scan.py``, ``run_inputs.py``, ``wcag_table.py`` and the runner.

Each script runs as the skill runs it, isolated (``python3 -I -B``), from a project root made at test time (a copy of F1, or a
small site written here). The browser assets are JavaScript that runs only in the pinned Playwright image; here they are
checked as text, and ``tests/integration/test_fixture_f1.py`` runs them.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest

from helpers import FIXTURES, REPO

SKILL = REPO / "skills" / "qa-frontend"
SCAN = SKILL / "scripts" / "static_ui_scan.py"
INPUTS = SKILL / "scripts" / "run_inputs.py"
TABLE = SKILL / "scripts" / "wcag_table.py"
ASSETS = SKILL / "assets"
PRODUCER = REPO / "tests" / "corpus" / "producers" / "frontend-runner"
AXE_SAMPLE = REPO / "tests" / "corpus" / "axe" / "axe-core-4.14.0-fixture-page.json"
F1 = FIXTURES / "web-frontend"
EXE = "EXE-01ABCDEFGHJKMNPQRSTVWXYZ00"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(script, *args, cwd):
    done = subprocess.run([sys.executable, "-I", "-B", str(script)] + [str(a) for a in args], capture_output=True, text=True,
                          timeout=120, cwd=str(cwd), env={"PATH": os.environ["PATH"], "HOME": str(cwd)})
    return done.returncode, (json.loads(done.stdout) if done.stdout.strip() else None), done.stderr


def f1_copy(tmp_path):
    root = tmp_path / "f1"
    shutil.copytree(str(F1), str(root), ignore=shutil.ignore_patterns("__pycache__"))
    return root


def site(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def located(summary):
    return sorted((item["rule"], item["at"]) for item in summary["listed"])


# ----------------------------------------------------------------------------------------------- static_ui_scan.py

def test_static_scan_finds_exactly_the_four_seeds_of_f1_by_path_and_line(tmp_path):
    root = f1_copy(tmp_path)
    code, out, err = run(SCAN, "--root", "site", "--out", ".qa/work/frontend/static-ui.sarif", cwd=root)
    assert (code, out["ok"], out["approximate"]) == (0, True, True), err
    assert located(out) == [("click-handler-on-non-interactive", "site/index.html:22"), ("email-input-not-typed", "site/index.html:38"),
                            ("fixed-width-over-375", "site/styles.css:33"), ("img-without-alt", "site/index.html:19")], out
    assert out["files"] == {"css": 1, "html": 2} and out["unreadable"] == [] and out["out"] == ".qa/work/frontend/static-ui.sarif"
    by_rule = {item["rule"]: item for item in out["listed"]}
    assert by_rule["img-without-alt"]["wcag"] == ["1.1.1"]
    assert by_rule["click-handler-on-non-interactive"]["wcag"] == ["2.1.1", "4.1.2"]
    assert by_rule["fixed-width-over-375"]["wcag"] == ["1.4.10"] and "480px" in by_rule["fixed-width-over-375"]["message"]
    assert (by_rule["email-input-not-typed"]["cwe"], by_rule["email-input-not-typed"]["wcag_related"]) == (["CWE-20"], ["3.3.1"])
    report = json.loads((root / ".qa" / "work" / "frontend" / "static-ui.sarif").read_text(encoding="utf-8"))
    assert report["version"] == "2.1.0" and report["runs"][0]["tool"]["driver"]["name"] == "qa-frontend-static-ui-scan"
    results = report["runs"][0]["results"]
    assert len(results) == 4 and all(r["properties"]["approximate"] is True and "approximate" in r["message"]["text"] for r in results)
    assert out["sha256"].startswith("sha256:")
    # the scan wrote nothing else and changed no project file
    changed = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and ".qa" not in p.parts
               and (F1 / p.relative_to(root)).read_bytes() != p.read_bytes()]
    assert changed == []


def test_static_scan_without_out_writes_nothing(tmp_path):
    root = f1_copy(tmp_path)
    code, out, err = run(SCAN, "--root", "site", cwd=root)
    assert (code, out["results"], out["out"], out["sha256"]) == (0, 4, None, None), err
    assert not (root / ".qa").exists()


GOOD = """<!doctype html><html lang="en"><body>
<img src="a.png" alt="">
<img src="b.png" alt="Logo">
<img src="c.png" aria-hidden="true">
<img src="d.png" role="presentation">
<button onclick="go()">Go</button>
<a href="#x" onclick="go()">Go</a>
<div role="button" tabindex="0" onclick="go()">Go</div>
<label>Name <input name="name"></label>
<label for="q">Search</label><input id="q" type="search">
<input aria-label="Phone" type="tel">
<input type="email" name="email" aria-label="Email">
<input type="hidden" name="email_token">
<input type="submit" value="Send">
<select aria-labelledby="x"><option>1</option></select>
<div style="max-width: 900px; width: calc(100% - 900px)"></div>
<style>
/* .wide { width: 900px; } */
@media (min-width: 768px) { .wide { width: 900px; } }
@media print { .page { width: 8in; } }
.narrow { width: 20rem; }
</style>
</body></html>
"""

BAD = """<!doctype html><html lang="en"><body>
<img src="a.png">
<span onclick="go()">Go</span>
<div tabindex="0" onclick="go()">Go</div>
<input name="city" placeholder="City">
<textarea name="notes"></textarea>
<input type="text" id="contact-email">
<p style="width: 30rem">wide</p>
<style>
.a { color: red; }
.b {
  min-width: 400px;
}
</style>
</body></html>
"""


def test_static_scan_rules_pass_the_accessible_forms_and_flag_the_others(tmp_path):
    root = site(tmp_path / "p", {"web/good.html": GOOD, "web/bad.html": BAD, "web/main.css": "a { width: 100%; }\n.x { width: 376px; }\n"})
    code, out, err = run(SCAN, "--root", "web", cwd=root)
    assert code == 0, err
    assert located(out) == [
        ("click-handler-on-non-interactive", "web/bad.html:3"), ("click-handler-on-non-interactive", "web/bad.html:4"),
        ("email-input-not-typed", "web/bad.html:7"), ("fixed-width-over-375", "web/bad.html:12"), ("fixed-width-over-375", "web/bad.html:8"),
        ("fixed-width-over-375", "web/main.css:2"), ("img-without-alt", "web/bad.html:2"), ("input-without-label", "web/bad.html:5"),
        ("input-without-label", "web/bad.html:6"), ("input-without-label", "web/bad.html:7")], out
    listed = {(item["rule"], item["at"]): item for item in out["listed"]}
    assert listed[("click-handler-on-non-interactive", "web/bad.html:3")]["wcag"] == ["2.1.1", "4.1.2"]
    assert listed[("click-handler-on-non-interactive", "web/bad.html:4")]["wcag"] == ["4.1.2"]   # reachable, but no role
    assert "placeholder" in listed[("input-without-label", "web/bad.html:5")]["message"]
    assert "30rem" in listed[("fixed-width-over-375", "web/bad.html:8")]["message"]


def test_static_scan_lists_links_large_and_non_utf8_files_as_unreadable(tmp_path):
    root = site(tmp_path / "p", {"web/index.html": "<img src=x>\n"})
    outside = site(tmp_path / "outside", {"secret.html": "<img src=y>\n"})
    os.symlink(str(outside / "secret.html"), str(root / "web" / "linked.html"))
    os.symlink(str(outside), str(root / "web" / "linked-dir"))
    (root / "web" / "big.css").write_bytes(b"a { width: 900px; }\n" + b" " * (1024 * 1024))
    (root / "web" / "latin.html").write_bytes(b"<p>caf\xe9</p><img src=z>\n")
    for skipped in ("node_modules", ".qa", ".git"):
        site(root, {"web/%s/x.html" % skipped: "<img src=q>\n"})
    code, out, err = run(SCAN, "--root", "web", cwd=root)
    assert code == 0, err
    assert located(out) == [("img-without-alt", "web/index.html:1")]
    assert sorted((u["path"], u["reason"]) for u in out["unreadable"]) == [
        ("web/big.css", "over 1 MiB"), ("web/latin.html", "not UTF-8"), ("web/linked-dir", "a link, not followed"),
        ("web/linked.html", "a link, not followed")]


@pytest.mark.parametrize("out", ["/tmp/static-ui.sarif", "site/static-ui.sarif", ".qa/work/frontend/../x.sarif", ".qa/work/other/x.sarif",
                                 ".qa/work/frontend", "/qa-evidence/x/y.sarif"])
def test_static_scan_refuses_an_out_outside_its_directories(tmp_path, out):
    root = f1_copy(tmp_path)
    code, summary, err = run(SCAN, "--root", "site", "--out", out, cwd=root)
    assert (code, summary["ok"]) == (2, False), (summary, err)
    assert not (root / "site" / "static-ui.sarif").exists() and not (root / ".qa" / "work" / "other").exists()


def test_static_scan_refuses_a_link_on_the_way_and_a_root_that_is_a_link(tmp_path):
    root = f1_copy(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / ".qa" / "work").mkdir(parents=True)
    os.symlink(str(elsewhere), str(root / ".qa" / "work" / "frontend"))
    code, out, _ = run(SCAN, "--root", "site", "--out", ".qa/work/frontend/x.sarif", cwd=root)
    assert (code, out["ok"]) == (2, False) and list(elsewhere.iterdir()) == []
    os.symlink(str(root / "site"), str(root / "site-link"))
    code, out, _ = run(SCAN, "--root", "site-link", cwd=root)
    assert (code, out["ok"]) == (2, False) and "link" in out["error"]


def test_static_scan_copies_itself_to_work_for_a_container_run(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    code, out, err = run(SCAN, "--copy-to-work", cwd=root)
    assert (code, out["written"], out["input"]) == (0, ".qa/work/frontend/static_ui_scan.py", "/work/.qa-input/frontend/static_ui_scan.py"), err
    assert (root / ".qa" / "work" / "frontend" / "static_ui_scan.py").read_bytes() == SCAN.read_bytes()
    assert run(SCAN, "--copy-to-work", "--root", "x", cwd=root)[0] == 2


# ----------------------------------------------------------------------------------------------- run_inputs.py

def test_the_runner_assets_are_byte_equal_to_the_recorded_lockfile_producer():
    for name in ("package.json", "package-lock.json"):
        assert (ASSETS / "runner" / name).read_bytes() == (PRODUCER / name).read_bytes(), name


@pytest.mark.parametrize("kind,names", [
    ("runner", ["package-lock.json", "package.json"]),
    ("lighthouse", ["package-lock.json", "package.json"]),
    ("checks", ["checks.spec.mjs", "package-lock.json", "package.json", "pages.json", "playwright.config.mjs"]),
    ("axe", ["axe.spec.mjs", "package-lock.json", "package.json", "pages.json", "playwright.config.mjs"]),
])
def test_run_inputs_writes_each_kind_from_the_assets(tmp_path, kind, names):
    root = tmp_path / "p"
    root.mkdir()
    code, out, err = run(INPUTS, "--kind", kind, cwd=root)
    assert (code, out["ok"], out["kind"]) == (0, True, kind), err
    assert out["inputs"] == [".qa/work/frontend/" + n for n in names]
    assert out["install"] == "cd /work/.qa-input/frontend && npm ci --ignore-scripts --no-audit --no-fund"
    work = root / ".qa" / "work" / "frontend"
    assert sorted(p.name for p in work.iterdir()) == names
    for name in names:
        if name == "pages.json":
            assert json.loads((work / name).read_text()) == {"pages": ["/"], "states": {
                "routes": [], "loading": None, "list": None, "empty": None, "error": None}}
        else:
            source = ASSETS / "runner" / name if name.startswith("package") else ASSETS / name
            assert (work / name).read_bytes() == source.read_bytes(), name
    assert run(INPUTS, "--kind", kind, cwd=root)[0] == 0                        # a second call rewrites the same files


def test_run_inputs_writes_the_pages_and_states_given(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    code, out, err = run(INPUTS, "--kind", "checks", "--page", "/", "--page", "/about.html", "--state-route", "/api/products",
                         "--loading", "#loading", "--list", "#product-list", cwd=root)
    assert code == 0, err
    pages = json.loads((root / ".qa" / "work" / "frontend" / "pages.json").read_text())
    assert pages == out["pages"] == {"pages": ["/", "/about.html"], "states": {
        "routes": ["/api/products"], "loading": "#loading", "list": "#product-list", "empty": None, "error": None}}


@pytest.mark.parametrize("args", [
    ["--kind", "checks", "--page", "//elsewhere.example/x"], ["--kind", "checks", "--page", "http://elsewhere.example/"],
    ["--kind", "axe", "--page", "about.html"], ["--kind", "checks", "--page", "/a/../b"], ["--kind", "checks", "--page", "/a\\b"],
    ["--kind", "checks", "--page", "/", "--page", "/"], ["--kind", "runner", "--page", "/"], ["--kind", "lighthouse", "--state-route", "/api"],
    ["--kind", "checks", "--loading", "#loading"], ["--kind", "axe", "--state-route", "/api/products"],
    ["--kind", "checks", "--state-route", "https://elsewhere.example/api"],
])
def test_run_inputs_refuses_a_page_outside_the_base_url_and_misplaced_options(tmp_path, args):
    root = tmp_path / "p"
    root.mkdir()
    code, out, err = run(INPUTS, *args, cwd=root)
    assert (code, out["ok"]) == (2, False), (out, err)
    assert not (root / ".qa").exists()


def test_run_inputs_writes_through_no_link(tmp_path):
    root = tmp_path / "p"
    elsewhere = tmp_path / "elsewhere"
    (root / ".qa" / "work").mkdir(parents=True)
    elsewhere.mkdir()
    os.symlink(str(elsewhere), str(root / ".qa" / "work" / "frontend"))
    code, out, _ = run(INPUTS, "--kind", "runner", cwd=root)
    assert (code, out["ok"]) == (2, False) and list(elsewhere.iterdir()) == []


# ----------------------------------------------------------------------------------------------- the browser assets, as text

def test_the_runner_config_refuses_any_base_url_but_loopback_before_anything_else():
    text = (ASSETS / "playwright.config.mjs").read_text(encoding="utf-8")
    assert "import " not in text.split("export default")[0].replace("// It imports nothing", "")
    assert "new Set(['127.0.0.1', '[::1]', 'localhost'])" in text and "url.protocol !== 'http:'" in text
    assert text.index("process.exit(2)") < text.index("export default")
    assert "QA_BASE_URL" in text and "console.error('qa-frontend: refused" in text and "${" not in text.split("process.exit(2)")[0]
    for viewport in ("width: 375, height: 812", "width: 768, height: 1024", "width: 1280, height: 800"):
        assert viewport in text
    assert "retries: 0" in text and "'--disable-dev-shm-usage', '--no-sandbox'" in text and "QA_JUNIT_OUT" in text


def test_the_specs_use_the_wcag_tags_and_name_no_host():
    axe = (ASSETS / "axe.spec.mjs").read_text(encoding="utf-8")
    checks = (ASSETS / "checks.spec.mjs").read_text(encoding="utf-8")
    assert "const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'];" in axe and "QA_AXE_OUT" in axe and "flag: 'wx'" in axe
    for text in (axe, checks):
        assert "url.origin !== base.origin" in text and "http://" not in text and "https://" not in text and "npx" not in text
    for check in ("pageerror", "requestfailed", "scrollWidth", "keyboard.press('Tab')", "status: 500", "body: '[]'", "checkValidity",
                  "annotations.push", "sha256"):
        assert check in checks, check


# ----------------------------------------------------------------------------------------------- wcag_table.py

def grader():
    return load(SKILL / "evals" / "grade.py", "qa_frontend_grade_for_scripts")


def test_the_table_agrees_with_the_graders_fixed_definitions():
    table = load(TABLE, "qa_frontend_wcag_table")
    grade = grader()
    assert list(table.WCAG22) == list(grade.WCAG22) and {sc: level for sc, (level, _) in table.WCAG22.items()} == grade.WCAG22
    assert table.AXE_DECIDABLE == grade.AXE_DECIDABLE and len(table.WCAG22) == 55


def test_the_table_without_an_axe_report_is_complete_and_claims_nothing(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    code, out, err = run(TABLE, "--axe-execution", EXE, "--suspected", "1.1.1=FND-001", "--suspected", "2.1.1=FND-002", cwd=root)
    assert (code, out["criteria"], out["counts"]) == (0, 55, {"not_run": 55}), err
    document = json.loads((root / ".qa" / "work" / "frontend" / "wcag-table.json").read_text())
    grade = grader()
    pairs = grade.table_entries(document)
    assert grade.table_problems(pairs) == []
    entries = {entry["criterion"]: entry for entry in document["criteria"]}
    assert entries["1.1.1"]["suspected_findings"] == ["FND-001"] and EXE in entries["1.1.1"]["note"]
    assert entries["1.4.10"]["reason"] == "manual-only" and entries["1.4.3"]["reason"] == "tool-missing"
    assert grade.body_claims(json.dumps(document)) == []


def test_the_table_reads_a_stored_axe_report(tmp_path):
    root = tmp_path / "p"
    stored = root / ".qa" / "evidence" / EXE / "reports"
    stored.mkdir(parents=True)
    shutil.copyfile(str(AXE_SAMPLE), str(stored / "axe.json"))
    code, out, err = run(TABLE, "--axe-execution", EXE, "--axe-report", ".qa/evidence/%s/reports/axe.json" % EXE, cwd=root)
    assert (code, out["axe_report_read"]) == (0, True), err
    entries = {e["criterion"]: e for e in json.loads((root / ".qa" / "work" / "frontend" / "wcag-table.json").read_text())["criteria"]}
    assert (entries["1.1.1"]["status"], entries["1.1.1"]["rules"]) == ("failed", ["image-alt"])
    assert (entries["1.4.3"]["status"], entries["1.4.3"]["rules"]) == ("failed", ["color-contrast"])
    assert entries["3.1.1"]["status"] == "inconclusive" and entries["2.4.7"]["reason"] == "manual-only"
    assert not [e for e in entries.values() if e["status"] == "passed"]


@pytest.mark.parametrize("args", [["--axe-execution", "EXE-1"], ["--suspected", "1.4.14=FND-001"], ["--suspected", "1.1.1=F-1"],
                                  ["--axe-report", "/etc/hosts"], ["--axe-report", "report.json"], ["--out", "/tmp/table.json"],
                                  ["--out", "table.json"]])
def test_the_table_refuses_bad_arguments(tmp_path, args):
    root = tmp_path / "p"
    root.mkdir()
    (root / "report.json").write_text("[]")
    code, out, err = run(TABLE, *args, cwd=root)
    assert (code, out["ok"]) == (2, False), (out, err)
    assert not (root / ".qa" / "work").exists()
