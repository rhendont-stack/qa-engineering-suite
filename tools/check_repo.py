"""Repository checks (L0): no empty or placeholder files, valid JSON, valid policies, closed event schemas.

Usage: python3 -I tools/check_repo.py [root]      (root defaults to the current directory)

In a git work tree it checks exactly the files git lists (tracked, plus untracked and not ignored), so ignored local
directories are never read. Anywhere else it walks the root and skips ``.git``, ``.qa`` (except ``config.json``),
``__pycache__``, ``.venv*``, ``.tools``, ``.superpowers``, ``node_modules`` and ``.DS_Store``. Only regular files are
read: a symlink, device, FIFO or socket, and a listed file that has since been deleted, are skipped.

Hygiene: files under ``tests/`` and ``tools/`` must not hold an IPv4 address (loopback, unspecified, documentation ranges
and well-known public resolvers excepted) or an email address outside reserved domains (``example.*``, ``.invalid``,
``.test``, ``.local``, ``.localhost``, ``.internal``). Strings that must never be published (a private host alias, a
name) go one per line in an optional, git-ignored ``.private-terms`` file at the root; every checked file outside ``.qa/``
is searched for them, case-insensitively, and a hit is reported by line number without repeating the term.

Prints one line per problem, ``<path>: <kind>: <detail>``, sorted, then ``<n> problem(s)``. Exit 1 on any problem.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

# ``-I`` ignores PYTHONPATH: use the kernel of the repository this script belongs to, whichever root it checks.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared" / "kernel"))

from qa import gitinfo, schema  # noqa: E402
from qa.canonical import loads  # noqa: E402
from qa.errors import QaError  # noqa: E402

POLICY_SCHEMAS = {
    "redaction.json": "redaction-policy.schema.json",
    "command-guard.json": "command-guard.schema.json",
    "severity-rubric.json": "severity-rubric.schema.json",
    "priority-matrix.json": "priority-matrix.schema.json",
    "container-images.json": "container-images.schema.json",
}
GATE_SCHEMA = "gate-policy.schema.json"
PLACEHOLDER_WORDS = (b"todo", b"tbd", b"placeholder", b"...")
SKIPPED_DIRECTORIES = (".git", "__pycache__", ".tools", ".superpowers", "node_modules")

Problem = Tuple[str, str, str]  # path, kind, detail

PRIVATE_TERMS_FILE = ".private-terms"
HYGIENE_DIRECTORIES = ("tests", "tools")
IPV4 = re.compile(r"(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\w.])")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
ALLOWED_IPS = {"0.0.0.0", "1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4", "9.9.9.9"}
DOCUMENTATION_NETS = ((192, 0, 2), (198, 51, 100), (203, 0, 113))
RESERVED_EMAIL_TLDS = ("example", "invalid", "test", "local", "localhost", "internal")
RESERVED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net")


def git_listing(root: Path) -> Optional[List[str]]:
    """The files git lists for ``root``, or None when ``root`` is not a git work tree or git cannot say."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        session = gitinfo._Git(git, root)  # the kernel's hardened git: nothing the checked tree configures runs
        inside = session.run(["rev-parse", "--is-inside-work-tree"])
        if inside.returncode != 0 or inside.stdout.strip() != b"true":
            return None
        listed = session.run(["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    except gitinfo._Unavailable:
        return None
    if listed.returncode != 0:
        return None
    return sorted({os.fsdecode(name) for name in listed.stdout.split(b"\0") if name})


def walked(root: Path) -> Iterator[str]:
    for here, directories, files in os.walk(str(root), followlinks=False):
        relative = os.path.relpath(here, str(root))
        if relative == ".qa":
            directories[:] = []
            files = [name for name in files if name == "config.json"]
        else:
            directories[:] = sorted(d for d in directories
                                    if d not in SKIPPED_DIRECTORIES and not d.startswith(".venv"))
        for name in sorted(files):
            if name != ".DS_Store":
                yield name if relative == "." else os.path.join(relative, name)


def files_to_check(root: Path) -> List[str]:
    listed = git_listing(root)
    return listed if listed is not None else sorted(walked(root))


def is_placeholder(data: bytes) -> bool:
    text = data.strip()
    return not text or text.lower() in PLACEHOLDER_WORDS or (len(text.splitlines()) == 1 and text.startswith(b"#"))


def closed(doc: object) -> bool:
    """A schema is closed when it sets ``additionalProperties: false``, or is a ``oneOf`` of branches that all do."""
    if not isinstance(doc, dict):
        return False
    if doc.get("additionalProperties") is False:
        return True
    branches = doc.get("oneOf")
    return isinstance(branches, list) and bool(branches) and all(isinstance(b, dict) and b.get("additionalProperties") is False
                                                                   for b in branches)


def policy_schema(parts: Tuple[str, ...]) -> Optional[str]:
    """The schema a file under ``shared/policies/`` must satisfy; None for a policy no schema is mapped to."""
    name = parts[-1]
    if name in POLICY_SCHEMAS:
        return POLICY_SCHEMAS[name]
    return GATE_SCHEMA if name.startswith("gate.") and name.endswith(".json") and len(name) > len("gate..json") else None


def shared_parts(parts: Tuple[str, ...]) -> Tuple[str, ...]:
    """``skills/<skill>/scripts/_kernel/<kind>/...`` (a vendored copy) as ``shared/<kind>/...``, so a copy meets the same checks."""
    if len(parts) > 5 and parts[0] == "skills" and parts[2:4] == ("scripts", "_kernel") and parts[4] in ("policies", "schemas"):
        return ("shared",) + parts[4:]
    return parts


def check_file(relative: str, data: bytes) -> List[Problem]:
    found: List[Problem] = []
    if not data:
        return [(relative, "empty", "the file has no bytes")]
    if is_placeholder(data):
        found.append((relative, "placeholder", "the whole content is a placeholder"))
    if not relative.endswith(".json"):
        return found
    try:
        doc = loads(data)
    except QaError:
        return found + [(relative, "invalid_json", "the file is not valid JSON")]
    parts = shared_parts(tuple(relative.replace(os.sep, "/").split("/")))
    if len(parts) == 3 and parts[:2] == ("shared", "policies"):
        wanted = policy_schema(parts)
        if wanted is None:
            found.append((relative, "policy_invalid", "no schema is mapped to this policy file"))
        else:
            try:
                schema.validate(doc, wanted)
            except QaError as exc:
                found.append((relative, "policy_invalid", "does not satisfy %s: %s" % (wanted, first_error(exc))))
    elif len(parts) == 4 and parts[:3] == ("shared", "schemas", "events") and parts[3].endswith(".schema.json"):
        if not closed(doc):
            found.append((relative, "schema_not_closed", "the top level does not set additionalProperties to false"))
    return found


def private_terms(root: Path) -> List[bytes]:
    """The lower-cased terms of ``<root>/.private-terms``, or none when the file is absent or not a regular file."""
    path = root / PRIVATE_TERMS_FILE
    try:
        if not stat.S_ISREG(os.lstat(str(path)).st_mode):
            return []
        lines = path.read_bytes().splitlines()
    except OSError:
        return []
    return [line.strip().lower() for line in lines if line.strip() and not line.strip().startswith(b"#")]


def _ip_allowed(octets: Tuple[int, ...]) -> bool:
    text = ".".join(map(str, octets))
    return octets[0] == 127 or text in ALLOWED_IPS or octets[:3] in DOCUMENTATION_NETS


def _email_allowed(domain: str) -> bool:
    domain = domain.lower()
    return domain.rsplit(".", 1)[-1] in RESERVED_EMAIL_TLDS or any(
        domain == allowed or domain.endswith("." + allowed) for allowed in RESERVED_EMAIL_DOMAINS)


def hygiene(relative: str, data: bytes, terms: List[bytes]) -> List[Problem]:
    """Private terms anywhere outside ``.qa/``; IP and email shapes in ``tests/`` and ``tools/``. Never echoes a term."""
    found: List[Problem] = []
    parts = relative.replace(os.sep, "/").split("/")
    if parts[0] == ".qa" or relative == PRIVATE_TERMS_FILE:
        return found
    if terms:
        for number, line in enumerate(data.lower().splitlines(), 1):
            if any(term in line for term in terms):
                found.append((relative, "private_term", "line %d holds a term listed in %s" % (number, PRIVATE_TERMS_FILE)))
    if parts[0] not in HYGIENE_DIRECTORIES:
        return found
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return found
    for number, line in enumerate(text.splitlines(), 1):
        for match in IPV4.finditer(line):
            octets = tuple(int(group) for group in match.groups())
            if all(o <= 255 for o in octets) and not _ip_allowed(octets):
                found.append((relative, "ip_address", "line %d holds an IP address; use a documentation address" % number))
        for match in EMAIL.finditer(line):
            if not _email_allowed(match.group(1)):
                found.append((relative, "email_address", "line %d holds an email address; use a reserved domain" % number))
    return found


def first_error(exc: QaError) -> str:
    errors = exc.details.get("errors")
    return str(errors[0]) if errors else exc.code


def check(root: Path) -> List[Problem]:
    found: List[Problem] = []
    terms = private_terms(root)
    for relative in files_to_check(root):
        path = root / relative
        try:
            if not stat.S_ISREG(os.lstat(str(path)).st_mode):
                continue
            data = path.read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            found.append((relative, "unreadable", "the file cannot be read: %s" % type(exc).__name__))
            continue
        found.extend(check_file(relative, data))
        found.extend(hygiene(relative, data, terms))
    return sorted(found)


def main(argv: List[str]) -> int:
    sys.stdout.reconfigure(errors="backslashreplace")
    if len(argv) > 1:
        print("usage: check_repo.py [root]")
        return 2
    root = Path(argv[0]) if argv else Path(".")
    if not root.is_dir():
        print("check_repo.py: not a directory: %s" % root)
        return 2
    found = check(root)
    for relative, kind, detail in found:
        print("%s: %s: %s" % (relative, kind, detail))
    print("%d problem(s)" % len(found))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
