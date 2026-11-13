"""Producer for the OSV-Scanner SARIF corpus entry.

Usage: python3 generate.py WORKSPACE OUTPUT

The install step (the only step with a network) fetches the PyPI offline database from
osv-vulnerabilities.storage.googleapis.com into the working copy; the scan step reads it with ``--offline`` and
writes SARIF, network none. The image keeps the binary at /osv-scanner, off PATH. OSV-Scanner 2.7.1 looks for the
database at ``$OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY/osv-scalibr/<ecosystem>/all.zip`` (an ``osv-scanner`` directory
there is not read), so the install step writes it there.

What a run needs in WORKSPACE, as the kernel checks it: a grant names only declared hosts, so the configuration first
declares an environment for the database host (``osv-data``, class ``production``, base URL
``https://osv-vulnerabilities.storage.googleapis.com``), and a ``network-download`` grant there names the host, as the
spec's activity table asks. A run whose install step fetches from that host belongs to that environment as well as to
``local``, so it also needs ``install-dependencies`` and ``test-execution-local`` on ``osv-data``, in a grant whose
target is the host (a grant with no target covers no host), and ``install-dependencies`` on ``local``. Once the host
is declared, the guard itself no longer asks for ``network-download``."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import container_run  # noqa: E402

HERE = Path(__file__).resolve().parent
DB = "/work/osv-db"
INSTALL = ("mkdir -p %s/osv-scalibr/PyPI && wget -q -O %s/osv-scalibr/PyPI/all.zip "
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
