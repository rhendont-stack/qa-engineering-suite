from __future__ import annotations

import json

import pytest

from qa.detect import stack, tests

CANARY_CODE = "import pathlib; pathlib.Path(__file__).with_name('canary').write_text('ran')\n"


@pytest.fixture
def tmp_project(tmp_path):
    """pyproject.toml + package.json + build.gradle, a CI workflow running pytest, a README documenting vitest."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n\n[tool.pytest.ini_options]\naddopts = "-q"\n')
    (tmp_path / "package.json").write_text(json.dumps({"name": "demo", "devDependencies": {"vitest": "^1.0.0"}}))
    (tmp_path / "build.gradle").write_text("// canary: this file must never run\nplugins { id 'java' }\n")
    (tmp_path / "conftest.py").write_text(CANARY_CODE)
    (tmp_path / "package-lock.json").write_text("{}")
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("on: push\njobs:\n  t:\n    steps:\n      - uses: actions/checkout@v4\n      - run: python -m pytest -q\n")
    (tmp_path / "README.md").write_text("# Demo\n\nRun the unit tests:\n\n```\n$ npx vitest run\n```\n")
    return tmp_path


def test_stack_cites_every_manifest(tmp_project):
    found = stack.detect(tmp_project)
    assert {m["path"] for m in found["manifests"]} == {"pyproject.toml", "package.json", "build.gradle"}
    assert set(found) == {"languages", "manifests", "lockfiles", "ci", "unknowns"}
    assert {"python", "javascript", "java"} <= set(found["languages"])
    assert found["lockfiles"] == ["package-lock.json"] and found["ci"] == [".github/workflows/ci.yml"]
    assert found["unknowns"] == []


def test_tests_ranked_ci_then_documented_then_inferred(tmp_project):
    found = tests.detect(tmp_project)
    assert [c["source_kind"] for c in found] == ["ci", "documented", "inferred"]
    assert [c["framework"] for c in found] == ["pytest", "vitest", "gradle"]
    ci = found[0]
    assert ci["command"] == "python -m pytest -q" and ci["cited_path"] == ".github/workflows/ci.yml"
    assert ci["report_flag"] == "--junitxml={evidence}/junit.xml" and ci["source"] == ".github/workflows/ci.yml:6"
    assert found[1]["command"] == "npx vitest run" and found[1]["cited_path"] == "README.md"
    assert found[2]["cited_path"] == "build.gradle"
    assert all(set(c) == {"framework", "command", "report_flag", "source", "source_kind", "cited_path"} for c in found)


def test_detection_never_executes_project_files(tmp_project):
    stack.detect(tmp_project)
    tests.detect(tmp_project)
    assert not (tmp_project / "canary").exists()


def test_unparseable_manifest_is_listed_as_unknown(tmp_project):
    (tmp_project / "package.json").write_text("{not json")
    found = stack.detect(tmp_project)
    assert "package.json" in [u["path"] for u in found["unknowns"]]
    assert "package.json" not in [m["path"] for m in found["manifests"]]
    assert "vitest" not in [c["framework"] for c in tests.detect(tmp_project) if c["source_kind"] == "inferred"]


def test_ci_block_scalar_and_package_script_resolution(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest --ci"}}))
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yaml").write_text("jobs:\n  t:\n    steps:\n      - run: |\n          npm ci\n          npm test\n          go test ./...\n")
    found = tests.detect(tmp_path)
    assert [(c["framework"], c["command"], c["source_kind"]) for c in found] == [
        ("jest", "npm test", "ci"), ("go test", "go test ./...", "ci")]


def test_nothing_detected_in_an_empty_project(tmp_path):
    assert tests.detect(tmp_path) == []
    assert stack.detect(tmp_path) == {"languages": [], "manifests": [], "lockfiles": [], "ci": [], "unknowns": []}


def test_detection_skips_dependency_directories(tmp_project):
    (tmp_project / "node_modules" / "dep").mkdir(parents=True)
    (tmp_project / "node_modules" / "dep" / "package.json").write_text("{}")
    assert "node_modules/dep/package.json" not in [m["path"] for m in stack.detect(tmp_project)["manifests"]]


def test_a_symlinked_manifest_is_unknown_not_followed(tmp_project, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "go.mod"
    outside.write_text("module x\n")
    (tmp_project / "go.mod").symlink_to(outside)
    found = stack.detect(tmp_project)
    assert "go.mod" in [u["path"] for u in found["unknowns"]] and "go.mod" not in [m["path"] for m in found["manifests"]]


def test_cli_detect_prints_one_json_object(ws, qa_cli):
    (ws.root / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = "-q"\n')
    code, out = qa_cli("detect", "stack")
    assert code == 0 and out["command"] == "detect stack" and out["ok"] is True
    assert [m["path"] for m in out["stack"]["manifests"]] == ["pyproject.toml"]
    code, out = qa_cli("detect", "tests")
    assert code == 0 and out["command"] == "detect tests"
    assert [(c["framework"], c["source_kind"]) for c in out["candidates"]] == [("pytest", "inferred")]


def test_installing_a_tool_is_not_running_it(tmp_project):
    (tmp_project / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  t:\n    steps:\n      - run: pip install pytest pytest-cov && npm i -D vitest jest-junit\n"
        "      - run: |\n          sudo apt-get install -y golang\n          npm ci\n          yarn add --dev jest\n"
        "      - run: python -m pytest -q\n")
    found = tests.detect(tmp_project)
    by_framework = {c["framework"]: c for c in found}
    assert by_framework["pytest"]["command"] == "python -m pytest -q" and by_framework["pytest"]["source_kind"] == "ci"
    assert by_framework["vitest"]["source_kind"] == "documented"  # the README's command, not `npm i -D vitest`
    assert "jest" not in by_framework
    assert all("install" not in c["command"] and " add " not in c["command"] for c in found)


def test_installing_pytest_alone_leaves_the_inferred_candidate(tmp_project):
    (tmp_project / ".github" / "workflows" / "ci.yml").write_text("jobs:\n  t:\n    steps:\n      - run: pip install pytest\n")
    pytest_found = [c for c in tests.detect(tmp_project) if c["framework"] == "pytest"]
    assert [(c["source_kind"], c["command"]) for c in pytest_found] == [("inferred", "python -m pytest")]
