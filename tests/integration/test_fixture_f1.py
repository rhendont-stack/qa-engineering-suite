"""F1 ``web-frontend``: a small static web front end with a stdlib server and an answer key.

``server.dispatch`` is the whole server without sockets; ``server.py`` serves it on loopback. The fixture's own files never say what is
wrong with them.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from helpers import FIXTURES, answer_key
from qa import ingest
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
