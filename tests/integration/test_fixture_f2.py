"""F2 ``rest-api``: a small orders API with its contract and an answer key.

``dispatch`` is the whole API without sockets; ``server.py`` serves it on loopback. ``openapi.yaml`` is written in the JSON-compatible
subset of YAML, so the standard library reads it. The fixture's own files never say what is wrong with it.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from helpers import FIXTURES, LAUNCHER, REPO, answer_key
from test_container_live import assert_no_service_left, live, service_run  # noqa: F401 (live is a fixture)

F2 = FIXTURES / "rest-api"
PYTEST_RUN = ("run", "--report", "junit:{evidence}/junit.xml", "--", sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o",
              "junit_family=xunit2", "--junitxml={evidence}/junit.xml")
METHODS = ("get", "put", "post", "delete", "patch")
# What a smoke test must never contain: the request that would show one of the weaknesses the key lists.
EXPOSING = ("user-b", "admin", "-1", "limit", "offset", "preload", "PUT", "DELETE", "/orders/", "Traceback")


def contract():
    return json.loads((F2 / "openapi.yaml").read_text(encoding="utf-8"))


def operations(document):
    return sorted("%s %s" % (method.upper(), path) for path, item in document["paths"].items() for method in item if method in METHODS)


def forget_the_package():
    for name in [name for name in sys.modules if name == "orders_api" or name.startswith("orders_api.")]:
        del sys.modules[name]


@pytest.fixture(autouse=True)
def drop_the_package_after_each_test():
    yield
    forget_the_package()


def load_app(monkeypatch, directory):
    forget_the_package()
    monkeypatch.syspath_prepend(str(directory))
    return importlib.import_module("orders_api.app")


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


SCRIPT = [
    ("GET", "/health", {}, b""),
    ("POST", "/auth/token", {}, b'{"user": "user-a", "password": "password-a"}'),
    ("POST", "/auth/token", {}, b'{"user": "user-a", "password": "nope"}'),
    ("GET", "/orders", {}, b""),
    ("POST", "/orders", {"Authorization": "Bearer user-a-token"}, b'{"item": "pen", "quantity": 2, "unit_price_cents": 150}'),
    ("GET", "/orders/1", {"Authorization": "Bearer user-a-token"}, b""),
    ("GET", "/orders/1", {"Authorization": "Bearer user-b-token"}, b""),
    ("GET", "/orders/99", {"Authorization": "Bearer user-a-token"}, b""),
    ("PUT", "/orders/1", {"Authorization": "Bearer user-a-token"}, b'{"quantity": 3}'),
    ("PUT", "/orders/1", {"Authorization": "Bearer user-a-token"}, b'{"quantity": 3}'),
    ("POST", "/orders", {"Authorization": "Bearer user-a-token"}, b"{not json"),
    ("POST", "/orders", {"Authorization": "Bearer user-a-token"}, b'{"item": "", "quantity": "x"}'),
    ("GET", "/orders", {"Authorization": "Bearer user-a-token"}, b""),
    ("GET", "/admin/stats", {"Authorization": "Bearer user-b-token"}, b""),
    ("DELETE", "/orders/1", {"Authorization": "Bearer user-b-token"}, b""),
    ("DELETE", "/orders/1", {"Authorization": "Bearer user-a-token"}, b""),
    ("GET", "/nowhere", {}, b""),
]


def over_http(port, method, path, headers, body):
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=body or None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return answer.status, answer.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_f2_smoke_suite_passes_and_hides_every_seed(qa_on_fixture):
    key = answer_key("rest-api")["suites"]["python"]
    project = qa_on_fixture("rest-api")
    code, ran = project.cli(*PYTEST_RUN)
    assert (code, ran["status"]) == (0, key["run_status"]) and ran["status"] == "passed"
    assert project.results(ran["execution"]) == key["tests"] and len(key["tests"]) == 3
    tests = "".join(path.read_text(encoding="utf-8") for path in (F2 / "tests").glob("test_*.py"))
    assert not [word for word in EXPOSING if word in tests]
    for path in F2.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            assert not re.search(r"(?i)seed|defect|\bbug\b|answer|off-by-one|vulnerab|exploit|payload", path.read_text(encoding="utf-8")), path


def test_f2_answer_key_locations_exist(repo_root):
    key = answer_key("rest-api")
    anchors = json.loads((repo_root / "shared" / "policies" / "severity-rubric.json").read_text(encoding="utf-8"))["anchors"]
    categories = set(json.loads((repo_root / "shared" / "schemas" / "defs.schema.json").read_text(encoding="utf-8"))["$defs"]["category"]["enum"])
    issues = key["issues"]
    assert key["fixture"] == "rest-api" and [issue["id"] for issue in issues] == ["F2-%02d" % n for n in range(1, len(issues) + 1)]
    assert len(issues) == 9
    for issue in issues:
        assert set(issue) == {"id", "location", "file", "lines", "match", "category", "severity_anchor", "class", "detectable_by"}, issue["id"]
        assert re.fullmatch(r"(GET|POST|PUT|DELETE) /\S*", issue["location"]), issue["id"]
        path = F2 / issue["file"]
        assert path.is_file(), issue["id"]
        first, last = issue["lines"]
        lines = path.read_text(encoding="utf-8").splitlines()
        assert 1 <= first <= last <= len(lines), issue["id"]
        assert issue["match"] in "\n".join(lines[first - 1:last]), issue["id"]
        assert issue["severity_anchor"] in anchors and issue["category"] in categories, issue["id"]
        assert issue["class"] and issue["detectable_by"] and all(isinstance(tool, str) and tool for tool in issue["detectable_by"]), issue["id"]
        assert {"static-review", "in-process-check"} <= set(issue["detectable_by"]), issue["id"]
    app_text = (F2 / "orders_api" / "app.py").read_text(encoding="utf-8")
    for issue in issues:
        method, path = issue["location"].split(" ", 1)
        assert re.search(r'\("%s", "%s"' % (method, re.escape(path)), app_text), issue["id"]


def test_dispatch_matches_the_server(tmp_path, monkeypatch):
    copy = tmp_path / "copy"
    shutil.copytree(str(F2), str(copy), ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    app = load_app(monkeypatch, copy)
    app.reset()
    direct = []
    for method, path, headers, body in SCRIPT:
        status, _headers, answer = app.dispatch(method, path, dict(headers), body)
        direct.append((status, answer))
    port = free_port()
    server = subprocess.Popen([sys.executable, "-m", "orders_api.server", "--host", "127.0.0.1", "--port", str(port)], cwd=str(copy),
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                if over_http(port, "GET", "/health", {}, b"")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            raise AssertionError("the server did not come up")
        served = [over_http(port, method, path, headers, body) for method, path, headers, body in SCRIPT[1:]]
    finally:
        server.terminate()
        server.wait(timeout=10)
    assert direct[0][0] == 200
    assert served == direct[1:]
    assert len({status for status, _answer in direct}) >= 5          # the script reaches several outcomes, not one


def test_dispatch_answers_odd_paths_and_list_work_grows_with_the_collection(monkeypatch):
    app = load_app(monkeypatch, F2)
    headers = {"Authorization": "Bearer user-a-token"}
    app.reset()
    for method in ("GET", "PUT", "DELETE"):
        for path in ("/orders/" + "9" * 5000, "/orders/" + "9" * 10, "/orders/0", "/orders/-1", "/orders/1.5", "/orders/", "//", ""):
            status, _headers, content = app.dispatch(method, path, headers, b'{"quantity": 1}')
            assert 400 <= status < 500 and status != 400, (method, path[:30], status)
            assert content and json.loads(content), (method, path[:30])
    took = {}
    for count in (100, 400):
        app.reset(preload=count)
        samples = []
        for _ in range(3):
            start = time.perf_counter()
            status, _headers, content = app.dispatch("GET", "/orders", headers, b"")
            samples.append(time.perf_counter() - start)
            assert status == 200 and len(json.loads(content)["items"]) == count // 2
        took[count] = min(samples)
    assert took[400] > 2 * took[100]
    app.reset()


@pytest.mark.parametrize("host", ["192.0.2.1", "0.0.0.0", "localhost", "::"])
def test_server_refuses_a_routable_host(tmp_path, host):
    done = subprocess.run([sys.executable, str(F2 / "orders_api" / "server.py"), "--host", host, "--port", "8080"], capture_output=True,
                          text=True, timeout=30, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", ORDERS_API_PRELOAD="not a number"),
                          cwd=str(tmp_path))
    assert done.returncode == 2 and "loopback" in done.stderr, done.stderr


def test_server_refuses_a_preload_that_is_not_a_number(tmp_path):
    done = subprocess.run([sys.executable, str(F2 / "orders_api" / "server.py"), "--host", "127.0.0.1", "--port", "8080"], capture_output=True,
                          text=True, timeout=30, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", ORDERS_API_PRELOAD="many"), cwd=str(tmp_path))
    assert done.returncode == 2 and "whole number" in done.stderr and "Traceback" not in done.stderr, done.stderr


def test_every_documented_operation_is_in_the_key_inventory():
    key = answer_key("rest-api")
    document = contract()
    assert key["operations"] == operations(document) and len(key["operations"]) == len(set(key["operations"])) >= 7
    assert key["undocumented_operations"] == ["GET /admin/stats"]
    app_text = (F2 / "orders_api" / "app.py").read_text(encoding="utf-8")
    routes = sorted(re.findall(r'^    \("(GET|POST|PUT|DELETE)", "(/\S*)", \w+, (?:True|False)\),$', app_text, re.M))
    assert ["%s %s" % route for route in routes] == sorted(key["operations"] + key["undocumented_operations"])
    assert not set(key["undocumented_operations"]) & set(key["operations"])
    for item in document["paths"].values():
        for method, operation in item.items():
            if method in METHODS:
                assert operation["responses"] and operation["operationId"], method


@pytest.mark.docker
def test_f2_runs_as_a_service(live):  # noqa: F811
    inputs = [(path.relative_to(F2).as_posix(), path) for path in sorted(F2.rglob("*.py")) if "__pycache__" not in path.parts]
    serve = "cd /work/.qa-input\nexec python -m orders_api.server --host 127.0.0.1 --port 8080\n"
    probe = ("import sys, time, urllib.request\n"
             "for _ in range(100):\n"
             "    try:\n"
             "        r = urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)\n"
             "        open('/qa-evidence/health.txt', 'w').write('%d %s' % (r.status, r.read().decode()))\n"
             "        break\n"
             "    except OSError:\n"
             "        time.sleep(0.2)\n"
             "else:\n"
             "    sys.exit(3)\n")
    done = service_run(live, serve, "python -c \"%s\"\n" % probe.replace('"', '\\"'), inputs=inputs)
    assert (done.run.exit_code, done.evidence_error, done.cleanup_failed) == (0, None, ()), (done.run.stderr, done.service_log)
    assert done.evidence["health.txt"].startswith(b"200 ")
    assert (done.network_mode, done.anomalies) == ("none", ())
    assert done.service["running_at_end"] is True
    assert_no_service_left(live)


# ----------------------------------------------------------------------------------------------- the qa-api-testing legs
# Each leg's command is read from the ``qa-leg`` blocks of the skill's references/tools.md, the one text the skill follows,
# and run through the real CLI in the fixture copy's own workspace. The grants are recorded there, never in the repository.

TOOLS_MD = REPO / "skills" / "qa-api-testing" / "references" / "tools.md"
INVENTORY_SCRIPT = REPO / "skills" / "qa-api-testing" / "scripts" / "endpoint_inventory.py"
LEG_PREFIX = ["python3", ".claude/skills/qa-api-testing/scripts/qa.py", "--role", "verifier", "--skill", "qa-api-testing"]
SERVE = "cd /work && exec python3 -m orders_api.server --host 127.0.0.1 --port 8080"
APPROVAL = "Hacer lo necesario para ejecutar el requerimiento, no me preguntes, ejecute de forma autonoma."
LEG_IMAGES = {"spectral": "spectral", "schemathesis-baseline": "schemathesis", "schemathesis-active": "schemathesis", "hurl": "hurl"}


def image(image_id):
    listing = json.loads((REPO / "shared" / "policies" / "container-images.json").read_text(encoding="utf-8"))
    return next(entry["image"] for entry in listing["images"] if entry["id"] == image_id)


def legs():
    """``{name: (attributes, the command line)}`` from the ``qa-leg`` fenced blocks."""
    text = TOOLS_MD.read_text(encoding="utf-8")
    found = {}
    for match in re.finditer(r"^```qa-leg (\S+)((?: [\w-]+=\S+)*)\n(.*?)\n```$", text, re.M | re.S):
        assert match.group(1) not in found, match.group(1)
        found[match.group(1)] = (dict(a.split("=", 1) for a in match.group(2).split()), match.group(3).strip())
    return found


def leg_words(name):
    attributes, line = legs()[name]
    words = shlex.split(line.replace("<spec>", "openapi.yaml").replace("<serve>", SERVE).replace("<token>", "user-a-token"))
    assert words[:len(LEG_PREFIX)] == LEG_PREFIX, words[:len(LEG_PREFIX)]
    return attributes, words[len(LEG_PREFIX):]


def leg_cli(project, words, timeout=1500):
    done = subprocess.run([sys.executable, str(LAUNCHER), "--workspace", str(project.root), "--role", "verifier", "--skill", "qa-api-testing",
                           *words], cwd=str(project.root), capture_output=True, timeout=timeout)
    return done.returncode, json.loads(done.stdout)


def copy_assets(project):
    done = subprocess.run([sys.executable, "-I", "-B", str(INVENTORY_SCRIPT), "assets"], cwd=str(project.root), capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0 and json.loads(done.stdout)["ok"], done.stdout + done.stderr


def grant(project, *activities):
    code, out = project.cli("authorize", "--environment", "local", "--activity", *activities, "--target", "127.0.0.1", "--approval", APPROVAL)
    assert code == 0 and out["ok"], out
    return out


def recorded(project, execution):
    return project.cli("state", "show", "executions")[1]["state"][execution]


def test_the_legs_are_the_four_the_skill_documents_with_pinned_images_and_loopback_only():
    found = legs()
    assert sorted(found) == ["hurl", "schemathesis-active", "schemathesis-baseline", "spectral"]
    assert {name: attributes["grant"] for name, (attributes, _) in found.items()} == {
        "spectral": "none", "hurl": "none", "schemathesis-baseline": "dast-passive", "schemathesis-active": "dast-active"}
    for name, (attributes, line) in found.items():
        _, words = leg_words(name)
        assert words[words.index("--container") + 1] == image(LEG_IMAGES[name]), name
        if "--service" in words:
            assert words[words.index("--service") + 1] == image("python-newest")
        assert attributes["report"] == words[words.index("--report") + 1].split(":", 1)[0]
        assert set(re.findall(r"https?://([^/:\s'\"]+)", line)) <= {"127.0.0.1"}, name
    baseline = leg_words("schemathesis-baseline")[1][-1]
    assert "--phases=examples,coverage " in baseline and "--config-file /work/.qa-input/api/schemathesis-baseline.toml" in baseline
    active = leg_words("schemathesis-active")[1][-1]
    for cap in ("--max-examples=", "--rate-limit=", "--max-failures=", "--request-timeout="):
        assert cap in active, cap


def test_the_schemathesis_legs_are_refused_without_a_grant(qa_on_fixture):
    project = qa_on_fixture("rest-api")
    copy_assets(project)
    for name in ("schemathesis-baseline", "schemathesis-active"):
        code, out = leg_cli(project, leg_words(name)[1])
        assert (code, out["status"], out["reason"]) == (3, "blocked", "not_authorized"), out
    grant(project, "dast-passive")
    code, out = leg_cli(project, leg_words("schemathesis-active")[1])
    assert (code, out["status"], out["reason"]) == (3, "blocked", "not_authorized"), out


@pytest.fixture
def api_project(qa_on_fixture):
    engine = os.environ.get("QA_TEST_CONTAINER_ENGINE")
    if not engine:
        pytest.skip("tool missing: QA_TEST_CONTAINER_ENGINE is not set")
    project = qa_on_fixture("rest-api")
    assert project.cli("config", "set", "container.engine", engine, "--approval", "fixture test")[0] == 0
    copy_assets(project)
    return project


def evidence_of(project, execution):
    return project.root / ".qa" / "evidence" / execution


@pytest.mark.docker
def test_f2_spectral_leg(api_project):
    code, out = leg_cli(api_project, leg_words("spectral")[1])
    print("spectral leg:", out["execution"], out["status"], out.get("reason"))
    assert (code, out["status"]) == (1, "failed"), out
    run = recorded(api_project, out["execution"])
    assert run["container"]["network_mode"] == "none" and run["container"]["service"] is None
    assert run["reports"][0]["format"] == "sarif" and run["reports"][0]["files"] == ["reports/spectral.sarif"]
    sarif = json.loads((evidence_of(api_project, out["execution"]) / "reports" / "spectral.sarif").read_text(encoding="utf-8"))
    rules = {result["ruleId"] for result in sarif["runs"][0]["results"]}
    assert rules and rules <= {"info-contact", "operation-description", "operation-tags", "oas3-api-servers", "info-description"}, rules


@pytest.mark.docker
def test_f2_schemathesis_baseline_leg(api_project):
    grant(api_project, "dast-passive")
    code, out = leg_cli(api_project, leg_words("schemathesis-baseline")[1])
    print("schemathesis baseline leg:", out["execution"], out["status"], out.get("reason"), out["tests"])
    assert (code, out["status"]) == (1, "failed"), out
    assert out["tests"]["total"] >= 7 and out["tests"]["failed"] >= 1
    run = recorded(api_project, out["execution"])
    assert "dast-passive" in [entry["activity"] for entry in run["authorizations"]]
    assert run["container"]["service"]["image"] == image("python-newest") and run["container"]["network_mode"] == "none"
    har = json.loads((evidence_of(api_project, out["execution"]) / "stdout.log").read_text(encoding="utf-8"))
    urls = {entry["request"]["url"].split("/")[2] for entry in har["log"]["entries"]}
    assert har["log"]["entries"] and urls == {"127.0.0.1:8080"}, urls


@pytest.mark.docker
def test_f2_schemathesis_active_leg(api_project):
    grant(api_project, "dast-passive", "dast-active")
    code, out = leg_cli(api_project, leg_words("schemathesis-active")[1])
    print("schemathesis active leg:", out["execution"], out["status"], out.get("reason"), out["tests"])
    assert (code, out["status"]) == (1, "failed"), out
    run = recorded(api_project, out["execution"])
    assert "dast-active" in [entry["activity"] for entry in run["authorizations"]]
    junit = (evidence_of(api_project, out["execution"]) / "reports" / "schemathesis.xml").read_text(encoding="utf-8")
    assert "POST /orders" in junit and re.search(r"Received: 200.*?Documented: 201", junit, re.S), "the documented-status check on POST /orders"


@pytest.mark.docker
def test_f2_hurl_leg(api_project):
    code, out = leg_cli(api_project, leg_words("hurl")[1])
    print("hurl leg:", out["execution"], out["status"], out.get("reason"), out["tests"])
    assert (code, out["status"]) == (0, "passed"), out
    assert out["tests"]["total"] == 1
    run = recorded(api_project, out["execution"])
    assert run["container"]["service"]["running_at_end"] is True and run["container"]["network_mode"] == "none"



# ----------------------------------------------------------------------------------------------- the qa-security leg on F2
# qa-security's zap-active leg, with the contract imported (run_inputs.py --kind zap-active --openapi openapi.yaml), against the
# orders API started as the run's service on 127.0.0.1:8000 (ZAP listens on 8080 itself).

SECURITY = REPO / "skills" / "qa-security"
SECURITY_PREFIX = ["python3", ".claude/skills/qa-security/scripts/qa.py", "--role", "auditor", "--skill", "qa-security"]
ZAP_SERVE = "cd /work && exec python3 -m orders_api.server --host 127.0.0.1 --port 8000"


def security_leg_words(name):
    text = (SECURITY / "references" / "tools.md").read_text(encoding="utf-8")
    blocks = {m.group(1): m.group(2).strip() for m in re.finditer(r"^```qa-leg (\S+)(?: [\w-]+=\S+)*\n(.*?)\n```$", text, re.M | re.S)}
    words = shlex.split(blocks[name].replace("<serve>", ZAP_SERVE))
    assert words[:len(SECURITY_PREFIX)] == SECURITY_PREFIX
    return words[len(SECURITY_PREFIX):]


def security_run(project, words, timeout=1700):
    done = subprocess.run([sys.executable, str(LAUNCHER), "--workspace", str(project.root), "--role", "auditor", "--skill", "qa-security",
                           *words], cwd=str(project.root), capture_output=True, timeout=timeout)
    return done.returncode, json.loads(done.stdout)


def security_script(project, name, *args):
    done = subprocess.run([sys.executable, "-I", "-B", str(SECURITY / "scripts" / name), *args], cwd=str(project.root),
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and json.loads(done.stdout)["ok"], done.stdout + done.stderr
    return json.loads(done.stdout)


@pytest.mark.docker
def test_f2_zap_api_active_leg(api_project):
    plan = security_script(api_project, "run_inputs.py", "--kind", "zap-active", "--openapi", "openapi.yaml")
    assert plan["openapi"] == "openapi.yaml"
    grant(api_project, "dast-passive", "dast-active")
    code, out = security_run(api_project, security_leg_words("zap-active"))
    print("zap api active leg:", out["execution"], out["status"], out.get("reason"))
    log = evidence_of(api_project, out["execution"]) / "stdout.log"
    assert out["status"] in ("passed", "failed"), (out, log.read_text(encoding="utf-8", errors="replace")[-3000:] if log.exists() else None)
    run = recorded(api_project, out["execution"])
    assert "dast-active" in [entry["activity"] for entry in run["authorizations"]]
    assert run["container"]["service"]["image"] == image("python-newest") and run["container"]["network_mode"] == "none"
    assert run["command"]["argv"] == ["sleep 3; QA_TARGET=http://127.0.0.1:8000 zap.sh -cmd -notel -autorun /work/.qa-input/security/zap-active.yaml"]
    assert "apiFile: /work/openapi.yaml" in (api_project.root / plan["written"]).read_text(encoding="utf-8")
    triaged = security_script(api_project, "sarif_triage.py", "--sarif", ".qa/evidence/%s/reports/zap.json" % out["execution"])
    listing = json.loads((api_project.root / triaged["out"]).read_text(encoding="utf-8"))
    endpoints = sorted({a["endpoint"] for a in listing["alerts"] if a["endpoint"]})
    print("zap api alerts:", sorted({(a["rule"], tuple(a["cwe"])) for a in listing["alerts"]}))
    print("zap api endpoints:", endpoints)
    assert listing["alerts"] and {e.split(" ", 1)[1].split("/")[2] for e in endpoints} == {"127.0.0.1:8000"}
    assert any("/orders" in e for e in endpoints), endpoints                                # reached through the imported contract
    spec = importlib.util.spec_from_file_location("qa_security_triage_f2", str(SECURITY / "scripts" / "sarif_triage.py"))
    triage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(triage)
    recall_spec = importlib.util.spec_from_file_location("qa_recall_f2", str(REPO / "benchmark" / "graders" / "recall.py"))
    recall = importlib.util.module_from_spec(recall_spec)
    recall_spec.loader.exec_module(recall)
    scored = recall.recall(answer_key("rest-api"), triage.detections(listing, "zap"), {"zap"})
    print("zap api recall on F2:", scored)


# ----------------------------------------------------------------------------------------------- the qa-performance leg on F2
# qa-performance's k6 leg, read from its references/tools.md, with the workload of its references/workload-model.md turned
# into a script by k6_script.py, against the orders API started as the run's service on 127.0.0.1:8080 with 300 orders
# preloaded. The load-test grant (at most 10 VUs, 100 requests a second, 60 seconds) is recorded in each test's own workspace
# with the user's words. The k6 legs run one at a time, never beside another leg.

PERFORMANCE = REPO / "skills" / "qa-performance"
PERFORMANCE_PREFIX = ["python3", ".claude/skills/qa-performance/scripts/qa.py", "--role", "verifier", "--skill", "qa-performance"]
K6_SERVE = SERVE + " --preload 300"
LOAD_LIMITS = {"max_vus": 10, "max_rps": 100, "max_duration_s": 60}
LEG_SCRIPT = "f2-orders-listing"


def performance_legs():
    text = (PERFORMANCE / "references" / "tools.md").read_text(encoding="utf-8")
    found = {}
    for match in re.finditer(r"^```qa-leg (\S+)((?: [\w-]+=\S+)*)\n(.*?)\n```$", text, re.M | re.S):
        found[match.group(1)] = (dict(a.split("=", 1) for a in match.group(2).split()), match.group(3).strip())
    return found


def k6_leg_words():
    _attributes, line = performance_legs()["k6"]
    words = shlex.split(line.replace("<serve>", K6_SERVE).replace("<script>", LEG_SCRIPT))
    assert words[:len(PERFORMANCE_PREFIX)] == PERFORMANCE_PREFIX
    return words[len(PERFORMANCE_PREFIX):]


def performance_run(project, words, timeout=600):
    done = subprocess.run([sys.executable, str(LAUNCHER), "--workspace", str(project.root), "--role", "verifier", "--skill", "qa-performance",
                           *words], cwd=str(project.root), capture_output=True, timeout=timeout)
    return done.returncode, json.loads(done.stdout)


def performance_script(project, name, *args):
    done = subprocess.run([sys.executable, "-I", "-B", str(PERFORMANCE / "scripts" / name), *args], cwd=str(project.root),
                          capture_output=True, text=True, timeout=180)
    assert done.returncode == 0 and json.loads(done.stdout)["ok"], done.stdout + done.stderr
    return json.loads(done.stdout)


def leg_script(project):
    """The leg's workload from workload-model.md, written as the skill writes it, and its script from k6_script.py."""
    text = (PERFORMANCE / "references" / "workload-model.md").read_text(encoding="utf-8")
    block = re.search(r"^```qa-workload %s\n(.*?)\n```$" % LEG_SCRIPT, text, re.M | re.S).group(1)
    folder = project.root / ".qa" / "work" / "performance"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (LEG_SCRIPT + ".json")).write_text(block, encoding="utf-8")
    return performance_script(project, "k6_script.py", "--workload", ".qa/work/performance/%s.json" % LEG_SCRIPT, "--max-vus", "5",
                              "--max-duration-s", "60", "--max-rps", "100", "--out", ".qa/work/performance/%s.js" % LEG_SCRIPT)


