"""The bundled script of qa-api-testing: ``endpoint_inventory.py``.

It is checked on a copy of fixture F2 (``fixtures/rest-api``: an OpenAPI 3.0 contract in block YAML and a standard-library
route table), and per reader and per route form on small files written at test time: block YAML with what the reader must
refuse, JSON, Flask, FastAPI and Express. ``mark`` is checked for the rules a status needs.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import textwrap

import pytest

from helpers import REPO

SCRIPT = REPO / "skills" / "qa-api-testing" / "scripts" / "endpoint_inventory.py"
F2 = REPO / "fixtures" / "rest-api"
EXE_ID = "EXE-01M77PJTWG6R7VNCR52TMTT8TN"


def load():
    """The script in this process. Its kernel import finds the source kernel already loaded, and the path entry and bytecode
    flag it sets are put back, so no later test imports the vendored copy."""
    import qa.workspace  # noqa: F401  (the source kernel, from pytest's pythonpath)
    saved_path, saved_flag = list(sys.path), sys.dont_write_bytecode
    try:
        spec = importlib.util.spec_from_file_location("qa_api_endpoint_inventory", str(SCRIPT))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path[:], sys.dont_write_bytecode = saved_path, saved_flag
    return module


def run(*args, cwd):
    done = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT)] + [str(a) for a in args], capture_output=True, text=True,
                          timeout=120, cwd=str(cwd), env={"PATH": os.environ["PATH"], "HOME": str(cwd)})
    return done.returncode, (json.loads(done.stdout) if done.stdout.strip() else None), done.stderr


def inventory(cwd, name=".qa/work/api/inventory.json"):
    return json.loads((cwd / name).read_text(encoding="utf-8"))


def snapshot(root):
    found = {}
    for here, dirs, names in os.walk(str(root)):
        dirs[:] = [d for d in dirs if d != ".qa"]
        for name in names:
            path = os.path.join(here, name)
            with open(path, "rb") as handle:
                found[os.path.relpath(path, str(root))] = handle.read()
    return found


@pytest.fixture
def f2(tmp_path):
    copy = tmp_path / "f2"
    shutil.copytree(str(F2), str(copy), ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".qa"))
    return copy


def line_of(path, needle):
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if needle in line:
            return number
    raise AssertionError("%s not in %s" % (needle, path))


# ----------------------------------------------------------------------------------------------- fixture F2

def test_scan_of_f2_lists_every_documented_operation_and_the_drift_with_lines(f2):
    before = snapshot(f2)
    code, out, err = run("scan", "--spec", "openapi.yaml", cwd=f2)
    assert code == 0, err
    assert out["ok"] is True and out["documented"] == 7 and out["unreadable"] == 0
    assert snapshot(f2) == before, "the scan changed a project file"
    document = inventory(f2)
    names = sorted(o["operation"] for o in document["operations"] if o["documented"])
    assert names == sorted(["GET /health", "POST /auth/token", "GET /orders", "POST /orders", "GET /orders/{id}",
                            "PUT /orders/{id}", "DELETE /orders/{id}"])
    app = f2 / "orders_api" / "app.py"
    assert document["drift"]["undocumented"] == [{"operation": "GET /admin/stats", "file": "orders_api/app.py",
                                                  "line": line_of(app, '"/admin/stats"'), "approximate": True}]
    mismatches = document["drift"]["status_mismatch"]
    assert [(m["operation"], m["returned"], m["file"]) for m in mismatches] == [("POST /orders", 200, "orders_api/app.py")]
    assert "reply(200, present(order))" in app.read_text(encoding="utf-8").splitlines()[mismatches[0]["line"] - 1]
    assert document["drift"]["unimplemented"] == [] and document["drift"]["method_mismatch"] == []


def test_every_operation_starts_not_run_with_a_reason_and_every_code_location_is_approximate(f2):
    run("scan", "--spec", "openapi.yaml", cwd=f2)
    for entry in inventory(f2)["operations"]:
        assert entry["status"] == "not_run" and entry["reason"]
        assert entry["code"] and all(c["approximate"] is True for c in entry["code"])
    secured = {o["operation"]: o["security"] for o in inventory(f2)["operations"]}
    assert secured["GET /orders"] == ["bearerAuth"] and secured["GET /health"] is None


def test_the_inventory_is_written_only_under_qa_work(f2):
    for out in ("inventory.json", ".qa/inventory.json", ".qa/work", "../outside.json", ".qa/work/../config.json"):
        code, body, _ = run("scan", "--spec", "openapi.yaml", "--out", out, cwd=f2)
        assert code == 2 and body["ok"] is False and "only under .qa/work/" in body["error"]
    assert not (f2 / "inventory.json").exists() and not (f2.parent / "outside.json").exists()


def test_the_inventory_never_follows_a_link_at_the_old_temporary_name(f2, tmp_path):
    """Final review I1: the inventory was written through the fixed name ``inventory.json.tmp`` with a write that follows a
    link. A link already at that name under .qa/work/ stays a link with its target untouched, and the inventory is a regular
    file, after ``scan`` and after ``mark``."""
    work = f2 / ".qa" / "work" / "api"
    work.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    os.symlink(str(outside), str(work / "inventory.json.tmp"))
    code, body, err = run("scan", "--spec", "openapi.yaml", cwd=f2)
    assert code == 0 and body["ok"] is True, err
    target = work / "inventory.json"
    assert target.is_file() and not target.is_symlink() and sum(1 for o in inventory(f2)["operations"] if o["documented"]) == 7
    assert outside.read_text(encoding="utf-8") == "keep\n" and os.path.islink(str(work / "inventory.json.tmp"))
    code, body, err = run("mark", "--operation", "GET /health", "--status", "blocked", "--reason", "engine absent", cwd=f2)
    assert code == 0 and body["marked"] == ["GET /health"], err
    assert target.is_file() and not target.is_symlink() and outside.read_text(encoding="utf-8") == "keep\n"
    assert sorted(p.name for p in work.iterdir()) == ["inventory.json", "inventory.json.tmp"]      # no temporary name left behind


def test_a_link_at_the_inventory_name_that_leaves_qa_work_is_refused(f2, tmp_path):
    work = f2 / ".qa" / "work" / "api"
    work.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("keep\n", encoding="utf-8")
    os.symlink(str(outside), str(work / "inventory.json"))
    code, body, _ = run("scan", "--spec", "openapi.yaml", cwd=f2)
    assert code == 2 and "only under .qa/work/" in body["error"]
    assert outside.read_text(encoding="utf-8") == "keep\n" and os.path.islink(str(work / "inventory.json"))


def test_a_missing_contract_is_an_argument_error(f2):
    code, body, _ = run("scan", "--spec", "nope.yaml", cwd=f2)
    assert code == 2 and "no contract" in body["error"]


# ----------------------------------------------------------------------------------------------- mark

def test_mark_sets_verified_only_with_an_execution_id(f2):
    run("scan", "--spec", "openapi.yaml", cwd=f2)
    code, body, _ = run("mark", "--operation", "GET /health", "--status", "verified", cwd=f2)
    assert code == 2 and "--execution" in body["error"]
    code, body, _ = run("mark", "--operation", "GET /health", "--status", "verified", "--execution", "EXE-123", cwd=f2)
    assert code == 2
    code, body, _ = run("mark", "--operation", "GET /health", "--operation", "GET /orders/{orderId}", "--status", "verified",
                        "--execution", EXE_ID, cwd=f2)
    assert code == 0 and body["marked"] == ["GET /health", "GET /orders/{id}"]
    entries = {o["operation"]: o for o in inventory(f2)["operations"]}
    assert entries["GET /health"]["status"] == "verified" and entries["GET /health"]["execution"] == EXE_ID
    assert entries["GET /orders/{id}"]["executions"] == [EXE_ID]


def test_mark_needs_a_reason_for_not_run_and_blocked_and_a_known_operation(f2):
    run("scan", "--spec", "openapi.yaml", cwd=f2)
    for status in ("not_run", "blocked"):
        code, body, _ = run("mark", "--operation", "POST /orders", "--status", status, cwd=f2)
        assert code == 2 and "--reason" in body["error"]
    code, body, _ = run("mark", "--operation", "PATCH /orders", "--status", "blocked", "--reason", "x", cwd=f2)
    assert code == 2 and "not in the inventory" in body["error"]
    code, body, _ = run("mark", "--operation", "POST /orders", "--status", "blocked", "--reason", "container engine unavailable",
                        "--execution", EXE_ID, cwd=f2)
    assert code == 0
    entry = {o["operation"]: o for o in inventory(f2)["operations"]}["POST /orders"]
    assert entry["status"] == "blocked" and entry["reason"] == "container engine unavailable" and "execution" not in entry


def test_mark_refuses_an_inventory_outside_qa_work(f2):
    (f2 / "inv.json").write_text(json.dumps({"operations": []}), encoding="utf-8")
    code, body, _ = run("mark", "--inventory", "inv.json", "--operation", "GET /health", "--status", "blocked", "--reason", "x",
                        cwd=f2)
    assert code == 2 and "under .qa/work/" in body["error"]


# ----------------------------------------------------------------------------------------------- the contract readers

def test_block_yaml_lists_what_it_cannot_read_with_line_numbers(tmp_path):
    spec = tmp_path / "spec.yaml"
    spec.write_text(textwrap.dedent("""\
        openapi: 3.0.3
        info:
          title: t   # a comment
          description: |
            free text: with a colon
            - and a dash
        paths:
          /a:
            get:
              responses:
                "200":
                  description: ok
          /b: &shared
            post:
              responses:
                '201': {"description": "made"}
          /c: *shared
          /d:
            delete:
              security: []
              responses:
                default:
                  description: any
        """), encoding="utf-8")
    module = load()
    document, fmt, unreadable = module.read_contract(spec)
    assert fmt == "yaml-block"
    assert sorted(document["paths"]) == ["/a", "/b", "/c", "/d"]
    assert document["paths"]["/b"] is None and document["paths"]["/c"] is None
    assert sorted(u["line"] for u in unreadable) == [13, 17]
    assert document["info"]["title"] == "t" and document["info"]["description"] == ""
    operations = {o["operation"]: o for o in module.documented_operations(document)}
    assert operations["GET /a"]["responses"] == ["200"]
    assert operations["DELETE /d"]["security"] == [] and operations["DELETE /d"]["responses"] == ["default"]


def test_block_yaml_reads_sequences_of_mappings_and_global_security(tmp_path):
    spec = tmp_path / "spec.yaml"
    spec.write_text(textwrap.dedent("""\
        openapi: 3.1.0
        security:
          - apiKey: []
          - oauth: [read, write]
        paths:
          /items/{itemId}:
            parameters:
              - name: itemId
                in: path
                required: true
            get:
              responses:
                2XX:
                  description: fine
            put:
              security:
                - basic: []
              responses:
                "204":
                  description: done
        """), encoding="utf-8")
    module = load()
    document, _, unreadable = module.read_contract(spec)
    assert unreadable == []
    assert document["paths"]["/items/{itemId}"]["parameters"][0] == {"name": "itemId", "in": "path", "required": True}
    operations = {o["operation"]: o for o in module.documented_operations(document)}
    assert operations["GET /items/{itemId}"]["security"] == ["apiKey", "oauth"]
    assert operations["PUT /items/{itemId}"]["security"] == ["basic"]
    assert module._documents_status(["2XX"], 204) and not module._documents_status(["2XX"], 404)
    assert module._documents_status(["default"], 500)


def test_json_contract_and_tab_indentation(tmp_path):
    module = load()
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"openapi": "3.0.0", "paths": {"/x": {"get": {"responses": {"200": {}}}}}}), encoding="utf-8")
    document, fmt, unreadable = module.read_contract(spec)
    assert fmt == "json" and unreadable == [] and module.documented_operations(document)[0]["operation"] == "GET /x"
    broken = tmp_path / "broken.json"
    broken.write_text('{"openapi": "3.0.0",\n "paths": }', encoding="utf-8")
    document, fmt, unreadable = module.read_contract(broken)
    assert document is None and unreadable[0]["line"] == 2
    tabs = tmp_path / "tabs.yaml"
    tabs.write_text("openapi: 3.0.0\npaths:\n\t/x:\n", encoding="utf-8")
    _, _, unreadable = module.read_contract(tabs)
    assert [u["line"] for u in unreadable] == [3]


def test_an_unreadable_contract_exits_1_and_still_writes_the_routes(tmp_path):
    (tmp_path / "spec.json").write_text("{ not json", encoding="utf-8")
    (tmp_path / "app.py").write_text("@app.get('/x')\ndef x():\n    return {}\n", encoding="utf-8")
    code, body, _ = run("scan", "--spec", "spec.json", cwd=tmp_path)
    assert code == 1 and body["ok"] is False and body["unreadable"] == 1
    assert inventory(tmp_path)["drift"]["undocumented"][0]["operation"] == "GET /x"


# ----------------------------------------------------------------------------------------------- the route scan

def test_flask_fastapi_and_express_routes_are_found_with_lines_and_statuses(tmp_path):
    (tmp_path / "flask_app.py").write_text(textwrap.dedent("""\
        from flask import Flask, abort
        app = Flask(__name__)

        @app.route("/items/<int:item_id>", methods=["GET", "DELETE"])
        def item(item_id):
            if item_id > 9:
                abort(404)
            return {"id": item_id}, 200
        """), encoding="utf-8")
    (tmp_path / "fast.py").write_text(textwrap.dedent("""\
        from fastapi import FastAPI, HTTPException
        api = FastAPI()

        @api.post("/things", status_code=201)
        async def make():
            raise HTTPException(status_code=409)
        """), encoding="utf-8")
    (tmp_path / "server.js").write_text("const app = require('express')();\napp.patch('/users/:userId', (req, res) => res.sendStatus(204));\n",
                                        encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("@app.get('/not-a-route')\ndef t():\n    pass\n", encoding="utf-8")
    routes = {r["operation"]: r for r in load().scan_routes(tmp_path)}
    assert sorted(routes) == ["DELETE /items/<int:item_id>", "GET /items/<int:item_id>", "PATCH /users/:userId", "POST /things"]
    assert routes["GET /items/<int:item_id>"]["line"] == 4 and routes["GET /items/<int:item_id>"]["file"] == "flask_app.py"
    assert [s["status"] for s in routes["GET /items/<int:item_id>"]["statuses"]] == [404, 200]
    assert [s["status"] for s in routes["POST /things"]["statuses"]] == [201, 409]
    assert routes["PATCH /users/:userId"]["line"] == 2 and routes["PATCH /users/:userId"]["statuses"] == []
    assert all(r["approximate"] is True for r in routes.values())


def test_path_parameters_of_every_style_compare_equal_and_method_mismatch_is_reported(tmp_path):
    module = load()
    assert module.normal("get", "/items/{id}/") == module.normal("GET", "/items/<int:item_id>") == module.normal("GET", "/items/:id")
    (tmp_path / "spec.yaml").write_text(textwrap.dedent("""\
        openapi: 3.0.0
        paths:
          /items/{id}:
            get:
              responses:
                "200":
                  description: ok
          /gone:
            get:
              responses:
                "200":
                  description: ok
        """), encoding="utf-8")
    (tmp_path / "app.py").write_text("@app.route('/items/<id>', methods=['GET', 'PATCH'])\ndef item(id):\n    return {}\n",
                                     encoding="utf-8")
    code, _, err = run("scan", "--spec", "spec.yaml", cwd=tmp_path)
    assert code == 0, err
    drift = inventory(tmp_path)["drift"]
    assert drift["method_mismatch"] == [{"path": "/items/{}", "documented": ["GET"], "implemented": ["GET", "PATCH"]}]
    assert drift["unimplemented"] == [{"operation": "GET /gone"}]
    assert [d["operation"] for d in drift["undocumented"]] == ["PATCH /items/<id>"]


# ----------------------------------------------------------------------------------------------- assets

def test_assets_are_copied_under_qa_work_and_never_overwritten(f2):
    code, body, err = run("assets", cwd=f2)
    assert code == 0, err
    names = sorted(os.path.basename(p) for p in body["copied"])
    assert names == ["schemathesis-baseline.toml", "smoke.hurl", "spectral.yaml"] and body["kept"] == []
    for name in names:
        assert (f2 / ".qa" / "work" / "api" / name).read_bytes() == (SCRIPT.parent.parent / "assets" / name).read_bytes()
    (f2 / ".qa" / "work" / "api" / "smoke.hurl").write_text("GET http://127.0.0.1:8080/health\nHTTP 200\n", encoding="utf-8")
    code, body, _ = run("assets", cwd=f2)
    assert code == 0 and body["copied"] == [] and len(body["kept"]) == 3
    assert (f2 / ".qa" / "work" / "api" / "smoke.hurl").read_text(encoding="utf-8").startswith("GET http://127.0.0.1:8080/health")
    code, body, _ = run("assets", "--dest", "assets-here", cwd=f2)
    assert code == 2 and not (f2 / "assets-here").exists()


def test_assets_never_follow_a_link_at_a_copy_name(f2, tmp_path, monkeypatch, capsys):
    """Final review I1, the assets path: a copy was written after a separate look for an entry, with a write that follows a
    link. A link already at a copy's name is kept and never followed, whether it leads out of .qa/work/ or stays inside, and
    whether or not a look before the write sees it (here every look is made to see nothing, as for a link that appears just
    after it)."""
    work = f2 / ".qa" / "work" / "api"
    work.mkdir(parents=True)
    outside = tmp_path / "outside.hurl"
    outside.write_text("keep\n", encoding="utf-8")
    os.symlink(str(outside), str(work / "smoke.hurl"))
    os.symlink(str(tmp_path / "absent.yaml"), str(work / "spectral.yaml"))              # a dangling link out of .qa/work/
    code, body, err = run("assets", cwd=f2)
    assert code == 0 and body["copied"] == [".qa/work/api/schemathesis-baseline.toml"], err
    assert sorted(body["kept"]) == [".qa/work/api/smoke.hurl", ".qa/work/api/spectral.yaml"]
    copy = work / "schemathesis-baseline.toml"
    assert not copy.is_symlink() and copy.read_bytes() == (SCRIPT.parent.parent / "assets" / copy.name).read_bytes()
    assert outside.read_text(encoding="utf-8") == "keep\n" and not (tmp_path / "absent.yaml").exists()
    assert os.path.islink(str(work / "smoke.hurl")) and os.path.islink(str(work / "spectral.yaml"))

    for name in ("smoke.hurl", "spectral.yaml", "schemathesis-baseline.toml"):
        os.unlink(str(work / name))
    inside = work / "inside.hurl"
    inside.write_text("keep\n", encoding="utf-8")
    os.symlink(str(inside), str(work / "smoke.hurl"))                                     # links that stay under .qa/work/
    os.symlink(str(work / "absent.yaml"), str(work / "spectral.yaml"))
    module = load()
    monkeypatch.chdir(f2)
    monkeypatch.setattr(os.path, "lexists", lambda path: False)
    monkeypatch.setattr(pathlib.Path, "exists", lambda self: False)
    monkeypatch.setattr(pathlib.Path, "is_symlink", lambda self: False)
    code = module.main(["assets"])
    monkeypatch.undo()
    body = json.loads(capsys.readouterr().out)
    assert code == 0 and body["copied"] == [".qa/work/api/schemathesis-baseline.toml"], body
    assert sorted(body["kept"]) == [".qa/work/api/smoke.hurl", ".qa/work/api/spectral.yaml"]
    assert inside.read_text(encoding="utf-8") == "keep\n" and not (work / "absent.yaml").exists()
    assert os.path.islink(str(work / "smoke.hurl")) and os.path.islink(str(work / "spectral.yaml"))


def test_the_assets_hold_the_bounds_the_skill_states():
    assets = SCRIPT.parent.parent / "assets"
    assert "extends: spectral:oas" in (assets / "spectral.yaml").read_text(encoding="utf-8")
    toml = (assets / "schemathesis-baseline.toml").read_text(encoding="utf-8")
    for line in ('rate-limit = "10/s"', "max-failures = 20", "request-timeout = 5", "max-examples = 20"):
        assert line in toml, line
    sections = dict(re.findall(r"^\[phases\.(\w+)\]\s*\nenabled = (true|false)", toml, re.M))
    assert sections == {"examples": "true", "coverage": "true", "fuzzing": "false", "stateful": "false"}
    hurl = (assets / "smoke.hurl").read_text(encoding="utf-8")
    assert "{{base}}" in hurl and "http://" not in hurl.replace("http://127.0.0.1", "")
