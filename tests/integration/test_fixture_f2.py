"""F2 ``rest-api``: a small orders API with its contract and an answer key.

``dispatch`` is the whole API without sockets; ``server.py`` serves it on loopback. ``openapi.yaml`` is written in the JSON-compatible
subset of YAML, so the standard library reads it. The fixture's own files never say what is wrong with it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from helpers import FIXTURES, answer_key
from test_container_live import SERVICE_IMAGE, assert_no_service_left, live, service_run  # noqa: F401 (live is a fixture)

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


def load_app(monkeypatch, directory):
    for name in [name for name in sys.modules if name == "orders_api" or name.startswith("orders_api.")]:
        del sys.modules[name]
    monkeypatch.syspath_prepend(str(directory))
    import importlib
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
        assert "static-review" in issue["detectable_by"] or "in-process-check" in issue["detectable_by"] or issue["detectable_by"], issue["id"]
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


def test_dispatch_never_raises_and_list_work_grows_with_the_collection(tmp_path, monkeypatch):
    app = load_app(monkeypatch, F2)
    headers = {"Authorization": "Bearer user-a-token"}
    took = {}
    for count in (100, 400):
        app.reset(preload=count)
        start = time.perf_counter()
        status, _headers, answer = app.dispatch("GET", "/orders", headers, b"")
        took[count] = time.perf_counter() - start
        assert status == 200 and len(json.loads(answer)["items"]) == count // 2
    assert took[400] > 2 * took[100]
    app.reset()


def test_server_refuses_a_routable_host(tmp_path):
    done = subprocess.run([sys.executable, str(F2 / "orders_api" / "server.py"), "--host", "192.0.2.1", "--port", "8080"], capture_output=True,
                          text=True, timeout=30, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), cwd=str(tmp_path))
    assert done.returncode == 2 and "loopback" in done.stderr, done.stderr


def test_every_documented_operation_is_in_the_key_inventory():
    key = answer_key("rest-api")
    document = contract()
    assert key["operations"] == operations(document) and len(key["operations"]) == len(set(key["operations"])) >= 7
    assert key["undocumented_operations"] == ["GET /admin/stats"]
    assert not set(key["undocumented_operations"]) & set(key["operations"])
    for item in document["paths"].values():
        for method, operation in item.items():
            if method in METHODS:
                assert operation["responses"] and operation["operationId"], method


@pytest.mark.docker
def test_f2_runs_as_a_service(live, tmp_path):  # noqa: F811
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
