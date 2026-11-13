"""Producer for the squawk corpus entry.

Usage: python3 generate.py WORKSPACE OUTPUT

Lints 0002_add_email_index.sql with ``squawk --reporter json``, network none. squawk 2.72.0 offers the reporters tty,
gcc, json and gitlab, none of them SARIF, so its JSON is kept as an attachment: qa-database converts it to SARIF.
The JSON goes to stdout, which the run stores as stdout.log."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import container_run  # noqa: E402

HERE = Path(__file__).resolve().parent
SCRIPT = "squawk --reporter json /work/.qa-input/squawk/0002_add_email_index.sql"


def main(argv: list) -> int:
    workspace, output = container_run.main_args(argv, "generate.py WORKSPACE OUTPUT")
    inputs = container_run.stage(workspace, {"squawk/0002_add_email_index.sql": HERE / "0002_add_email_index.sql"})
    result = container_run.run(workspace, "squawk", SCRIPT, inputs=inputs)
    container_run.copy_out(container_run.evidence(workspace, result) / "stdout.log", output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
