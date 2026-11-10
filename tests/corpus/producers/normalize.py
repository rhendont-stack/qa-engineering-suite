"""Remove machine-specific details from freshly generated corpus files.

Usage: normalize.py <file> [<file> ...]

Rewrites each file in place, replacing the hostname, timestamps, absolute
paths, container paths, coverage report epochs and session ids, Surefire system property values and the user name
with fixed tokens. Run it on every corpus file after
the producer has written it.
"""
from __future__ import annotations

import getpass
import os
import re
import socket
import sys
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[3]

TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")
# Every producer makes its temporary directory with tempfile.mkdtemp(prefix="qa-<producer>-").
TEMP_DIR = re.compile(r"(?:/private)?/(?:var/folders/[^/\s\"]+/[^/\s\"]+/T|tmp)/qa-[a-z0-9-]+-[A-Za-z0-9_]+")
HOSTNAME_ATTR = re.compile(r'hostname="[^"]*"')
# Surefire lists the whole JVM environment (user, home, paths, OS) as <property name=".." value=".."/>.
# No value is kept: a fixed token cannot name this machine, whoever runs the producer.
SUREFIRE_PROPERTY = re.compile(r'(<property\b[^>]*?\bvalue=")[^"]*(")')
# Coverage reports date themselves in epoch milliseconds: Cobertura's timestamp, JaCoCo's session start and dump.
EPOCH_ATTR = re.compile(r'\b(timestamp|start|dump)="\d{10,13}"')
# JaCoCo names the session after the host that ran it: "<host>-<hex>".
SESSION_ID = re.compile(r'(<sessioninfo\b[^>]*?\bid=")[^"]*(")')
# The Maven container runs as root with the project at /work and its repository at /root/.m2.
CONTAINER_PATHS = re.compile(r"/root(?![\w.-])|/work(?![\w.-])")


# Names that identify nobody and occur in real tool output: never checked, never replaced.
EXEMPT_NAMES = frozenset({"root", "runner", "localhost"})


def home_pattern(home: str) -> Optional["re.Pattern[str]"]:
    """A home directory matches as a path: not followed by a name character."""
    if not home or Path(home).parent == Path(home):  # empty or the filesystem root
        return None
    return re.compile(re.escape(home) + r"(?![\w.-])")


def name_pattern(name: str) -> Optional["re.Pattern[str]"]:
    """A user or host name matches as a whole token, so `gitlab-alice-token` is not a hit."""
    if not name or name.lower() in EXEMPT_NAMES:
        return None
    return re.compile(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-])")


def sensitive_patterns() -> List[Tuple[str, "re.Pattern[str]", str]]:
    """(label, pattern, replacement token) for this machine, most specific first."""
    host = socket.gethostname()
    home = str(Path.home())
    candidates = [
        ("root directory", re.compile(re.escape(str(ROOT)) + r"(?![\w.-])"), "{ROOT}"),
        ("home directory", home_pattern(home), "{HOME}"),
        ("host name", name_pattern(host), "HOST"),
        ("host name", name_pattern(host.split(".")[0]), "HOST"),
        ("user name", name_pattern(getpass.getuser()), "USER"),
        ("user name", name_pattern(os.path.basename(home)), "USER"),
    ]
    return [(label, pat, token) for label, pat, token in candidates if pat is not None]


def normalize_text(text: str) -> str:
    text = HOSTNAME_ATTR.sub('hostname="HOST"', text)
    text = SUREFIRE_PROPERTY.sub(r"\1{PROPERTY}\2", text)
    text = CONTAINER_PATHS.sub(lambda m: "{CONTAINER_HOME}" if m.group(0) == "/root" else "{WORK}", text)
    text = EPOCH_ATTR.sub(lambda m: '%s="0"' % m.group(1), text)
    text = SESSION_ID.sub(r"\1SESSION\2", text)
    text = TEMP_DIR.sub("{TMP}", text)
    text = TIMESTAMP.sub("1970-01-01T00:00:00.000+00:00", text)
    for _label, pattern, token in sensitive_patterns():
        text = pattern.sub(token, text)
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
