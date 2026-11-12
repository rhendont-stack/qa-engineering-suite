"""Producer for the Schemathesis JUnit corpus entry.

Usage: python3 generate.py WORKSPACE OUTPUT

Runs ``st run`` restricted to the examples and coverage phases (passive: the schema's own examples and boundary values,
no fuzzing) against hello_service.py, the run's fixture service on loopback. WORKSPACE needs a ``dast-passive`` grant on
environment ``local`` for target ``127.0.0.1``. ``/broken`` answers 500 where openapi.yaml promises 200, so one
operation fails, which is the point of the sample."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import container_run  # noqa: E402

HERE = Path(__file__).resolve().parent
SCRIPT = (container_run.WAIT + "st run /work/.qa-input/schemathesis/openapi.yaml --url=%s --phases=examples,coverage "
          "--report=junit --report-junit-path=/qa-evidence/schemathesis.xml" % container_run.TARGET)


def main(argv: list) -> int:
    workspace, output = container_run.main_args(argv, "generate.py WORKSPACE OUTPUT")
    inputs = container_run.stage(workspace, {"schemathesis/openapi.yaml": HERE / "openapi.yaml",
                                             "service/hello_service.py": HERE.parent / "hello_service.py"})
    result = container_run.run(workspace, "schemathesis", SCRIPT, inputs=inputs, reports=(("junit", "{evidence}/schemathesis.xml"),),
                               service=True, timeout_s=300)
    container_run.copy_out(container_run.stored_report(workspace, result), output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
