"""Producer for the axe-core corpus entry.

Usage: python3 generate.py WORKSPACE OUTPUT

Runs ``axe-run.mjs`` (Chromium and ``@axe-core/playwright``) against ``page_service.py``, the run's fixture page on loopback,
in the pinned Playwright image. The install step puts the frontend runner's locked packages in the project
(``npm ci --ignore-scripts`` from ``frontend-runner/package-lock.json``, copied to the project root); the test step has no
network. WORKSPACE needs an ``install-dependencies`` grant on environment ``local`` (see container_run.py). The results
object is declared as an ``axe`` report, and the copy the run stored is the corpus file.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import container_run  # noqa: E402

HERE = Path(__file__).resolve().parent
RUNNER = HERE.parent / "frontend-runner"
INSTALL = "npm ci --ignore-scripts --no-audit --no-fund"
SERVICE_RUN = "exec python3 /work/.qa-input/service/page_service.py %d" % container_run.PORT
SCRIPT = (container_run.WAIT + "node /work/.qa-input/frontend/axe-run.mjs %s/ /qa-evidence/axe.json" % container_run.TARGET)


def prepare(workspace: Path) -> None:
    """The runner's package.json and lockfile at the project root, where ``npm ci`` reads them."""
    for name in ("package.json", "package-lock.json"):
        shutil.copyfile(str(RUNNER / name), str(workspace / name))


def main(argv: list) -> int:
    workspace, output = container_run.main_args(argv, "generate.py WORKSPACE OUTPUT")
    prepare(workspace)
    inputs = container_run.stage(workspace, {"service/page_service.py": HERE / "page_service.py",
                                             "frontend/axe-run.mjs": HERE / "axe-run.mjs"})
    result = container_run.run(workspace, "playwright", SCRIPT, inputs=inputs, reports=(("axe", "{evidence}/axe.json"),),
                               install=INSTALL, service=True, service_run=SERVICE_RUN, timeout_s=600)
    container_run.copy_out(container_run.stored_report(workspace, result), output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
