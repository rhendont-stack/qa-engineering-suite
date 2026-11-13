"""Producer for the oasdiff JUnit corpus entry.

Usage: python3 generate.py WORKSPACE OUTPUT

Runs ``oasdiff breaking base.yaml revision.yaml --fail-on ERR --format junit``, network none. The revision removes an
operation and makes a query parameter required, so oasdiff reports breaking changes and exits 1."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import container_run  # noqa: E402

HERE = Path(__file__).resolve().parent
SCRIPT = ("oasdiff breaking /work/.qa-input/oasdiff/base.yaml /work/.qa-input/oasdiff/revision.yaml --fail-on ERR "
          "--format junit > {evidence}/oasdiff.xml")


def main(argv: list) -> int:
    workspace, output = container_run.main_args(argv, "generate.py WORKSPACE OUTPUT")
    inputs = container_run.stage(workspace, {"oasdiff/base.yaml": HERE / "base.yaml", "oasdiff/revision.yaml": HERE / "revision.yaml"})
    result = container_run.run(workspace, "oasdiff", SCRIPT, inputs=inputs, reports=(("junit", "{evidence}/oasdiff.xml"),))
    container_run.copy_out(container_run.stored_report(workspace, result), output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
