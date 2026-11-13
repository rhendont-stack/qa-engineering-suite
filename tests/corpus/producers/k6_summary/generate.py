"""Producer for the k6 corpus entry.

Usage: python3 generate.py WORKSPACE OUTPUT

Runs thresholds.js with ``k6 run --no-usage-report`` in the pinned k6 image, network none and no service: the script
makes no request. WORKSPACE needs a ``load-test`` grant on environment ``local`` (see container_run.py). The summary is
declared as a ``k6`` report, and the copy the run stored is the corpus file. k6 exits 99 because one threshold fails,
which is the point of the sample.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import container_run  # noqa: E402

HERE = Path(__file__).resolve().parent
SCRIPT = "k6 run --no-usage-report /work/.qa-input/k6/thresholds.js"


def main(argv: list) -> int:
    workspace, output = container_run.main_args(argv, "generate.py WORKSPACE OUTPUT")
    inputs = container_run.stage(workspace, {"k6/thresholds.js": HERE / "thresholds.js"})
    result = container_run.run(workspace, "k6", SCRIPT, inputs=inputs, reports=(("k6", "{evidence}/k6-summary.json"),),
                               timeout_s=120)
    container_run.copy_out(container_run.stored_report(workspace, result), output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
