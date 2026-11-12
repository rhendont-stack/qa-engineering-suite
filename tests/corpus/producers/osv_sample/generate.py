"""Producer for the OSV-Scanner SARIF corpus entry.

Usage: python3 generate.py WORKSPACE OUTPUT

The install step (the only step with a network) fetches the PyPI offline database from
osv-vulnerabilities.storage.googleapis.com into the working copy; the scan step reads it with ``--offline`` and
writes SARIF, network none. WORKSPACE needs an ``install-dependencies`` grant and one ``network-download`` grant listing
that host. The image keeps the binary at /osv-scanner, off PATH.

Not yet run to completion: ``qa authorize`` accepts a grant's target only on a declared host, and the guard asks for
``network-download`` only for an undeclared one, so the corpus has no OSV entry until that is settled."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import container_run  # noqa: E402

HERE = Path(__file__).resolve().parent
DB = "/work/osv-db"
INSTALL = ("mkdir -p %s/osv-scanner/PyPI && wget -q -O %s/osv-scanner/PyPI/all.zip "
           "https://osv-vulnerabilities.storage.googleapis.com/PyPI/all.zip" % (DB, DB))
SCRIPT = ("OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY=%s /osv-scanner scan source --offline --format=sarif "
          "--output-file=/qa-evidence/osv.sarif /work/.qa-input/osv" % DB)


def main(argv: list) -> int:
    workspace, output = container_run.main_args(argv, "generate.py WORKSPACE OUTPUT")
    inputs = container_run.stage(workspace, {"osv/requirements.txt": HERE / "requirements.txt"})
    result = container_run.run(workspace, "osv-scanner", SCRIPT, inputs=inputs, reports=(("sarif", "{evidence}/osv.sarif"),),
                               install=INSTALL, timeout_s=600)
    container_run.copy_out(container_run.stored_report(workspace, result), output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
