"""What every domain-tool producer shares: run the tool through ``qa run --container`` and copy out what it wrote.

A producer's ``generate.py`` calls ``produce``. The workspace it names must already exist (``qa init``), with its engine
chosen through ``qa config set container.engine`` and every grant the run needs recorded through ``qa authorize`` with
the user's approval: this module records no approval and no grant. Each run uses an image pinned in
``shared/policies/container-images.json`` and only files staged under the workspace's ``.qa/work/``, passed as
``--input`` (they land at ``/work/.qa-input/``). The fixture service, when a producer needs one, is
``hello_service.py`` on ``127.0.0.1:8000`` in the ``python-newest`` image: not 8080, where ZAP starts its own proxy in the
namespace the service and the tool share (moving ZAP's port takes an argument the guard reads as active scanning).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
LAUNCHER = ROOT / "skills" / "qa-test-execution" / "scripts" / "qa.py"
IMAGES = ROOT / "shared" / "policies" / "container-images.json"
PORT = 8000
TARGET = "http://127.0.0.1:%d" % PORT
SERVICE_RUN = "exec python3 /work/.qa-input/service/hello_service.py %d" % PORT
SERVICE_USER = "65534:65534"
# The service needs a moment to listen; every tool that talks to it waits this long first.
WAIT = "sleep 3; "


def image(tool_id: str) -> str:
    """The pinned ``IMAGE@DIGEST`` of a catalog entry."""
    for entry in json.loads(IMAGES.read_text(encoding="utf-8"))["images"]:
        if entry["id"] == tool_id:
            return entry["image"]
    raise SystemExit("no catalog image %r" % tool_id)


def tool_version(tool_id: str) -> str:
    for entry in json.loads(IMAGES.read_text(encoding="utf-8"))["images"]:
        if entry["id"] == tool_id:
            return entry["tool_version"]
    raise SystemExit("no catalog image %r" % tool_id)


def stage(workspace: Path, files: Dict[str, Path]) -> List[str]:
    """Copy each source to ``.qa/work/<name>`` and return the ``--input`` paths, relative to the workspace root."""
    inputs = []
    for name, source in files.items():
        target = workspace / ".qa" / "work" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(source), str(target))
        inputs.append(".qa/work/" + name)
    return inputs


def run(workspace: Path, tool_id: str, script: str, *, inputs: Sequence[str] = (), reports: Sequence[Tuple[str, str]] = (),
        install: Optional[str] = None, service: bool = False, timeout_s: int = 300) -> dict:
    """``qa run --container`` with the tool's pinned image; returns the run's JSON summary, printed as it arrives."""
    argv = [sys.executable, str(LAUNCHER), "--workspace", str(workspace), "run", "--container", image(tool_id),
            "--timeout", str(timeout_s), "--shell"]
    for path in inputs:
        argv += ["--input", path]
    for fmt, pattern in reports:
        argv += ["--report", "%s:%s" % (fmt, pattern)]
    if install is not None:
        argv += ["--install", install]
    if service:
        argv += ["--service", image("python-newest"), "--service-run", SERVICE_RUN, "--service-user", SERVICE_USER]
    argv += ["--", script]
    print("$ qa run --container %s ... -- %s" % (tool_id, script), flush=True)
    done = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    sys.stderr.write(done.stderr)
    result = json.loads(done.stdout)
    print(json.dumps({key: result.get(key) for key in ("execution", "status", "reason", "tests", "anomalies", "limitations")}))
    if "error" in result:  # refused before anything was recorded; a run that failed still has its evidence
        raise SystemExit("qa run refused: %s" % result["error"])
    return result


def evidence(workspace: Path, result: dict) -> Path:
    if not result.get("evidence"):
        raise SystemExit("the run stored no evidence: %s" % result.get("limitations"))
    return workspace / result["evidence"]


def copy_out(source: Path, output: Path) -> None:
    """Copy a file the run stored to the corpus and normalise it in place."""
    sys.path.insert(0, str(HERE))
    import normalize  # noqa: E402 (beside this file)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(normalize.normalize_text(source.read_text(encoding="utf-8")), encoding="utf-8")
    print("wrote %s" % output.relative_to(ROOT))


def stored_report(workspace: Path, result: dict) -> Path:
    """The one report file the run stored under ``reports/``."""
    found = sorted((evidence(workspace, result) / "reports").iterdir())
    if len(found) != 1:
        raise SystemExit("expected one stored report, found %d" % len(found))
    return found[0]


def main_args(argv: list, usage: str) -> Tuple[Path, Path]:
    if len(argv) != 3:
        raise SystemExit("usage: %s" % usage)
    return Path(argv[1]).resolve(), Path(argv[2]).resolve()
