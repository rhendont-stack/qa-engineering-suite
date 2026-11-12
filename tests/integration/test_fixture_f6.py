"""F6 ``vuln-lab``: a small catalogue site that serves only loopback, generated with a credential made at that moment.

``generate.py`` copies ``lab/`` with the README and the manifest, and writes ``lab/settings.py``. The committed generator
assembles the credential from parts, so no file in the repository holds it or anything shaped like it. The lab's code runs only
inside the rtest container or a container leg, never on the host that drives the engine: every test here that starts or
imports it is skipped anywhere else. The key describes each weakness by class only.
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from helpers import FIXTURES, answer_key
from qa import config
from test_container_live import assert_no_service_left, live, service_run  # noqa: F401 (live is a fixture)

F6 = FIXTURES / "vuln-lab"
# The credential's shape: Gitleaks's built-in Stripe rule and the kernel's stripe_key redaction pattern both read it.
TOKEN_SHAPE = re.compile(r"(?<![A-Za-z0-9])[sr]k_(?:test|live|prod)_[A-Za-z0-9]{10,99}")
MASKED = re.compile(rb"\[REDACTED:stripe_key:([0-9a-f]{8})\]")
GENERATED = ["README.md", "lab/app.py", "lab/catalog.py", "lab/notes/welcome.txt", "lab/settings.py", "lab/templates/hello.html",
             "lab/templates/index.html", "lab/tokens.py", "requirements.txt"]
SKIPPED_DIRECTORIES = {".git", "__pycache__", ".tools", ".superpowers", "node_modules", ".qa", ".pytest_cache"}
# Words no file of the lab or its copy may hold (F2 and F3 hold their fixtures to the same list).
NAMING = re.compile(r"(?i)seed|defect|\bbug\b|answer|off-by-one|vulnerab|exploit|payload")
CWES = {"CWE-89", "CWE-79", "CWE-22", "CWE-798", "CWE-1395", "CWE-489", "CWE-942", "CWE-1004", "CWE-614", "CWE-347", "CWE-693"}
# The entries a test in this file shows in process, by that test. Every other entry is static-review only until a tool leg
# (Task 10) observes it; no tool name is written into the key without an observed run.
DEMONSTRATED_IN_PROCESS = {
    "F6-04": "test_generated_lab_holds_the_token_and_the_repo_does_not",
    "F6-06": "test_the_lab_serves_its_pages_and_shows_the_classes_marked_in_process",
    "F6-07": "test_the_lab_serves_its_pages_and_shows_the_classes_marked_in_process",
    "F6-08": "test_the_lab_serves_its_pages_and_shows_the_classes_marked_in_process",
    "F6-09": "test_the_lab_serves_its_pages_and_shows_the_classes_marked_in_process",
    "F6-11": "test_the_lab_serves_its_pages_and_shows_the_classes_marked_in_process",
}
OBSERVED_TOOLS = {"gitleaks", "osv-scanner"}  # tools seen on F6 through qa run --container; see the task report for the run
ROUTE = re.compile(r'^    \("(GET)", "(/\S*)"\): \w+,$', re.M)


def in_a_container():
    return sys.platform.startswith("linux") and any(os.path.exists(path) for path in ("/.dockerenv", "/run/.containerenv"))


CONTAINER_ONLY = pytest.mark.skipif(not in_a_container(), reason="rtest container only: the lab's code never runs on this host")


def load_generator():
    spec = importlib.util.spec_from_file_location("vuln_lab_generator", str(F6 / "generate.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def generated(directory):
    load_generator().generate(directory)
    return directory


def token_of(copy):
    shaped = TOKEN_SHAPE.findall((copy / "lab" / "settings.py").read_text(encoding="utf-8"))
    assert len(shaped) == 1, len(shaped)
    return shaped[0]


def repo_files(root):
    for here, directories, names in os.walk(str(root)):
        directories[:] = [name for name in directories if name not in SKIPPED_DIRECTORIES and not name.startswith(".venv")]
        for name in names:
            path = Path(here, name)
            if path.is_file() and not path.is_symlink():
                yield path


def test_generated_lab_holds_the_token_and_the_repo_does_not(tmp_path, repo_root):
    generator = load_generator()
    first, second = tmp_path / "first", tmp_path / "second"
    assert generator.generate(first) == GENERATED
    assert sorted(path.relative_to(first).as_posix() for path in first.rglob("*") if path.is_file()) == GENERATED
    for name in GENERATED:
        if name != "lab/settings.py":
            assert (first / name).read_bytes() == (F6 / name).read_bytes(), name
    generator.generate(second)
    token, other = token_of(first), token_of(second)
    assert token != other                                                       # made at generation time, never stored
    body = token.split("_", 2)[2]
    assert "FAKE" in body and "EXAMPLE" in body and token.startswith("sk_test_")
    redaction = json.loads((repo_root / "shared" / "policies" / "redaction.json").read_text(encoding="utf-8"))
    stripe = [pattern["regex"] for pattern in redaction["patterns"] if pattern["kind"] == "stripe_key"]
    assert len(stripe) == 1 and re.search(stripe[0], token).group(0) == token  # the kernel masks it by pattern, not by luck
    shaped, holding = [], []
    for path in repo_files(repo_root):
        text = path.read_bytes().decode("utf-8", "replace")
        if TOKEN_SHAPE.search(text):
            shaped.append(path.relative_to(repo_root).as_posix())
        if token in text or other in text or body in text:
            holding.append(path.relative_to(repo_root).as_posix())
    assert (shaped, holding) == ([], [])
    source = (F6 / "generate.py").read_text(encoding="utf-8")
    assert "FAKE" not in source and "EXAMPLE" not in source                       # assembled from parts
    assert not (F6 / "lab" / "settings.py").exists()
    with pytest.raises(SystemExit):
        generator.generate(F6 / "lab" / "out")                                   # never inside the repository's work tree
    assert not (F6 / "lab" / "out").exists()
    with pytest.raises(SystemExit):
        generator.generate(first)                                                # nor over a copy that already holds files
    assert token_of(first) == token


def sarif_with(token):
    rule = "stripe-access-token"
    return {"version": "2.1.0", "runs": [{
        "tool": {"driver": {"name": "gitleaks", "rules": [{"id": rule, "shortDescription": {"text": "Stripe access token"}}]}},
        "results": [{"ruleId": rule, "level": "error",
                     "message": {"text": "%s has detected secret for file lab/settings.py: %s" % (rule, token)},
                     "locations": [{"physicalLocation": {"artifactLocation": {"uri": "lab/settings.py"},
                                                         "region": {"startLine": 4, "snippet": {"text": 'SECRET_KEY = "%s"' % token}}}}]}]}]}


def test_stored_sarif_and_logs_hold_no_token_value(ws, tmp_path, actor, qa_cli):
    token = token_of(generated(tmp_path / "copy"))
    source = tmp_path / "scan.sarif"
    source.write_text(json.dumps(sarif_with(token)), encoding="utf-8")
    config.record_initialized(ws, actor)              # the ws fixture's config is not yet accepted, as in test_cli
    assert qa_cli("config", "set", "project.trust", "trusted", "--approval", "fixture test")[0] == 0
    echo =("import shutil, sys\nshutil.copyfile(sys.argv[1], sys.argv[2])\ntext = open(sys.argv[1], encoding='utf-8').read()\n"
            "print(text)\nprint(text, file=sys.stderr)\n")
    _code, ran = qa_cli("run", "--activity", "static-analysis", "--category", "security", "--report", "sarif:{evidence}/scan.sarif",
                        "--", sys.executable, "-c", echo, str(source), "{evidence}/scan.sarif")
    assert ran.get("findings") == {"opened": 1, "updated": 0}, sorted(ran)
    stored = [(path.relative_to(ws.root).as_posix(), path.read_bytes()) for path in sorted((ws.root / ".qa").rglob("*")) if path.is_file()]
    body = token.split("_", 2)[2].encode()
    leaked = [name for name, data in stored if token.encode() in data or body in data]
    summary = json.dumps(ran, sort_keys=True).encode()                      # the CLI's own JSON summary on stdout
    assert leaked == [] and token.encode() not in summary and body not in summary
    masked = sorted(name.rsplit("/", 1)[-1] for name, data in stored if MASKED.search(data))
    assert {"scan.sarif", "stdout.log", "stderr.log"} <= set(masked), masked
    fingerprints = {fingerprint for _name, data in stored for fingerprint in MASKED.findall(data)}
    assert len(fingerprints) == 1                                                # one value, one fingerprint, wherever it was


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def ask(port, method, target, headers=None, address="127.0.0.1"):
    connection = http.client.HTTPConnection(address, port, timeout=10)
    try:
        connection.request(method, target, headers=dict({"Host": "127.0.0.1:%d" % port}, **(headers or {})))
        answer = connection.getresponse()
        return answer.status, {name.lower(): value for name, value in answer.getheaders()}, answer.read()
    finally:
        connection.close()


def free_port6():
    with socket.socket(socket.AF_INET6) as probe:
        probe.bind(("::1", 0))
        return probe.getsockname()[1]


@contextmanager
def served(tmp_path, address="127.0.0.1"):
    copy = generated(tmp_path / "copy")
    port = free_port() if address == "127.0.0.1" else free_port6()
    server = subprocess.Popen([sys.executable, str(copy / "lab" / "app.py"), "--host", address, "--port", str(port)], cwd=str(tmp_path),
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                if ask(port, "GET", "/", address=address)[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            raise AssertionError("the lab did not come up")
        yield port, copy
    finally:
        server.terminate()
        server.wait(timeout=10)


@CONTAINER_ONLY
@pytest.mark.parametrize("host", ["192.0.2.1", "0.0.0.0", "localhost", "::", "127.0.0.2"])
def test_lab_refuses_a_routable_host(tmp_path, host):
    copy = generated(tmp_path / "copy")
    done = subprocess.run([sys.executable, str(copy / "lab" / "app.py"), "--host", host, "--port", "8080"], capture_output=True, text=True,
                          timeout=30, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), cwd=str(tmp_path))
    assert done.returncode == 2 and "loopback" in done.stderr and "Traceback" not in done.stderr, done.stderr


FOREIGN_HOSTS = ["example.test", "example.test:8080", "127.0.0.1.example.test", "localhost.example.test", "[::2]", "192.0.2.1", "",
                 "127.0.0.1:80:80", "127.0.0.1:port", "[::1", "::1"]


@CONTAINER_ONLY
def test_lab_answers_421_to_a_foreign_host_header(tmp_path):
    with served(tmp_path) as (port, _copy):
        for host in FOREIGN_HOSTS:
            for method in ("GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH", "TRACE"):
                status, headers, _body = ask(port, method, "/products?category=stationery&limit=5", {"Host": host})
                assert status == 421, (host, method, status)
                assert "set-cookie" not in headers and "access-control-allow-origin" not in headers, (host, method)
        for host in ("127.0.0.1:%d" % port, "localhost:%d" % port, "[::1]:%d" % port, "127.0.0.1", "LocalHost"):
            assert ask(port, "GET", "/", {"Host": host})[0] == 200, host
        assert ask(port, "POST", "/")[0] == 405                                  # a loopback request reaches the routes


@CONTAINER_ONLY
def test_lab_starts_on_the_ipv6_loopback(tmp_path):
    try:
        free_port6()
    except OSError as error:
        pytest.skip("tool missing: no IPv6 loopback in this container (%s)" % type(error).__name__)
    with served(tmp_path, address="::1") as (port, _copy):
        assert ask(port, "GET", "/", {"Host": "[::1]:%d" % port}, address="::1")[0] == 200
        assert ask(port, "GET", "/", {"Host": "example.test"}, address="::1")[0] == 421


@CONTAINER_ONLY
def test_the_lab_serves_its_pages_and_shows_the_classes_marked_in_process(tmp_path):
    with served(tmp_path) as (port, _copy):
        status, headers, page = ask(port, "GET", "/")
        assert status == 200 and b"/products?category=stationery" in page and headers["server"] == "lab"
        for name in ("content-security-policy", "x-content-type-options", "x-frame-options"):     # F6-11
            assert name not in headers, name
        status, headers, listing = ask(port, "GET", "/products?category=stationery&limit=5", {"Origin": "http://elsewhere.example.test"})
        assert status == 200 and [row["name"] for row in json.loads(listing)["items"]] == ["notebook", "pen"]
        assert headers["access-control-allow-origin"] == "http://elsewhere.example.test"                # F6-07
        assert headers["access-control-allow-credentials"] == "true"
        status, headers, detail = ask(port, "GET", "/products?category=stationery&limit=many")
        assert status == 500 and b"Traceback" in detail and b"ValueError" in detail                    # F6-06
        status, headers, _body = ask(port, "GET", "/session")
        cookie = headers["set-cookie"]
        assert status == 200 and cookie.startswith("lab_session=")
        attributes = [part.strip().lower() for part in cookie.split(";")[1:]]
        assert "httponly" not in attributes and "secure" not in attributes                             # F6-08, F6-09
        assert "samesite=lax" in attributes and "path=/" in attributes
        session = cookie.split(";", 1)[0]
        status, _headers, account = ask(port, "GET", "/account", {"Cookie": session})
        assert status == 200 and json.loads(account)["sub"].startswith("visitor-")
        assert ask(port, "GET", "/account")[0] == 401
        changed = session[:-1] + ("A" if session[-1] != "A" else "B")
        assert ask(port, "GET", "/account", {"Cookie": changed})[0] == 401                             # a changed signature
        status, _headers, greeting = ask(port, "GET", "/hello?name=visitor")
        assert status == 200 and b"Hello, visitor." in greeting
        status, _headers, note = ask(port, "GET", "/notes?name=welcome.txt")
        assert status == 200 and note == (F6 / "lab" / "notes" / "welcome.txt").read_bytes()
        assert ask(port, "GET", "/notes?name=absent.txt")[0] == 404
        assert ask(port, "GET", "/nowhere")[0] == 404


def test_f6_answer_key_locations_exist_after_generation(tmp_path, repo_root):
    key = answer_key("vuln-lab")
    copy = generated(tmp_path / "copy")
    anchors = json.loads((repo_root / "shared" / "policies" / "severity-rubric.json").read_text(encoding="utf-8"))["anchors"]
    categories = set(json.loads((repo_root / "shared" / "schemas" / "defs.schema.json").read_text(encoding="utf-8"))["$defs"]["category"]["enum"])
    issues = key["issues"]
    assert key["fixture"] == "vuln-lab" and [issue["id"] for issue in issues] == ["F6-%02d" % n for n in range(1, len(issues) + 1)]
    assert len(issues) == 11
    fields = {"id", "location", "file", "lines", "match", "category", "severity_anchor", "class", "detectable_by"}
    routes = {"%s %s" % route for route in ROUTE.findall((F6 / "lab" / "app.py").read_text(encoding="utf-8"))}
    assert routes == {"GET /", "GET /hello", "GET /products", "GET /notes", "GET /session", "GET /account"}
    for issue in issues:
        if issue["file"] == "requirements.txt":
            assert set(issue) == fields | {"advisory", "file_level"} and issue["file_level"] is True, issue["id"]
            # filled from a recorded offline OSV-Scanner run over this manifest; no pending state is left
            assert isinstance(issue["advisory"], str) and re.fullmatch(r"(CVE-\d{4}-\d{4,}|GHSA(-[0-9a-z]{4}){3}|PYSEC-\d{4}-\d+)",
                                                                     issue["advisory"]), issue["id"]
            assert "osv-scanner" in issue["detectable_by"], issue["id"]
        else:
            assert set(issue) == fields, issue["id"]
        assert issue["location"] == issue["file"] or issue["location"] in routes, issue["id"]
        path = copy / issue["file"]
        assert path.is_file(), issue["id"]
        first, last = issue["lines"]
        lines = path.read_text(encoding="utf-8").splitlines()
        assert 1 <= first <= last <= len(lines), issue["id"]
        assert issue["match"] in "\n".join(lines[first - 1:last]), issue["id"]
        assert issue["severity_anchor"] in anchors and issue["category"] in categories, issue["id"]
        assert re.match(r"CWE-\d+ ", issue["class"]), issue["id"]
        tools = issue["detectable_by"]
        assert "static-review" in tools and len(tools) == len(set(tools)), issue["id"]
        assert ("in-process-check" in tools) is (issue["id"] in DEMONSTRATED_IN_PROCESS), issue["id"]
        assert set(tools) <= {"static-review", "in-process-check"} | OBSERVED_TOOLS, issue["id"]
    assert {issue["class"].split(" ", 1)[0] for issue in issues} == CWES and len({issue["class"] for issue in issues}) == len(issues)
    assert [issue["id"] for issue in issues if "gitleaks" in issue["detectable_by"]] == ["F6-04"]
    assert [issue["file"] for issue in issues if issue["file"] == "lab/settings.py"] == ["lab/settings.py"] * 2
    assert all(callable(globals().get(name)) for name in DEMONSTRATED_IN_PROCESS.values())
    for extra in key.get("known_extras", []):
        assert set(extra) == {"file", "class", "note"} and (copy / extra["file"]).is_file()


def test_the_lab_and_its_copy_name_nothing_they_hold(tmp_path):
    copy = generated(tmp_path / "copy")
    for path in sorted(F6.rglob("*")) + sorted(copy.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            assert not NAMING.search(path.read_text(encoding="utf-8")), path.name
    readme = (copy / "README.md").read_text(encoding="utf-8")
    assert "training lab" in readme and "127.0.0.1" in readme and "::1" in readme and "421" in readme
    assert (copy / "requirements.txt").read_text(encoding="utf-8").count("==") == 1


@pytest.mark.docker
def test_f6_runs_as_a_service(live, tmp_path):  # noqa: F811
    copy = generated(tmp_path / "copy")
    inputs = [(path.relative_to(copy).as_posix(), path) for path in sorted(copy.rglob("*")) if path.is_file()]
    serve = "cd /work/.qa-input\nexec python lab/app.py --host 127.0.0.1 --port 8080\n"
    probe = ("import http.client, sys, time\n"
             "def ask(host):\n"
             "    c = http.client.HTTPConnection('127.0.0.1', 8080, timeout=2)\n"
             "    c.request('GET', '/', headers={'Host': host})\n"
             "    return c.getresponse().status\n"
             "for _ in range(100):\n"
             "    try:\n"
             "        ok = ask('127.0.0.1:8080')\n"
             "        break\n"
             "    except OSError:\n"
             "        time.sleep(0.2)\n"
             "else:\n"
             "    sys.exit(3)\n"
             "open('/qa-evidence/status.txt', 'w').write('%d %d' % (ok, ask('example.test')))\n")
    done = service_run(live, serve, "python -c \"%s\"\n" % probe.replace('"', '\\"'), inputs=inputs)
    assert (done.run.exit_code, done.evidence_error, done.cleanup_failed) == (0, None, ()), (done.run.stderr, done.service_log)
    assert done.evidence["status.txt"] == b"200 421"
    assert (done.network_mode, done.anomalies) == ("none", ())
    assert done.service["running_at_end"] is True
    assert_no_service_left(live)
