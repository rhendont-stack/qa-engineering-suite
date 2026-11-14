"""F1 ``web-frontend``: a small static web front end with a stdlib server and an answer key.

``server.dispatch`` is the whole server without sockets; ``server.py`` serves it on loopback. The fixture's own files never say what is
wrong with them.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from helpers import FIXTURES, LAUNCHER, REPO, answer_key
from qa import container, ingest
from test_container_live import assert_no_service_left, live, service_run  # noqa: F401 (live is a fixture)

F1 = FIXTURES / "web-frontend"
RUN_JUNIT = ("run", "--report", "junit:{evidence}/junit.xml", "--", sys.executable, "tests/run_junit.py", "--out", "{evidence}/junit.xml")
TOOLS = {"axe-core", "playwright", "lighthouse", "static-review", "in-process-check"}
# What a smoke test must never contain: the page text and requests that would reach one of the problems the key lists.
EXPOSING = ("alt=", "onclick", "email", "aaaaaa", "480", "is-visible", "contact-card", "about.js", "empty", "error", "500", "Traceback")


def load_server(name="f1_server"):
    spec = importlib.util.spec_from_file_location(name, str(F1 / "server.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def get(port, path):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=10) as reply:
            return reply.status, reply.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_f1_smoke_suite_passes_and_hides_every_seed(qa_on_fixture):
    key = answer_key("web-frontend")["suites"]["python"]
    project = qa_on_fixture("web-frontend")
    code, ran = project.cli(*RUN_JUNIT)
    assert (code, ran["status"]) == (0, key["run_status"]) and ran["status"] == "passed"
    assert project.results(ran["execution"]) == key["tests"] and len(key["tests"]) == 7
    tests = (F1 / "tests" / "test_site.py").read_text(encoding="utf-8")
    assert not [word for word in EXPOSING if word in tests]
    for path in F1.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            assert not re.search(r"(?i)seed|defect|\bbug\b|answer|vulnerab|exploit|payload", path.read_text(encoding="utf-8")), path


def test_f1_answer_key_locations_exist(repo_root):
    key = answer_key("web-frontend")
    anchors = json.loads((repo_root / "shared" / "policies" / "severity-rubric.json").read_text(encoding="utf-8"))["anchors"]
    categories = set(json.loads((repo_root / "shared" / "schemas" / "defs.schema.json").read_text(encoding="utf-8"))["$defs"]["category"]["enum"])
    issues = key["issues"]
    assert key["fixture"] == "web-frontend" and [issue["id"] for issue in issues] == ["F1-%02d" % n for n in range(1, 9)]
    for issue in issues:
        assert set(issue) == {"id", "location", "file", "lines", "match", "category", "severity_anchor", "class", "detectable_by"}, issue["id"]
        path = F1 / issue["file"]
        assert path.is_file(), issue["id"]
        first, last = issue["lines"]
        lines = path.read_text(encoding="utf-8").splitlines()
        assert 1 <= first <= last <= len(lines), issue["id"]
        assert issue["match"] in "\n".join(lines[first - 1:last]), issue["id"]
        assert issue["severity_anchor"] in anchors and issue["category"] in categories, issue["id"]
        assert issue["class"] and issue["detectable_by"] and set(issue["detectable_by"]) <= TOOLS, issue["id"]
        route = issue["location"].split(" ")[0]
        assert route == "/about.html" or route == "/", issue["id"]
    # the four defects a static scan of the files can see, by file and line
    by_id = {issue["id"]: issue for issue in issues}
    for static in ("F1-01", "F1-02", "F1-03", "F1-07"):
        assert "static-review" in by_id[static]["detectable_by"] and by_id[static]["file"].rsplit(".", 1)[1] in ("html", "css"), static


def test_run_junit_writes_a_parsable_report(tmp_path):
    out = tmp_path / "junit.xml"
    done = subprocess.run([sys.executable, "-B", str(F1 / "tests" / "run_junit.py"), "--out", str(out)], capture_output=True, text=True,
                          timeout=120, cwd=str(tmp_path))                      # a working directory that is not the fixture
    assert done.returncode == 0, done.stdout + done.stderr
    summary = ingest.parse_report("junit", out, 10**6)
    assert summary.total >= 1 and summary.passed == summary.total


def test_run_junit_exits_1_when_a_test_fails(tmp_path):
    import shutil
    copy = tmp_path / "copy"
    shutil.copytree(str(F1), str(copy), ignore=shutil.ignore_patterns("__pycache__"))
    (copy / "tests" / "test_extra.py").write_text(
        "import unittest\n\n\nclass Extra(unittest.TestCase):\n    def test_fails(self):\n        self.assertEqual(1, 2)\n", encoding="utf-8")
    done = subprocess.run([sys.executable, "-B", str(copy / "tests" / "run_junit.py"), "--out", str(tmp_path / "r.xml")],
                          capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
    assert done.returncode == 1, done.stdout + done.stderr
    assert ingest.parse_report("junit", tmp_path / "r.xml", 10**6).failed == 1


@pytest.mark.parametrize("host", ["192.0.2.1", "0.0.0.0", "localhost", "::"])
def test_server_refuses_a_routable_host(tmp_path, host):
    done = subprocess.run([sys.executable, "-B", str(F1 / "server.py"), "--host", host, "--port", "8080"], capture_output=True, text=True,
                          timeout=30, cwd=str(tmp_path))
    assert done.returncode == 2 and "loopback" in done.stderr, done.stderr


def test_dispatch_error_switch_answers_500():
    server = load_server()
    assert server.dispatch("GET", "/api/products")[0] == 200 and len(json.loads(server.dispatch("GET", "/api/products")[2])) == 3
    server.set_products("empty")
    assert server.dispatch("GET", "/api/products")[::2] == (200, b"[]")
    server.set_products("error")
    status, headers, body = server.dispatch("GET", "/api/products")
    assert status == 500 and headers["Content-Type"] == "application/json" and json.loads(body)
    assert server.dispatch("GET", "/")[0] == 200                           # only the product list changes
    with pytest.raises(ValueError):
        server.set_products("slow")


def test_dispatch_serves_only_files_inside_the_site():
    server = load_server()
    for path in ("/../server.py", "/..%2fserver.py", "/site/../server.py", "//", "/tests/test_site.py", "/server.py", "/a\\b", "/%00"):
        assert server.dispatch("GET", path)[0] == 404, path
    assert server.dispatch("GET", "/banner.svg")[1]["Content-Type"] == "image/svg+xml"
    assert server.dispatch("HEAD", "/")[0] == 200 and server.dispatch("DELETE", "/")[0] == 405


def test_the_site_loads_nothing_from_another_host():
    for path in sorted((F1 / "site").iterdir()):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(?i)https?://|//[a-z0-9.-]+\.[a-z]{2,}|@import|@font-face", text.replace('xmlns="http://www.w3.org/2000/svg"', "")), path


@pytest.mark.parametrize("mode,expected", [("normal", 3), ("empty", 0), ("error", None)])
def test_server_serves_the_site_and_the_products_switch_over_loopback(mode, expected):
    port = free_port()
    server = subprocess.Popen([sys.executable, "-B", str(F1 / "server.py"), "--host", "127.0.0.1", "--port", str(port), "--products", mode],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                if get(port, "/")[0] == 200:
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("the server did not come up")
        status, body = get(port, "/api/products")
        assert status == (500 if expected is None else 200)
        if expected is not None:
            assert len(json.loads(body)) == expected
        assert get(port, "/about.html")[0] == 200 and get(port, "/nowhere")[0] == 404
    finally:
        server.terminate()
        server.wait(timeout=10)


@pytest.mark.docker
def test_f1_runs_as_a_service(live):  # noqa: F811
    inputs = [(path.relative_to(F1).as_posix(), path) for path in sorted(F1.rglob("*")) if path.is_file() and "__pycache__" not in path.parts]
    serve = "cd /work/.qa-input\nexec python server.py --host 127.0.0.1 --port 8080\n"
    probe = ("import sys, time, urllib.request\n"
             "for _ in range(100):\n"
             "    try:\n"
             "        r = urllib.request.urlopen('http://127.0.0.1:8080/', timeout=2)\n"
             "        open('/qa-evidence/index.txt', 'w').write('%d %s' % (r.status, r.headers['Content-Type']))\n"
             "        break\n"
             "    except OSError:\n"
             "        time.sleep(0.2)\n"
             "else:\n"
             "    sys.exit(3)\n")
    done = service_run(live, serve, "python -c \"%s\"\n" % probe.replace('"', '\\"'), inputs=inputs)
    assert (done.run.exit_code, done.evidence_error, done.cleanup_failed) == (0, None, ()), (done.run.stderr, done.service_log)
    assert done.evidence["index.txt"].startswith(b"200 text/html")
    assert (done.network_mode, done.anomalies) == ("none", ())
    assert done.service["running_at_end"] is True
    assert_no_service_left(live)


# ----------------------------------------------------------------------------------------------- the qa-frontend legs
# Each leg's command is read from the ``qa-leg`` blocks of qa-frontend's references/tools.md, the one text the skill follows,
# and run through the real CLI in a copy of F1 with its own workspace, left untrusted as the skill leaves it: a container run
# needs no trust. F1's server runs only as the service of a container run, on loopback. The install step's grant is recorded
# in that temporary workspace with the user's words.

FRONTEND = REPO / "skills" / "qa-frontend"
FRONTEND_TOOLS_MD = FRONTEND / "references" / "tools.md"
FRONTEND_PREFIX = ["python3", ".claude/skills/qa-frontend/scripts/qa.py", "--role", "verifier", "--skill", "qa-frontend"]
F1_SERVE = "cd /work && exec python3 -B server.py --host 127.0.0.1 --port 8080"
APPROVAL = "Hacer lo necesario para ejecutar el requerimiento, no me preguntes, ejecute de forma autonoma."
FRONTEND_LEG_IMAGES = {"frontend-static": "python-newest", "frontend-checks": "playwright", "frontend-axe": "playwright",
                       "frontend-lighthouse": "playwright"}
CHECK_PAGES = ("--page", "/", "--page", "/about.html", "--state-route", "/api/products", "--loading", "#loading", "--list", "#product-list")


def image_of(image_id):
    listing = json.loads((REPO / "shared" / "policies" / "container-images.json").read_text(encoding="utf-8"))
    return next(entry["image"] for entry in listing["images"] if entry["id"] == image_id)


def frontend_legs():
    """``{name: (attributes, the command line)}`` from the ``qa-leg`` fenced blocks of qa-frontend's tools.md."""
    found = {}
    for match in re.finditer(r"^```qa-leg (\S+)((?: [\w-]+=\S+)*)\n(.*?)\n```$", FRONTEND_TOOLS_MD.read_text(encoding="utf-8"), re.M | re.S):
        assert match.group(1) not in found, match.group(1)
        found[match.group(1)] = (dict(a.split("=", 1) for a in match.group(2).split()), match.group(3).strip())
    return found


