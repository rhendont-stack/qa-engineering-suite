"""F4 ``tested-app``: a library with pytest, Vitest and JUnit 5 suites, run through the real CLI against the answer key.

The pytest leg needs no container. The Vitest and Gradle legs run in containers by digest and install their dependencies
there, under a grant the test records through ``qa authorize``. They are skipped unless QA_TEST_CONTAINER_ENGINE names an engine.
"""
from __future__ import annotations

import configparser
import fnmatch
import json
import os
import sys

import pytest

from helpers import FIXTURES, REPO, answer_key

PYTEST_RUN = ("run", "--report", "junit:{evidence}/junit.xml", "--", sys.executable, "-m", "pytest", "-q", "-o", "junit_family=xunit2",
              "--junitxml={evidence}/junit.xml")
FLAKY = "tests/test_sync.py::test_sync_settles"


def image(image_id):
    listing = json.loads((REPO / "shared" / "policies" / "container-images.json").read_text(encoding="utf-8"))
    return next(entry["image"] for entry in listing["images"] if entry["id"] == image_id)


@pytest.fixture
def container_project(qa_on_fixture):
    engine = os.environ.get("QA_TEST_CONTAINER_ENGINE")
    if not engine:
        pytest.skip("tool missing: QA_TEST_CONTAINER_ENGINE is not set")
    project = qa_on_fixture("tested-app")
    assert project.cli("config", "set", "container.engine", engine, "--approval", "fixture test")[0] == 0
    return project


def grant_install(project):
    code, out = project.cli("authorize", "--environment", "local", "--activity", "install-dependencies",
                            "--approval", "fixture test: dependencies may install inside the container")
    assert code == 0 and out["ok"]


def test_f4_statuses_match_the_key(qa_on_fixture):
    key = answer_key("tested-app")["suites"]["python"]
    project = qa_on_fixture("tested-app")
    code, ran = project.cli(*PYTEST_RUN)
    assert (code, ran["status"]) == (1, key["run_status"])
    assert project.results(ran["execution"]) == key["tests"]
    assert ran["tests"]["total"] == len(key["tests"])


def test_f4_flaky_is_failed_and_flaky_with_ratio(qa_on_fixture):
    expected = answer_key("tested-app")["suites"]["python"]["flaky"]
    (test_id, want), = expected.items()
    project = qa_on_fixture("tested-app")
    code, first = project.cli(*PYTEST_RUN)
    assert code == 1 and project.results(first["execution"])[test_id] == "failed"
    last = None
    for passes in (True, False, True):  # three isolated re-runs of the one test: pass, fail, pass
        code, last = project.cli("run", "--attempt-of", first["execution"], "--report", "junit:{evidence}/junit.xml", "--", sys.executable,
                                 "-m", "pytest", "-q", "-o", "junit_family=xunit2", "--junitxml={evidence}/junit.xml", FLAKY)
        assert code == (0 if passes else 1), last
        assert last["tests"]["total"] == 1
    view = last["flaky"][test_id]
    assert (view["status"], view["flaky"], view["attempts"], view["passes"], view["ratio"]) == ("failed", True, want["attempts"], want["passes"], want["ratio"])


def test_f4_install_needs_the_grant_and_nothing_starts_without_it(qa_on_fixture):
    project = qa_on_fixture("tested-app")
    code, out = project.cli("run", "--container", image("node-lts"), "--install", "npm ci --ignore-scripts", "--", "npx", "--no", "vitest", "run",
                            cwd=project.root / "web")
    assert (code, out["status"], out["reason"]) == (3, "blocked", "not_authorized")


@pytest.mark.docker
def test_f4_vitest_suite_matches_the_key_in_a_container(container_project):
    key = answer_key("tested-app")["suites"]["vitest"]
    grant_install(container_project)
    code, ran = container_project.cli(
        "run", "--container", image("node-lts"), "--install", "npm ci --ignore-scripts", "--report", "junit:{evidence}/junit.xml", "--",
        "npx", "--no", "vitest", "run", "--reporter=junit", "--outputFile={evidence}/junit.xml", cwd=container_project.root / "web")
    assert (code, ran["status"]) == (1, key["run_status"]), ran
    assert container_project.results(ran["execution"]) == key["tests"]


@pytest.mark.docker
def test_f4_gradle_suite_matches_the_key_in_a_container(container_project):
    key = answer_key("tested-app")["suites"]["gradle"]
    grant_install(container_project)
    home = "GRADLE_USER_HOME=/work/.gradle"  # the one directory that survives from the install step to the offline test step
    code, ran = container_project.cli(
        "run", "--container", image("temurin-lts"), "--install", home + " sh gradlew --no-daemon fetchDependencies",
        "--report", "junit:{evidence}/gradle/*.xml", "--shell", "--", home + " sh gradlew --offline --no-daemon test -PjunitDir={evidence}/gradle",
        cwd=container_project.root / "jvm")
    assert (code, ran["status"]) == (1, key["run_status"]), ran
    assert container_project.results(ran["execution"]) == key["tests"]


def test_the_fixtures_own_tests_are_not_part_of_the_suite():
    config = configparser.ConfigParser()
    config.read(str(REPO / "pytest.ini"))
    assert config["pytest"]["testpaths"].split() == ["tests"]
    skipped = config["pytest"]["norecursedirs"].split()
    assert {"fixtures", "benchmark"} <= set(skipped)
    for name in ("test_fixture_f4.py", "test_fixture_f8.py", "test_fixture_f9.py"):
        path = ("tests", "integration", name)
        assert REPO.joinpath(*path).is_file()
        assert not any(fnmatch.fnmatch(part, pattern) for part in path for pattern in skipped), name
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "fixtures" not in workflow and "pytest" in workflow
    assert (FIXTURES / "tested-app" / "pytest.ini").is_file()
