"""Shared helpers of the qa-regression-guard scripts: read-only git through the kernel's neutralised session, a unified-diff
parser, and what kind of file a path is. Standard library only.

git runs only through the vendored kernel's ``gitinfo`` session: the repository's own hooks, filters, diff drivers, fsmonitor
and similar program settings run nothing, so reading a hostile repository's history executes none of its code. Every call
reads; nothing here writes into the project or ``.qa/``.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Set, Tuple

HERE = os.path.dirname(os.path.realpath(__file__))
KERNEL = os.path.join(HERE, "_kernel")
if not os.path.isdir(KERNEL):  # run from the source tree, before the kernel is vendored
    KERNEL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "shared", "kernel")
if not sys.path or sys.path[0] != KERNEL:
    sys.path.insert(0, KERNEL)  # first: this directory holds qa.py, the launcher, which would shadow the kernel package
if "qa" in sys.modules and not hasattr(sys.modules["qa"], "__path__"):
    del sys.modules["qa"]  # the launcher module, not the package

from qa import gitinfo  # noqa: E402

LAUNCHER = os.path.join(HERE, "qa.py")
if not os.path.isfile(LAUNCHER):
    LAUNCHER = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "shared", "launcher", "qa.py")
SKILL = "qa-regression-guard"

LANGUAGES = {".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
             ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript", ".java": "java", ".kt": "kotlin",
             ".kts": "kotlin", ".go": "go", ".rb": "ruby", ".php": "php", ".cs": "csharp", ".rs": "rust"}
TEST_DIRS = {"tests", "test", "__tests__", "spec", "specs", "testing"}
TEST_NAME = re.compile(r"^(?:test_.*\.py|.*_test\.py|.*\.(?:test|spec)\.[cm]?[jt]sx?|.*(?:Test|Tests|IT|Spec)\.(?:java|kt)|.*_test\.go|"
                       r".*_spec\.rb|.*Test\.php|.*Tests?\.cs)$")
TEST_SUPPORT = {"conftest.py", "setup-tests.js", "setupTests.js", "setupTests.ts", "jest.setup.js", "jest.setup.ts", "vitest.setup.ts",
                "vitest.setup.js", "test_helper.rb", "spec_helper.rb"}
CI_FILES = {".gitlab-ci.yml", ".travis.yml", "Jenkinsfile", "azure-pipelines.yml", "bitbucket-pipelines.yml", "appveyor.yml",
            ".drone.yml", "cloudbuild.yaml"}
CI_DIRS = ((".github", "workflows"), (".circleci",), (".buildkite",))
TEST_CONFIG = re.compile(r"^(?:pytest\.ini|tox\.ini|setup\.cfg|pyproject\.toml|\.coveragerc|noxfile\.py|package\.json|\.nycrc(?:\.json)?|"
                         r"(?:jest|vitest|karma|playwright|cypress|mocha)\.config\.[cm]?[jt]s|karma\.conf\.js|\.mocharc(?:\.\w+)?|"
                         r"pom\.xml|build\.gradle(?:\.kts)?|codecov\.ya?ml|phpunit\.xml(?:\.dist)?|Makefile|\.rspec)$")
OPENAPI = re.compile(r"^(?:openapi|swagger)[\w.-]*\.(?:ya?ml|json)$", re.I)


class ChangeError(Exception):
    """A refusal the script reports as ``{"ok": false, "error": {code, message}}`` with exit 2."""

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail

    def to_json(self) -> Dict[str, Any]:
        return dict(self.detail, code=self.code, message=self.message)


def emit(body: Dict[str, Any]) -> int:
    print(json.dumps(body, sort_keys=True, ensure_ascii=False))
    return 0 if body.get("ok") else 2


def refuse(error: ChangeError) -> int:
    return emit({"ok": False, "error": error.to_json()})


# --------------------------------------------------------------------------- git, read-only

class Repo:
    """One repository read through the kernel's neutralised git session."""

    def __init__(self, root: str) -> None:
        self.root = Path(root)
        try:
            self.session = gitinfo.session(self.root)
        except gitinfo.Unavailable as exc:
            raise ChangeError("git_unavailable", "git does not run safely here: %s" % exc)
        if self.session is None:
            raise ChangeError("git_unavailable", "git is not installed")

    def _run(self, args: List[str]) -> Tuple[int, bytes]:
        try:
            done = self.session.run(args)
        except gitinfo.Unavailable as exc:
            raise ChangeError("git_unavailable", "git could not run: %s" % exc)
        return done.returncode, done.stdout

    def resolve(self, rev: str) -> str:
        """The full commit id ``rev`` names; an argument that looks like an option is never passed on."""
        if not isinstance(rev, str) or not rev or rev.startswith("-") or "\0" in rev:
            raise ChangeError("unknown_revision", "no such commit", revision=rev)
        code, out = self._run(["rev-parse", "--verify", "-q", "--end-of-options", rev + "^{commit}"])
        full = out.decode("ascii", "replace").strip()
        if code != 0 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", full):
            raise ChangeError("unknown_revision", "no such commit in this repository", revision=rev)
        return full

    def diff(self, base: str, head: str) -> str:
        code, out = self._run(["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "-U3", base, head, "--"])
        if code != 0:
            raise ChangeError("git_failed", "git diff failed", base=base, head=head)
        return out.decode("utf-8", "replace")

    def files(self, rev: str) -> List[str]:
        code, out = self._run(["ls-tree", "-r", "-z", "--name-only", rev])
        if code != 0:
            raise ChangeError("git_failed", "git ls-tree failed", revision=rev)
        return sorted(p.decode("utf-8", "replace") for p in out.split(b"\0") if p)

    def blobs(self, rev: str, paths: List[str], limit: int = 1 << 20) -> Dict[str, str]:
        """The text of each of ``paths`` at ``rev``, read in one ``cat-file --batch`` call; a path that is absent, larger than
        ``limit`` bytes, binary or not nameable on one line is left out."""
        wanted = [p for p in paths if "\n" not in p and "\r" not in p]
        if not wanted:
            return {}
        query = "".join("%s:%s\n" % (rev, p) for p in wanted).encode("utf-8")
        try:
            done = self.session.run(["cat-file", "--batch"], input=query, timeout_s=120)
        except gitinfo.Unavailable as exc:
            raise ChangeError("git_unavailable", "git could not run: %s" % exc)
        if done.returncode != 0:
            raise ChangeError("git_failed", "git cat-file failed", revision=rev)
        data, at, found = done.stdout, 0, {}
        for path in wanted:
            end = data.find(b"\n", at)
            if end < 0:
                break
            header = data[at:end].split()
            at = end + 1
            if len(header) != 3 or not header[2].isdigit():
                continue  # "<name> missing" or "ambiguous"
            size = int(header[2])
            body, at = data[at:at + size], at + size + 1
            if header[1] == b"blob" and size <= limit and b"\0" not in body:
                found[path] = body.decode("utf-8", "replace")
        return found


# --------------------------------------------------------------------------- the unified diff

def unquote(text: str) -> str:
    """A path git quoted C-style (``"a/\\303\\251.py"``), as text."""
    if not (len(text) > 1 and text.startswith('"') and text.endswith('"')):
        return text
    body, out, i = text[1:-1], bytearray(), 0
    simple = {"n": 10, "t": 9, "r": 13, "a": 7, "b": 8, "f": 12, "v": 11, "\\": 92, '"': 34}
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in simple:
                out.append(simple[nxt])
                i += 2
                continue
            if re.match(r"[0-7]{3}", body[i + 1:i + 4]):
                out.append(int(body[i + 1:i + 4], 8))
                i += 4
                continue
        out += ch.encode("utf-8")
        i += 1
    return out.decode("utf-8", "replace")


def _side_path(text: str) -> Optional[str]:
    text = text.rstrip("\t")
    if text == "/dev/null":
        return None
    path = unquote(text)
    return path[2:] if path[:2] in ("a/", "b/") else path


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_diff(text: str) -> List[Dict[str, Any]]:
    """The files of a unified diff: ``{path, old_path, status (A, D or M), binary, hunks}``; each hunk is ``{header, lines}`` and
    each line ``{kind (" ", "-" or "+"), old, new, text}`` with 1-based line numbers on its own sides."""
    files: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    hunk: Optional[Dict[str, Any]] = None
    left = right = 0
    old = new = 0

    def start(path: Optional[str] = None) -> Dict[str, Any]:
        entry = {"path": path, "old_path": path, "status": "M", "binary": False, "hunks": []}
        files.append(entry)
        return entry

    lines = text.split("\n")
    for line in lines:
        if hunk is not None and (left > 0 or right > 0):
            if line.startswith("\\"):
                continue
            kind = line[:1] if line[:1] in (" ", "-", "+") else " "
            body = line[1:]
            if kind == " ":
                hunk["lines"].append({"kind": " ", "old": old, "new": new, "text": body})
                old, new, left, right = old + 1, new + 1, left - 1, right - 1
            elif kind == "-":
                hunk["lines"].append({"kind": "-", "old": old, "new": None, "text": body})
                old, left = old + 1, left - 1
            else:
                hunk["lines"].append({"kind": "+", "old": None, "new": new, "text": body})
                new, right = new + 1, right - 1
            continue
        hunk = None
        if line.startswith("diff --git "):
            rest = line[len("diff --git "):]
            path = None
            if rest.startswith('"'):
                match = re.match(r'("(?:[^"\\]|\\.)*")', rest)
                path = _side_path(match.group(1)) if match else None
            else:
                size = (len(rest) - 5) // 2
                if size > 0 and rest.startswith("a/") and rest[2 + size:] == " b/" + rest[2:2 + size]:
                    path = rest[2:2 + size]
            current = start(path)
        elif line.startswith("new file mode") and current is not None:
            current["status"] = "A"
        elif line.startswith("deleted file mode") and current is not None:
            current["status"] = "D"
        elif line.startswith("Binary files ") and current is not None:
            current["binary"] = True
        elif line.startswith("--- "):
            if current is None or current["hunks"]:
                current = start()
            current["old_path"] = _side_path(line[4:])
            if current["old_path"] is None:
                current["status"] = "A"
        elif line.startswith("+++ ") and current is not None:
            path = _side_path(line[4:])
            if path is None:
                current["status"] = "D"
                current["path"] = current["path"] or current["old_path"]
            else:
                current["path"] = path
        elif line.startswith("@@") and current is not None:
            match = _HUNK.match(line)
            if match is None:
                continue
            old, left = int(match.group(1)), int(match.group(2)) if match.group(2) is not None else 1
            new, right = int(match.group(3)), int(match.group(4)) if match.group(4) is not None else 1
            hunk = {"header": line, "lines": []}
            current["hunks"].append(hunk)
    for entry in files:
        if entry["path"] is None:
            entry["path"] = entry["old_path"]
    return [entry for entry in files if entry["path"]]


# --------------------------------------------------------------------------- what a path is

def language_of(path: str) -> Optional[str]:
    return LANGUAGES.get(os.path.splitext(path)[1].lower())


def is_test_file(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if not parts:
        return False
    name = parts[-1]
    if name in TEST_SUPPORT:
        return True
    if language_of(path) is None:
        return False
    return bool(TEST_NAME.match(name)) or any(part in TEST_DIRS for part in parts[:-1]) or \
        any(parts[i:i + 2] == ("src", "test") for i in range(len(parts) - 1))


def is_ci_file(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(parts) and (parts[-1] in CI_FILES or any(parts[:len(d)] == d for d in CI_DIRS))


def kind_of(path: str, text: Optional[str] = None) -> str:
    """``test``, ``test-support``, ``ci``, ``test-config``, ``snapshot``, ``contract``, ``docs`` or ``product``."""
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else ""
    if name.endswith(".snap") or "__snapshots__" in parts:
        return "snapshot"
    if name in TEST_SUPPORT:
        return "test-support"
    if is_ci_file(path):
        return "ci"
    if TEST_CONFIG.match(name):
        return "test-config"
    if contract_of(path, text) is not None:
        return "contract"
    if is_test_file(path):
        return "test"
    if name.lower().endswith((".md", ".rst", ".txt", ".adoc")) or (parts and parts[0] in ("docs", "doc")):
        return "docs"
    return "product"


def contract_of(path: str, text: Optional[str] = None) -> Optional[Tuple[str, str]]:
    """``(kind, tool)`` for an API or schema contract file, else None."""
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else ""
    if OPENAPI.match(name) or (name.endswith((".yaml", ".yml", ".json")) and text is not None
                               and re.search(r'^\s*"?(?:openapi|swagger)"?\s*:', text, re.M)):
        return "openapi", "oasdiff"
    if name.endswith((".graphql", ".gql", ".graphqls")):
        return "graphql", "GraphQL Inspector"
    if name == "schema.prisma":
        return "prisma", "prisma migrate diff"
    if any(p in ("migrations", "migrate", "alembic", "flyway", "liquibase") for p in parts[:-1]) and name.endswith((".sql", ".py", ".xml", ".yaml", ".yml")):
        return "migration", "schema diff of the migrations"
    return None


def python_module(path: str) -> Optional[str]:
    """``a/b/c.py`` -> ``a.b.c``; ``a/b/__init__.py`` -> ``a.b``."""
    if not path.endswith(".py"):
        return None
    parts = list(PurePosixPath(path[:-3]).parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


def test_function_ranges(text: str) -> List[Tuple[str, int, int]]:
    """``(name, first line, last line)`` of every function whose name starts with ``test`` in Python source, methods included."""
    import ast
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            found.append((node.name, first, getattr(node, "end_lineno", node.lineno) or node.lineno))
    return sorted(found, key=lambda item: (item[1], item[0]))
