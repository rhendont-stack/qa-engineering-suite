"""Producer for the Lighthouse corpus entry.

Usage: python3 generate.py WORKSPACE OUTPUT

Runs Lighthouse 13 (the locked ``lighthouse`` package, ``--only-categories=performance``) against the axe sample's fixture
page on loopback, headless in the pinned Playwright image's Chromium (``CHROME_PATH``). Install and grants are as in
``axe_sample/generate.py``. The result is declared as a ``lighthouse`` report, and the copy the run stored is the corpus file.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import container_run  # noqa: E402

# The axe producer's file, loaded by path: both are named generate.py.
_spec = importlib.util.spec_from_file_location("axe_generate", str(Path(__file__).resolve().parents[1] / "axe_sample" / "generate.py"))
axe_producer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(axe_producer)

CHROME = "/ms-playwright/chromium-1255/chrome-linux64/chrome"
SCRIPT = (container_run.WAIT + "CHROME_PATH=%s node_modules/.bin/lighthouse %s/ --quiet --output=json "
          "--output-path=/qa-evidence/lighthouse.json --only-categories=performance "
          "--chrome-flags='--headless=new --no-sandbox --disable-gpu'" % (CHROME, container_run.TARGET))


def main(argv: list) -> int:
    workspace, output = container_run.main_args(argv, "generate.py WORKSPACE OUTPUT")
    axe_producer.prepare(workspace)
    inputs = container_run.stage(workspace, {"service/page_service.py": axe_producer.HERE / "page_service.py"})
    result = container_run.run(workspace, "playwright", SCRIPT, inputs=inputs, reports=(("lighthouse", "{evidence}/lighthouse.json"),),
                               install=axe_producer.INSTALL, service=True, service_run=axe_producer.SERVICE_RUN, timeout_s=900)
    container_run.copy_out(container_run.stored_report(workspace, result), output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
