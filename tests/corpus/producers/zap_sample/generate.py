"""Producer for the ZAP SARIF corpus entry.

Usage: python3 generate.py WORKSPACE OUTPUT

Runs ZAP 2.17 with the Automation Framework plan zap-baseline.yaml (spider, passive checks, ``sarif-json`` report; no
activeScan job; the report job names its file zap.json) against hello_service.py, the run's fixture service on
loopback. The command line is exactly the baseline form the guard reads as passive, so WORKSPACE needs a
``dast-passive`` grant on environment ``local`` for target ``127.0.0.1``. ZAP's update check fails silently with no
network."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import container_run  # noqa: E402

HERE = Path(__file__).resolve().parent
SCRIPT = (container_run.WAIT + "QA_TARGET=%s zap.sh -cmd -notel -autorun /work/.qa-input/security/zap-baseline.yaml"
          % container_run.TARGET)


def main(argv: list) -> int:
    workspace, output = container_run.main_args(argv, "generate.py WORKSPACE OUTPUT")
    inputs = container_run.stage(workspace, {"security/zap-baseline.yaml": HERE / "zap-baseline.yaml",
                                             "service/hello_service.py": HERE.parent / "hello_service.py"})
    result = container_run.run(workspace, "zap", SCRIPT, inputs=inputs, reports=(("sarif", "{evidence}/zap.json"),),
                               service=True, timeout_s=600)
    container_run.copy_out(container_run.stored_report(workspace, result), output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