def load_grant(project):
    limits = [word for name, value in sorted(LOAD_LIMITS.items()) for word in ("--limit", "load-test.%s=%d" % (name, value))]
    code, out = project.cli("authorize", "--environment", "local", "--activity", "load-test", "--target", "127.0.0.1", *limits,
                            "--approval", APPROVAL)
    assert code == 0 and out["ok"], out
    return out


def test_the_k6_leg_is_documented_with_pinned_images_and_loopback_only():
    found = performance_legs()
    assert sorted(found) == ["k6"] and found["k6"][0] == {"grant": "load-test", "report": "k6"}
    words = k6_leg_words()
    assert words[words.index("--container") + 1] == image("k6") and words[words.index("--service") + 1] == image("python-newest")
    assert words[words.index("--activity") + 1] == "load-test" and words[words.index("--input") + 1] == ".qa/work/performance/%s.js" % LEG_SCRIPT
    assert set(re.findall(r"https?://([^/:\s'\"]+)", found["k6"][1])) == {"127.0.0.1"}
    assert words[-1].startswith("sleep 3; k6 run --no-usage-report -e BASE_URL=http://127.0.0.1:8080 -e SUMMARY_PATH=/qa-evidence/k6-summary.json ")


def test_the_k6_leg_is_refused_without_a_load_test_grant_and_a_grant_needs_its_limits(qa_on_fixture):
    project = qa_on_fixture("rest-api")
    leg_script(project)
    code, out = performance_run(project, k6_leg_words())
    assert (code, out["status"], out["reason"]) == (3, "blocked", "not_authorized"), out
    code, out = project.cli("authorize", "--environment", "local", "--activity", "load-test", "--target", "127.0.0.1", "--approval", APPROVAL)
    assert code != 0 and not out["ok"], out


