"""Producer for the Hurl JUnit corpus entry.

Usage: python3 generate.py WORKSPACE OUTPUT

Runs ``hurl --test --report-junit`` on hello.hurl (holds) and broken.hurl (fails on purpose) against hello_service.py,
the run's fixture service on loopback. Hurl sends exactly the requests the files hold, so no grant beyond the
workspace's default is needed."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import container_run  # noqa: E402

HERE = Path(__file__).resolve().parent
SCRIPT = (container_run.WAIT + "hurl --test --report-junit /qa-evidence/hurl.xml --variable base=%s "
          "/work/.qa-input/hurl/hello.hurl /work/.qa-input/hurl/broken.hurl" % container_run.TARGET)


def main(argv: list) -> int:
    workspace, output = container_run.main_args(argv, "generate.py WORKSPACE OUTPUT")
    inputs = container_run.stage(workspace, {"hurl/hello.hurl": HERE / "hello.hurl", "hurl/broken.hurl": HERE / "broken.hurl",
                                             "service/hello_service.py": HERE.parent / "hello_service.py"})
    result = container_run.run(workspace, "hurl", SCRIPT, inputs=inputs, reports=(("junit", "{evidence}/hurl.xml"),),
                               service=True, timeout_s=300)
    container_run.copy_out(container_run.stored_report(workspace, result), output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