def frontend_words(name, serve=F1_SERVE):
    attributes, line = frontend_legs()[name]
    words = shlex.split(line.replace("<serve>", serve))
    assert words[:len(FRONTEND_PREFIX)] == FRONTEND_PREFIX, words[:len(FRONTEND_PREFIX)]
    return attributes, words[len(FRONTEND_PREFIX):]


def frontend_cli(root, words, timeout=1700):
    done = subprocess.run([sys.executable, str(LAUNCHER), "--workspace", str(root), "--role", "verifier", "--skill", "qa-frontend", *words],
                          cwd=str(root), capture_output=True, timeout=timeout)
    return done.returncode, json.loads(done.stdout)


def frontend_script(root, name, *args):
    done = subprocess.run([sys.executable, "-I", "-B", str(FRONTEND / "scripts" / name), *args], cwd=str(root), capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0 and json.loads(done.stdout)["ok"], done.stdout + done.stderr
    return json.loads(done.stdout)


@pytest.fixture
def f1_project(qa_on_fixture):
    """A copy of F1 in its own untrusted workspace, with the container engine set; skipped without one."""
    engine = os.environ.get("QA_TEST_CONTAINER_ENGINE")
    if not engine:
        pytest.skip("tool missing: QA_TEST_CONTAINER_ENGINE is not set")
    project = qa_on_fixture("web-frontend")
    assert project.cli("config", "set", "container.engine", engine, "--approval", "fixture test")[0] == 0
    code, out = project.cli("config", "set", "project.trust", "untrusted", "--approval", "fixture test")
    assert code == 0 and project.cli("config", "show")[1]["config"]["project"]["trust"] == "untrusted", out
    return project


def install_grant(project):
    code, out = project.cli("authorize", "--environment", "local", "--activity", "install-dependencies", "--target", "127.0.0.1",
                            "--approval", APPROVAL)
    assert code == 0 and out["ok"], out
    return out


def execution_of(project, execution):
    return project.cli("state", "show", "executions")[1]["state"][execution]


def evidence_of(project, execution):
    return project.root / ".qa" / "evidence" / execution


def browser_leg(project, name, kind, *page_args, words=None, started=True):
    install_grant(project)
    frontend_script(project.root, "run_inputs.py", "--kind", kind, *page_args)
    code, out = frontend_cli(project.root, words or frontend_words(name)[1])
    run = execution_of(project, out["execution"])
    print("%s leg:" % name, out["execution"], "exit", code, out["status"], out.get("reason"), "tests", out.get("tests"),
          "limitations", out.get("limitations"))
    print("%s record:" % name, json.dumps({k: run.get(k) for k in ("status", "reason", "exit_code", "reports", "container")}, sort_keys=True)[:3000])
    if started:
        assert [a["activity"] for a in run["authorizations"] if a["activity"] == "install-dependencies"], run["authorizations"]
        assert run["container"]["network_mode"] == "none" and run["container"]["service"]["image"] == image_of("python-newest")
    return code, out, run


STATIC_FINDING = {
    "title": "site/index.html:19: the hero image has no text alternative (WCAG 1.1.1)",
    "description": "The img element at site/index.html:19 has no alt attribute and no other accessible name; read from the source "
                   "by the static scan, not shown in a browser.",
    "category": "accessibility",
    "severity_rationale": {"anchor": "A11Y-M1", "modifiers": []},
    "component": {"path": "site/index.html", "criticality": "normal"},
    "verification_status": "suspected",
    "evidence": ["site/index.html:19"],
    "reproduction_steps": ["read site/index.html:19"],
    "impact": "A screen reader announces the image by its file name or not at all.",
    "probable_cause": "site/index.html:19 the img element has no alt attribute",
    "recommendation": "Give the image an alt text that says what it shows, or alt=\"\" if it is decorative; nothing was changed here.",
    "refs": {"rule": "img-without-alt", "location": "site/index.html", "wcag": ["1.1.1"]},
}
EMAIL_FINDING = {
    "title": "site/index.html:38: the email field is typed text, so it takes an address without @ (CWE-20)",
    "description": "The input at site/index.html:38 is named email but typed text, so the browser checks nothing it holds; read from "
                   "the source by the static scan, not shown in a browser.",
    "category": "functional",
    "severity_rationale": {"anchor": "FUNC-M2", "modifiers": []},
    "component": {"path": "site/index.html", "criticality": "normal"},
    "verification_status": "suspected",
    "evidence": ["site/index.html:38"],
    "reproduction_steps": ["read site/index.html:38"],
    "impact": "A visitor can submit an address without @ and never hear back.",
    "probable_cause": "site/index.html:38 the input is type=text",
    "recommendation": "Type the field email and check the value on the server too; nothing was changed here.",
    "refs": {"rule": "email-input-not-typed", "location": "site/index.html", "cwe": ["CWE-20"], "wcag_related": ["3.3.1"]},
}
# The documented link of two records of one problem (references/triage.md, two records of one problem).
LINK_EVIDENCE = "`finding update <KEEP> --json <file>`"
LINK_STATE = '`finding state <OTHER> duplicate --reason "<why they are one problem>" --duplicate-of <KEEP>`'


def junit_cases(path):
    import xml.etree.ElementTree as ET
    cases = {}
    for case in ET.parse(str(path)).getroot().iter("testcase"):
        outcome = "failed" if case.find("failure") is not None or case.find("error") is not None else \
            "skipped" if case.find("skipped") is not None else "passed"
        properties = {p.get("name"): p.get("value") or (p.text or "") for p in case.iter("property")}
        cases[case.get("name")] = (outcome, properties, (case.find("failure").get("message") if case.find("failure") is not None else ""))
    return cases


def test_the_frontend_legs_are_the_documented_ones_with_pinned_images_and_loopback_only():
    found = frontend_legs()
    assert sorted(found) == sorted(FRONTEND_LEG_IMAGES)
    assert {name: attributes["grant"] for name, (attributes, _) in found.items()} == {
        "frontend-static": "none", "frontend-checks": "install", "frontend-axe": "install", "frontend-lighthouse": "install"}
    for name, (attributes, line) in found.items():
        _, words = frontend_words(name)
        assert words[0] == "run" and words[words.index("--container") + 1] == image_of(FRONTEND_LEG_IMAGES[name]), name
        assert attributes["report"] == words[words.index("--report") + 1].split(":", 1)[0], name
        assert set(re.findall(r"https?://([^/:\s'\"]+)", line)) <= {"127.0.0.1"}, name
        assert "npx" not in line and "lhci" not in line and "temporary-public-storage" not in line, name
        if name != "frontend-static":
            assert words[words.index("--service") + 1] == image_of("python-newest") and words[words.index("--service-user") + 1] == "65534:65534"
            assert words[words.index("--install") + 1] == "cd /work/.qa-input/frontend && npm ci --ignore-scripts --no-audit --no-fund"
            assert "/work/.qa-input/frontend/node_modules/.bin/" in words[-1], name
    lighthouse = frontend_words("frontend-lighthouse")[1][-1]
    assert "--no-enable-error-reporting" in lighthouse and "--output=json" in lighthouse and "--disable-dev-shm-usage" in lighthouse
    assert "CHROME_PATH=/ms-playwright/chromium-1255/chrome-linux64/chrome" in lighthouse
    for name in ("frontend-checks", "frontend-axe"):
        assert "QA_BASE_URL=http://127.0.0.1:8080 " in frontend_words(name)[1][-1]


@pytest.mark.docker
def test_f1_static_ui_leg(f1_project):
    project = f1_project
    # the host scan's findings for two results, recorded first as the skill records them (references/states.md, the finding file)
    work = project.root / ".qa" / "work" / "frontend"
    work.mkdir(parents=True)
    yours = {}
    for name, body in (("img", STATIC_FINDING), ("email", EMAIL_FINDING)):
        (work / ("%s.json" % name)).write_text(json.dumps(body), encoding="utf-8")
        code, added = frontend_cli(project.root, ["finding", "add", "--json", ".qa/work/frontend/%s.json" % name])
        assert code == 0, added
        yours[name] = added["id"]
    frontend_script(project.root, "static_ui_scan.py", "--copy-to-work")
    code, out = frontend_cli(project.root, frontend_words("frontend-static")[1])
    print("frontend-static leg:", out["execution"], "exit", code, out["status"], out.get("reason"), out.get("findings"))
    assert out["findings"] == {"opened": 3, "updated": 1}, out
    run = execution_of(project, out["execution"])
    assert (code, out["status"]) == (1, "failed"), out
    assert run["container"]["network_mode"] == "none" and run["reports"][0]["files"] == ["reports/static-ui.sarif"]
    report = json.loads((evidence_of(project, out["execution"]) / "reports" / "static-ui.sarif").read_text(encoding="utf-8"))
    found = sorted((r["ruleId"], r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"],
                    r["locations"][0]["physicalLocation"]["region"]["startLine"]) for r in report["runs"][0]["results"])
    print("frontend-static results:", found)
    assert found == [("click-handler-on-non-interactive", "site/index.html", 22), ("email-input-not-typed", "site/index.html", 38),
                     ("fixed-width-over-375", "site/styles.css", 33), ("img-without-alt", "site/index.html", 19)]
    findings = project.cli("state", "show", "findings")[1]["state"]
    print("frontend-static findings:", sorted((f["refs"]["rule"], f["component"].get("path"), f["category"]) for f in findings.values()))
    assert sorted((f["refs"]["rule"], f["component"].get("path"), f["category"]) for f in findings.values()) == [
        ("click-handler-on-non-interactive", "site/index.html", "accessibility"),
        ("email-input-not-typed", "site/index.html", "accessibility"), ("email-input-not-typed", "site/index.html", "functional"),
        ("fixed-width-over-375", "site/styles.css", "accessibility"), ("img-without-alt", "site/index.html", "accessibility")]
    kept = [f for f in findings.values() if f["refs"]["rule"] == "img-without-alt"]
    assert len(kept) == 1 and kept[0]["id"] == yours["img"] and kept[0]["refs"]["wcag"] == ["1.1.1"]
    assert kept[0]["verification_status"] == "suspected"
    # The email field's copy, linked as references/triage.md documents it. The kernel's dedupe never proposes this pair (no
    # source line, no shared CWE) and refuses it; the duplicate transition takes it.
    triage = (FRONTEND / "references" / "triage.md").read_text(encoding="utf-8")
    assert LINK_EVIDENCE in triage and LINK_STATE in triage
    copy = next(fid for fid, f in findings.items() if f["refs"]["rule"] == "email-input-not-typed" and f["category"] == "accessibility")
    assert findings[copy]["severity_rationale"]["anchor"] == "TOOL-PROVISIONAL" and findings[copy]["state"] == "open"
    code, refused = frontend_cli(project.root, ["finding", "dedupe", "--confirm", "%s:%s" % (yours["email"], copy)])
    assert code != 0 and refused["error"]["code"] == "not_a_duplicate", refused
    (work / "email-evidence.json").write_text(json.dumps({"evidence": EMAIL_FINDING["evidence"] + [out["execution"]]}), encoding="utf-8")
    code, updated = frontend_cli(project.root, ["finding", "update", yours["email"], "--json", ".qa/work/frontend/email-evidence.json"])
    assert code == 0, updated
    code, linked = frontend_cli(project.root, ["finding", "state", copy, "duplicate", "--reason",
                                               "the static leg's record of the email field at site/index.html:38", "--duplicate-of",
                                               yours["email"]])
    assert (code, linked["state"]) == (0, "duplicate"), linked
    findings = project.cli("state", "show", "findings")[1]["state"]
    print("frontend-static link:", copy, "->", findings[copy].get("duplicate_of"), "evidence", findings[yours["email"]]["evidence"])
    assert findings[copy]["state"] == "duplicate" and findings[copy]["duplicate_of"] == yours["email"]
    assert findings[yours["email"]]["state"] == "open" and out["execution"] in findings[yours["email"]]["evidence"]


# What each browser check that fails on F1 locates, by the check's name: the answer-key entry whose class it shows. The
# measured runs are in the task report; the key names playwright, axe-core or lighthouse on exactly these entries.
CHECKS_LOCATE = {"/: email fields refuse a value without @": "F1-01", "/: every click target is reachable by keyboard": "F1-02",
                 "/about.html: no console error, page error or failed request": "F1-05",
                 "/: empty state under a routed empty list": "F1-06", "/: no horizontal overflow": "F1-07",
                 "/: error state under a routed HTTP 500": "F1-08"}
AXE_LOCATE = {("/", "image-alt"): "F1-03", ("/", "color-contrast"): "F1-04"}
LIGHTHOUSE_LOCATE = {("/", "image-alt"): "F1-03", ("/", "color-contrast"): "F1-04", ("/about.html", "errors-in-console"): "F1-05"}
VIEWPORTS = ("phone-375x812", "tablet-768x1024", "desktop-1280x800")


def tagged(tool):
    return sorted(issue["id"] for issue in answer_key("web-frontend")["issues"] if tool in issue["detectable_by"])


@pytest.mark.docker
def test_f1_checks_leg(f1_project):
    code, out, run = browser_leg(f1_project, "frontend-checks", "checks", *CHECK_PAGES)
    assert (code, out["status"]) == (1, "failed"), out
    cases = junit_cases(evidence_of(f1_project, out["execution"]) / "reports" / "junit.xml")
    for name in sorted(cases):
        print("  %-8s %s %s" % (cases[name][0], name, cases[name][2][:160].replace("\n", " ")))
    shots = sorted(v[1]["screenshot"] for v in cases.values() if v[1].get("screenshot"))
    print("screenshots:", shots)
    assert out["tests"] == {"errors": 0, "failed": 16, "passed": 14, "skipped": 12, "total": 42}, out["tests"]
    assert len(cases) == 42 and all(re.match(r"\[(%s)\] /" % "|".join(VIEWPORTS), name) for name in cases)
    failed = sorted(name for name, (outcome, _, _) in cases.items() if outcome == "failed")
    expected = sorted("[%s] %s" % (viewport, check) for check in CHECKS_LOCATE for viewport in VIEWPORTS
                      if check != "/: no horizontal overflow" or viewport == "phone-375x812")
    assert failed == expected, failed
    assert sorted(set(CHECKS_LOCATE.values())) == tagged("playwright")
    assert sorted(f1_project.results(out["execution"]).values()).count("not_run") == 12
    assert len(shots) == 6 and all(re.match(r"sha256:[0-9a-f]{64} \d+x\d+ px, \d+ bytes, viewport \d+x\d+$", s) for s in shots)


@pytest.mark.docker
def test_f1_axe_leg(f1_project):
    code, out, run = browser_leg(f1_project, "frontend-axe", "axe", "--page", "/", "--page", "/about.html")
    assert (code, out["status"]) == (1, "failed"), out
    stored = json.loads((evidence_of(f1_project, out["execution"]) / "reports" / "axe.json").read_text(encoding="utf-8"))
    found = {}
    for page in stored:
        path = page["url"].split(":8080", 1)[1]
        print("axe", path, "version", page["testEngine"]["version"], "violations", sorted((v["id"], len(v["nodes"])) for v in page["violations"]),
              "incomplete", sorted(v["id"] for v in page["incomplete"]))
        found.update({(path, v["id"]): [t for t in v["tags"] if t.startswith("wcag")] for v in page["violations"]})
    rules = {rule["id"] for page in stored for key in ("violations", "incomplete", "passes", "inapplicable") for rule in page[key]}
    named = {"%s.%s.%s" % m.groups() for page in stored for key in ("violations", "incomplete", "passes", "inapplicable")
             for rule in page[key] for tag in rule["tags"] for m in [re.fullmatch(r"wcag(\d)(\d)(\d{1,2})", tag)] if m}
    print("axe rules run:", len(rules), "criteria named:", sorted(named), "violations:", found)
    assert sorted(found) == sorted(AXE_LOCATE) and sorted(set(AXE_LOCATE.values())) == tagged("axe-core")
    assert "wcag111" in found[("/", "image-alt")] and "wcag143" in found[("/", "color-contrast")]
    grade = load_module(FRONTEND / "evals" / "grade.py", "qa_frontend_grade_axe_leg")
    assert sorted(named & set(grade.WCAG22)) == sorted(grade.AXE_DECIDABLE) and named - set(grade.WCAG22) == {"2.1.3"}
    findings = f1_project.cli("state", "show", "findings")[1]["state"]
    print("axe findings:", sorted((f["refs"]["rule"], f["component"].get("path"), f["source"].get("uri")) for f in findings.values()))
    assert sorted((f["refs"]["rule"], f["source"].get("uri")) for f in findings.values()) == [("color-contrast", "/"), ("image-alt", "/")]


@pytest.mark.docker
def test_f1_lighthouse_leg(f1_project):
    located = {}
    for path in ("/", "/about.html"):
        words = frontend_words("frontend-lighthouse")[1]
        if path != "/":
            words = words[:-1] + [words[-1].replace(" http://127.0.0.1:8080/ ", " http://127.0.0.1:8080%s " % path)]
        code, out, run = browser_leg(f1_project, "frontend-lighthouse", "lighthouse", words=words)
        assert (code, out["status"]) == (0, "passed"), out
        stored = json.loads((evidence_of(f1_project, out["execution"]) / "reports" / "lighthouse.json").read_text(encoding="utf-8"))
        failing = sorted(a for a, v in stored["audits"].items() if v.get("score") == 0)
        print("lighthouse", path, stored["lighthouseVersion"], "scores", {k: v.get("score") for k, v in stored["categories"].items()},
              "failing audits:", failing)
        assert stored["lighthouseVersion"] == "13.6.0" and sorted(stored["categories"]) == ["accessibility", "best-practices", "performance"]
        located.update({(path, audit): issue for (where, audit), issue in LIGHTHOUSE_LOCATE.items() if where == path and audit in failing})
    print("lighthouse locates:", located)
    assert located == LIGHTHOUSE_LOCATE and sorted(set(located.values())) == tagged("lighthouse")


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def with_base_url(value):
    _, words = frontend_words("frontend-checks")
    assert "QA_BASE_URL=http://127.0.0.1:8080 " in words[-1]
    return words[:-1] + [words[-1].replace("QA_BASE_URL=http://127.0.0.1:8080 ", "QA_BASE_URL=%s " % value)]


@pytest.mark.docker
def test_frontend_runner_refuses_a_non_loopback_base_url(f1_project):
    # The documented command with a non-loopback base URL (a documentation address): the kernel reads the URL's host, which
    # no environment declares and no grant lists, and records the run blocked before any container starts.
    code, out, run = browser_leg(f1_project, "frontend-checks", "checks", "--page", "/", words=with_base_url("http://192.0.2.10"),
                                 started=False)
    assert (code, out["status"], out["reason"], run.get("container")) == (3, "blocked", "not_authorized", None), out
    assert any("192.0.2.10" in item for item in out["limitations"]) and out["evidence"] is None
    grants = f1_project.cli("state", "show", "authorizations")[1]["state"]
    assert "192.0.2.10" not in json.dumps(grants)
    # The runner's own refusal, behind the kernel's: a base URL the kernel lets through (its host is 127.0.0.1) that is not
    # http: reaches the configuration, which exits nonzero at load, before any browser starts and with no report.
    code, out, run = browser_leg(f1_project, "frontend-checks", "checks", "--page", "/", words=with_base_url("https://127.0.0.1:8080"))
    logs = "".join((evidence_of(f1_project, out["execution"]) / name).read_text(encoding="utf-8") for name in ("stdout.log", "stderr.log"))
    print("refusal logs:", logs[-600:])
    assert out["status"] != "passed" and run["exit_code"] not in (0, None), out
    assert "qa-frontend: refused" in logs and "no browser was started" in logs and "https://127.0.0.1" not in logs
    assert not (evidence_of(f1_project, out["execution"]) / "reports" / "junit.xml").exists()
    assert not re.search(r"Running \d+ test", logs), logs


REFUSAL = b"qa-frontend: refused: QA_BASE_URL must be http:// on 127.0.0.1, [::1] or localhost; no browser was started\n"


def load_runner_config(base_url):
    """The runner configuration loaded by the playwright image's own node, outside ``qa run``: the file on stdin (no mount),
    no network, a container named ``qa-rtest-<hex>`` that is removed by that name. Returns the engine's outcome."""
    engine_text = os.environ.get("QA_TEST_CONTAINER_ENGINE")
    if not engine_text:
        pytest.skip("tool missing: QA_TEST_CONTAINER_ENGINE is not set")
    engine = container.Engine(engine_text)
    name = "qa-rtest-" + secrets.token_hex(6)
    try:
        return engine.run(["run", "--rm", "-i", "--pull=never", "--network=none", "--entrypoint=node", "--env=QA_BASE_URL=" + base_url,
                           "--label=qa.suite.test=frontend-config", "--name=" + name, image_of("playwright"), "--input-type=module"],
                          stdin_path=FRONTEND / "assets" / "playwright.config.mjs", timeout_s=120, max_bytes=1 << 20)
    finally:
        engine.run(["rm", "-f", name], stdin_path=None, timeout_s=60, max_bytes=1 << 16)
        assert engine.run(["inspect", "--type=container", name], stdin_path=None, timeout_s=60, max_bytes=1 << 16).exit_code != 0


@pytest.mark.docker
@pytest.mark.parametrize("base_url", ["http://192.0.2.10:8080/", "http://shop.example:8080/", "http://qa-user:qa-pass@127.0.0.1:8080/"])
def test_the_runner_config_refuses_a_base_url_off_loopback_at_load(base_url):
    # The configuration's own refusal, run directly: a host outside loopback (an address, a name) and a loopback URL that carries
    # credentials. It exits 2 at load with its one line and nothing else: no browser starts and the value is not printed.
    done = load_runner_config(base_url)
    print("config load:", base_url.split("//", 1)[1].split("@")[-1], "exit", done.exit_code, done.stdout[-200:], done.stderr[-300:])
    assert (done.exit_code, done.stdout, done.stderr) == (2, b"", REFUSAL), done
    assert b"192.0.2.10" not in done.stderr and b"shop.example" not in done.stderr and b"qa-pass" not in done.stderr


@pytest.mark.docker
def test_the_runner_config_loads_for_the_loopback_base_url():
    # The control: the base URL of the documented legs loads with no output, so the refusals above come from the host check.
    done = load_runner_config("http://127.0.0.1:8080")
    assert (done.exit_code, done.stdout, done.stderr) == (0, b"", b""), done