@pytest.fixture
def performance_project(qa_on_fixture):
    engine = os.environ.get("QA_TEST_CONTAINER_ENGINE")
    if not engine:
        pytest.skip("tool missing: QA_TEST_CONTAINER_ENGINE is not set")
    project = qa_on_fixture("rest-api")
    assert project.cli("config", "set", "container.engine", engine, "--approval", "fixture test")[0] == 0
    return project


def k6_measured(project, execution):
    """perf_table.py's rows for one run, as ``{(metric, stat): (value, label)}``."""
    table = performance_script(project, "perf_table.py", "--execution", execution)
    assert table["measured"] is True, table
    return {(row["metric"], row["stat"]): (row["value"], row["label"]) for row in table["rows"]}, table


def k6_stored_summary(project, execution):
    return json.loads((evidence_of(project, execution) / "reports" / "k6-summary.json").read_text(encoding="utf-8"))


@pytest.mark.docker
def test_f2_k6_leg(performance_project):
    leg_script(performance_project)
    load_grant(performance_project)
    for repeat in (1, 2):                                      # two runs, one after the other, for the noise between them
        code, out = performance_run(performance_project, k6_leg_words())
        log = evidence_of(performance_project, out["execution"]) / "stdout.log"
        assert out["status"] in ("passed", "failed"), (out, log.read_text(encoding="utf-8", errors="replace")[-3000:] if log.exists() else None)
        run = recorded(performance_project, out["execution"])
        assert "load-test" in [entry["activity"] for entry in run["authorizations"]]
        assert run["container"]["image"] == image("k6") and run["container"]["service"]["image"] == image("python-newest")
        assert run["container"]["network_mode"] == "none" and run["report"]["format"] == "k6" and run["report"]["label"] == "measured"
        assert [r["files"] for r in run["reports"]] == [["reports/k6-summary.json"]]
        rows, table = k6_measured(performance_project, out["execution"])
        print("k6 leg run %d:" % repeat, out["execution"], out["status"], out.get("reason"), "class", table["environment_class"],
              "thresholds", table["thresholds"])
        print("k6 leg run %d numbers:" % repeat, json.dumps({"%s %s" % key: value for key, value in sorted(rows.items())}, sort_keys=True))
        assert rows[("vus_max", "max")] == (5, "simulated") and rows[("http_reqs", "count")][0] > 0
        assert {label for _value, label in rows.values()} <= {"simulated", "derived"}
        assert rows[("http_req_failed", "rate")][0] == 0, "every listing answered 200"


@pytest.mark.docker
def test_f2_k6_leg_stays_within_the_grant(performance_project):
    leg_script(performance_project)
    load_grant(performance_project)
    code, out = performance_run(performance_project, k6_leg_words())
    assert out["status"] in ("passed", "failed"), out
    run = recorded(performance_project, out["execution"])
    assert "load_limit_exceeded" not in run["anomalies"], run["anomalies"]
    assert not [note for note in run["limitations"] if note.startswith("load_limit_exceeded")], run["limitations"]
    stored = k6_stored_summary(performance_project, out["execution"])
    measured = {"vus_max.max": run["report"]["metrics"]["vus_max"]["max"], "http_reqs.rate": run["report"]["metrics"]["http_reqs"]["rate"],
                "duration_s": stored["state"]["testRunDurationMs"] / 1000}
    print("k6 leg within the grant:", out["execution"], out["status"], measured, "limits", LOAD_LIMITS)
    assert measured["vus_max.max"] <= LOAD_LIMITS["max_vus"] and measured["http_reqs.rate"] <= LOAD_LIMITS["max_rps"]
    assert 30 <= measured["duration_s"] <= LOAD_LIMITS["max_duration_s"]
