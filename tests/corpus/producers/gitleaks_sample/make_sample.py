"""Producer for the gitleaks SARIF corpus entry.

Usage: make_sample.py <output.sarif>

Writes one file holding an AWS-style access key into a temporary directory,
scans that directory with `gitleaks dir --redact`, and deletes the directory.
The key is built at runtime from fixed parts, so no literal key is committed.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# The key is "AKIA" + the two parts below. Tests rebuild it from these names.
KEY_PREFIX = "AK" + "IA"
KEY_BODY_PARTS = ("QYLPMN5H", "HHTKR3WZ")


def build_key() -> str:
    return KEY_PREFIX + "".join(KEY_BODY_PARTS)


def main(argv: list) -> int:
    if len(argv) != 2:
        print("usage: make_sample.py <output.sarif>", file=sys.stderr)
        return 2
    out = Path(argv[1]).resolve()
    tmp = Path(tempfile.mkdtemp(prefix="qa-gitleaks-sample-"))
    try:
        (tmp / "settings.py").write_text('aws_access_key_id = "%s"\n' % build_key(), encoding="utf-8")
        cmd = ["gitleaks", "dir", "--redact", "--report-format", "sarif", "--report-path", str(out), str(tmp)]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        sys.stdout.write(proc.stdout)
        # gitleaks exits 1 when it finds a leak, which is the point of the sample.
        return 0 if proc.returncode in (0, 1) else proc.returncode
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
