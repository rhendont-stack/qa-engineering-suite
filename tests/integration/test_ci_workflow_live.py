"""The workflow `qa ci generate` writes (tests/corpus/ci/qa-reexecution.yml) through actionlint and zizmor, each in its pinned
image on a real engine, offline. Skipped unless QA_TEST_CONTAINER_ENGINE names one.

Each tool runs through `qa run --container` (D4) as static analysis, so the kernel records the run; its findings are read
from the run's own stdout evidence, because only declared reports leave the container and neither tool writes one here. A
control workflow with an unpinned action and an expression in a run step shows that both tools, run this way, do report.
"""
from __future__ import annotations

import json
import os

import pytest

from helpers import CORPUS
from qa import config, execution, resource_dir, state

pytestmark = pytest.mark.docker

GENERATED = ".github/workflows/qa-reexecution.yml"
CONTROL = ".github/workflows/control.yml"
CONTROL_TEXT = ("name: control\n"
                "on: issues\n"
                "jobs:\n"
                "  echo:\n"
                "    runs-on: ubuntu-24.04\n"
                "    steps:\n"
                "      - uses: actions/checkout@v4\n"
                "      - run: echo \"${{ github.event.issue.title }}\"\n")


def commands(workflow):
    return {"actionlint": ["actionlint", "-no-color", "-format", "{{json .}}", workflow],
            "zizmor": ["zizmor", "--offline", "--no-progress", "--format", "json", workflow]}


def image(name):
    images = json.loads((resource_dir("policies") / "container-images.json").read_text(encoding="utf-8"))["images"]
    [entry] = [found for found in images if found["id"] == name]
    return entry["image"]


@pytest.fixture
def live(ws, actor):
    text = os.environ.get("QA_TEST_CONTAINER_ENGINE")
    if not text:
        pytest.skip("tool missing: QA_TEST_CONTAINER_ENGINE is not set")
    config.record_initialized(ws, actor)
    config.set_value(ws, actor, "container.engine", text, "the live suite's own engine")
    (ws.root / ".github" / "workflows").mkdir(parents=True)
    (ws.root / GENERATED).write_bytes((CORPUS / "ci" / "qa-reexecution.yml").read_bytes())
    (ws.root / CONTROL).write_text(CONTROL_TEXT, encoding="utf-8")
    return ws


def findings(ws, actor, name, workflow):
    """(exit code, the findings the tool printed as JSON, its stderr) for one recorded container run."""
    result = execution.execute(ws, actor, execution.RunRequest(command=commands(workflow)[name], activity="static-analysis",
                                                               category="maintainability", container=image(name),
                                                               timeout_s=300))
    record = state.load(ws)["executions"][result["execution"]]
    stdout = ws.path("evidence", result["execution"], "stdout.log").read_text(encoding="utf-8")
    stderr = ws.path("evidence", result["execution"], "stderr.log").read_text(encoding="utf-8")
    assert record["isolation"] == "container" and record["status"] != "blocked", (name, result, stderr)
    return record["exit_code"], (json.loads(stdout) if stdout.strip() else None) or [], stderr


def test_generated_workflow_passes_actionlint_and_zizmor(live, actor):
    for name in ("actionlint", "zizmor"):
        code, found, stderr = findings(live, actor, name, CONTROL)
        assert code != 0 and found, (name, code, found, stderr)  # the tool reads the file and reports, run this way
        code, found, stderr = findings(live, actor, name, GENERATED)
        assert (code, found) == (0, []), (name, found, stderr)
