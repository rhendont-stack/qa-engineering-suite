"""Producer for the Spectral SARIF corpus entry.

Usage: python3 generate.py WORKSPACE OUTPUT

Lints openapi.yaml with Spectral's bundled ``spectral:oas`` rules (ruleset.yaml) and ``--format sarif``, network none,
telemetry off (``DO_NOT_TRACK=1 SCARF_NO_ANALYTICS=true``). The spec leaves out what those rules ask for (contact,
description, servers, tags, an operation id), so the report has warnings to count."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import container_run  # noqa: E402

HERE = Path(__file__).resolve().parent
SCRIPT = ("DO_NOT_TRACK=1 SCARF_NO_ANALYTICS=true spectral lint --ruleset /work/.qa-input/spectral/ruleset.yaml "
          "--format sarif --output /qa-evidence/spectral.sarif /work/.qa-input/spectral/openapi.yaml")


def main(argv: list) -> int:
    workspace, output = container_run.main_args(argv, "generate.py WORKSPACE OUTPUT")
    inputs = container_run.stage(workspace, {"spectral/openapi.yaml": HERE / "openapi.yaml",
                                             "spectral/ruleset.yaml": HERE / "ruleset.yaml"})
    result = container_run.run(workspace, "spectral", SCRIPT, inputs=inputs, reports=(("sarif", "{evidence}/spectral.sarif"),))
    container_run.copy_out(container_run.stored_report(workspace, result), output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
