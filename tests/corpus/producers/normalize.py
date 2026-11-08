"""Remove machine-specific details from freshly generated corpus files.

Usage: normalize.py <file> [<file> ...]

Rewrites each file in place, replacing the hostname, timestamps, absolute
paths and the user name with fixed tokens. Run it on every corpus file after
the producer has written it.
"""
from __future__ import annotations

import getpass
import os
import re
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")
TEMP_DIR = re.compile(r"(?:/private)?/(?:var/folders/[^/\s\"]+/[^/\s\"]+/T|tmp)/qa-gitleaks-sample-[A-Za-z0-9_]+")
HOSTNAME_ATTR = re.compile(r'hostname="[^"]*"')


def normalize_text(text: str) -> str:
    text = HOSTNAME_ATTR.sub('hostname="HOST"', text)
    text = TEMP_DIR.sub("{TMP}", text)
    text = TIMESTAMP.sub("1970-01-01T00:00:00.000+00:00", text)
    host = socket.gethostname()
    for literal, token in (
        (str(ROOT), "{ROOT}"),
        (str(Path.home()), "{HOME}"),
        (host, "HOST"),
        (host.split(".")[0], "HOST"),
        (getpass.getuser(), "USER"),
        (os.path.basename(str(Path.home())), "USER"),
    ):
        if literal:
            text = text.replace(literal, token)
    return text


def main(argv: list) -> int:
    if len(argv) < 2:
        print("usage: normalize.py <file> [<file> ...]", file=sys.stderr)
        return 2
    for name in argv[1:]:
        path = Path(name)
        path.write_text(normalize_text(path.read_text(encoding="utf-8")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
