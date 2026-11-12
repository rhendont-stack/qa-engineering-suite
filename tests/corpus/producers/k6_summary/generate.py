"""Producer for the k6 corpus entry.

Usage: python3 generate.py WORKSPACE OUTPUT

Runs thresholds.js with ``k6 run --no-usage-report`` in the pinned k6 image, network none and no service: the script
makes no request. WORKSPACE needs a ``load-test`` grant on environment ``local`` (see container_run.py). The summary is
declared as a ``k6`` report; ``--from-stdout`` takes it from the run's stdout instead, for a kernel that does not read
``k6`` reports yet. k6 exits 99 because one threshold fails, which is the point of the sample.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import container_run  # noqa: E402

HERE = Path(__file__).resolve().parent
SCRIPT = "k6 run --no-usage-report /work/.qa-input/k6/thresholds.js"


def main(argv: list) -> int:
    from_stdout = "--from-stdout" in argv
    workspace, output = container_run.main_args([a for a in argv if a != "--from-stdout"],
                                                "generate.py WORKSPACE OUTPUT [--from-stdout]")
    inputs = container_run.stage(workspace, {"k6/thresholds.js": HERE / "thresholds.js"})
    reports = () if from_stdout else (("k6", "{evidence}/k6-summary.json"),)
    result = container_run.run(workspace, "k6", SCRIPT, inputs=inputs, reports=reports, timeout_s=120)
    if from_stdout:  # k6 prints its banner before the summary and its progress lines after it
        text = (container_run.evidence(workspace, result) / "stdout.log").read_text(encoding="utf-8")
        start = text.index("{\n")
        end = start + json.JSONDecoder().raw_decode(text[start:])[1]
        source = workspace / ".qa" / "work" / "k6" / "summary-from-stdout.json"
        source.write_text(text[start:end] + "\n", encoding="utf-8")
    else:
        source = container_run.stored_report(workspace, result)
    container_run.copy_out(source, output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
