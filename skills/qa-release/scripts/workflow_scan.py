"""Lists the settings in a project's GitHub Actions workflows that weaken its pipeline, as SARIF results of category
``pipeline`` with CWE references.

Usage: python3 workflow_scan.py --root DIR [--out PATH]
       python3 workflow_scan.py --copy-to-work

It reads every ``*.yml`` and ``*.yaml`` file directly in ``DIR/.github/workflows/`` (GitHub runs no other) with its own reader
for the block-style YAML that workflows are written in. Rules:

* ``unpinned-action`` (CWE-829): a step's or a job's ``uses`` that names an action or a reusable workflow by a tag or a branch
  (anything but a full 40-character commit SHA), or a ``docker://`` image without an ``@sha256:`` digest. A local action
  (``./path``) is not listed.
* ``pr-target-head-checkout`` (CWE-829): a workflow triggered by ``pull_request_target`` or ``workflow_run`` that checks out
  the pull request's code: a ``checkout`` action whose ``ref`` or ``repository`` reads the pull request head
  (``github.event.pull_request.head``, ``github.head_ref``, ``github.event.workflow_run.head_``, ``refs/pull/``), or a ``run``
  line that fetches or checks out a pull request. The result is at that line; the trigger's line is its related location.
* ``expression-in-run`` (CWE-94): a ``${{ }}`` expression inside a ``run`` script (or an ``actions/github-script``
  ``script``) that reads data another person controls: ``github.event`` fields other than numbers, ids and commit hashes,
  ``github.head_ref``, ``inputs``. GitHub writes the value into the script before the shell reads it.
* ``tests-allowed-to-fail`` (no CWE): ``continue-on-error: true`` on a step whose ``run`` holds a test command, or on a job
  whose steps run one; or a test command followed on its line by ``|| true``, ``|| :`` or ``|| exit 0``.
* ``secret-echoed`` (CWE-532): a ``run`` line that prints a secret: ``echo`` or ``printf`` of a variable that an ``env``
  entry in scope sets from ``secrets``, or of a ``${{ secrets.* }}`` expression; ``printenv`` or a bare ``env`` while a secret
  is in the environment.
* ``excessive-permissions`` (CWE-250): ``permissions: write-all``; a ``write`` scope at the workflow level, which every job
  gets; a ``write`` scope in a ``pull_request_target`` or ``workflow_run`` workflow; no ``permissions`` at the workflow level
  and none on a job, so the job gets the repository's default token permissions (approximate: the default depends on a
  repository setting this script cannot read).

Every result is a lead read from the files: what GitHub resolves at run time (a default branch, an organization's settings)
is decided elsewhere. ``properties.approximate`` is true where a result also rests on such a setting.

The reader. It reads block mappings, block sequences, plain and quoted single-line values, literal and folded block values,
comments, a leading ``---`` and flat single-line flow collections (``[a, b]``, ``{}``, ``{a: b}``). A line it cannot read
exactly is listed ``unreadable`` with its line number and reason, and nothing under it is read, never guessed: an anchor, an
alias, a tag, a merge key, a complex key, a duplicate key, a tab in the indentation, a quoted or plain value continued on
another line, ``: `` inside a plain value, a nested or unclosed flow collection, an explicit block indentation indicator, a
line indented where no value can start, a second document, a line over 4096 characters (checked before any pattern reads
it). A file over 1 MiB, a link, a file that is not regular or not UTF-8 is listed ``unreadable`` with its reason and no line;
a leading UTF-8 byte-order mark is dropped. The rules read only what the reader read, and each rule reads a line in one
pass, so the time a scan takes grows with the size of the files, never faster.

Paths are relative to the current directory when the file lies under it (run it from the project root), absolute otherwise.
``--out PATH`` writes SARIF 2.1.0 to a new file under ``.qa/work/release/`` or, inside a container run, ``/qa-evidence/``;
without it nothing is written. ``--copy-to-work`` writes a copy of this script to ``.qa/work/release/workflow_scan.py`` for a
container run's ``--input``. Files are written by exclusive create through no link. Prints one JSON summary. Exit 0 when the
scan ran (results or not), 2 on bad arguments or a refused path. Standard library only; nothing it reads is run or followed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Sequence, Tuple

VERSION = "0.1.0"
DRIVER = "qa-release-workflow-scan"
CATEGORY = "pipeline"
WORK_DIR = ".qa/work/release"
EVIDENCE_DIR = "/qa-evidence"
MAX_BYTES = 1024 * 1024
MAX_LINE = 4096
MAX_DEPTH = 64
LONG_LINE = "a line over %d characters; nothing on it or under it is read" % MAX_LINE
LONG_BLOCK_LINE = "a line over %d characters in a block value; the value is not read" % MAX_LINE
RULES: Dict[str, Dict[str, Any]] = {
    "unpinned-action": {"cwe": ["CWE-829"], "text": "An action or reusable workflow is referenced by a tag or branch, not a commit SHA"},
    "pr-target-head-checkout": {"cwe": ["CWE-829"],
                                "text": "A pull_request_target or workflow_run workflow checks out the pull request's code"},
    "expression-in-run": {"cwe": ["CWE-94"], "text": "An expression another person controls is written into a run script"},
    "tests-allowed-to-fail": {"cwe": [], "text": "A failing test run does not fail the job"},
    "secret-echoed": {"cwe": ["CWE-532"], "text": "A secret is written to the job log"},
    "excessive-permissions": {"cwe": ["CWE-250"], "text": "The job token gets more permissions than the jobs need"},
}
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
HEAD_REF = re.compile(r"github\.event\.pull_request\.head\.|github\.head_ref|github\.event\.workflow_run\.head_|refs/pull/")
PR_CHECKOUT = re.compile(r"\bgh\s+pr\s+checkout\b|refs/pull/")
FETCH_WORD = re.compile(r"\bfetch\b")
PULL_PATH = re.compile(r"\bpull/")
UNSAFE_CONTEXT = re.compile(r"\bgithub\.event\.[\w.\-\[\]'\"*]+|\bgithub\.head_ref\b|\binputs\.[\w\-]+")
SAFE_FIELD = re.compile(r"(?:^|[._])(?:number|id|sha|ids|count|created_at|updated_at|run_attempt|run_number)$")
# The test command, shared word for word with release_check.py (a test asserts it): a runner's word, or a build tool with a test
# goal later in the same command. test_command_end reads a line in one pass.
TEST_WORD = re.compile(
    r"\b(?:py\.test|pytest|unittest|nose2|tox|nox|jest|vitest|mocha|karma|ava|rspec|phpunit|ctest|"
    r"playwright\s+test|cypress\s+run|go\s+test|cargo\s+test|dotnet\s+test|swift\s+test|mix\s+test|rake\s+(?:test|spec)|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test|make\s+(?:test|check))\b")
BUILD_TOOL = re.compile(r"\b(?:(mvnw?)|(gradlew?))\b")
BUILD_GOAL = {"mvn": re.compile(r"\b(?:test|verify)\b"), "gradle": re.compile(r"\b(?:test|check)\b")}
COMMAND_END = re.compile(r"[|;&\n]")
MASKED = re.compile(r"\|\|\s*(?:true\b|:(?=\s|$|;)|exit\s+0\b)")
PRINTS = re.compile(r"(?:^|[;&|(`]\s*|\$\(\s*)(?:echo|printf)\b")
PRINT_STOP = re.compile(r"[;&|](?![|&])")
VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
DUMPS = re.compile(r"(?:^|[;&|(]\s*)(?:printenv|env)\s*(?:$|[;&|)>])")
SECRETS_WORD = re.compile(r"\bsecrets\.")
PRIVILEGED_TRIGGERS = ("pull_request_target", "workflow_run")


class Refused(Exception):
    pass


# --------------------------------------------------------------------------- writing without following links

def _no_link_on_the_way(root: Path, parts: Sequence[str]) -> None:
    here = root
    for part in parts:
        here = here / part
        try:
            mode = os.lstat(str(here)).st_mode
        except FileNotFoundError:
            os.mkdir(str(here), 0o755)
            mode = os.lstat(str(here)).st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise Refused("%s is a link or not a directory" % "/".join(parts))


def write_new(text: str, data: bytes, root: Optional[Path] = None) -> str:
    """Write ``data`` to ``text``: a new file directly in ``/qa-evidence/`` (a container run), or under ``.qa/work/release/``
    through no link (a fresh temporary file by exclusive create, then a rename onto the name)."""
    root = (root or Path.cwd()).resolve()
    if os.path.isabs(text):
        pure = PurePosixPath(text)
        if str(pure.parent) != EVIDENCE_DIR or pure.name in ("", ".", ".."):
            raise Refused("an absolute --out must name a file directly in %s/" % EVIDENCE_DIR)
        mode = os.lstat(EVIDENCE_DIR).st_mode if os.path.lexists(EVIDENCE_DIR) else None
        if mode is None or stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise Refused("%s is missing, a link or not a directory" % EVIDENCE_DIR)
        fd = os.open(text, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return text
    parts = PurePosixPath(text).parts
    if parts[:3] != (".qa", "work", "release") or len(parts) < 4 or any(p in ("..", ".") for p in parts):
        raise Refused("--out must name a file under %s/ or %s/" % (WORK_DIR, EVIDENCE_DIR))
    _no_link_on_the_way(root, parts[:-1])
    folder = root.joinpath(*parts[:-1])
    temporary = folder / (".%s.%s.tmp" % (parts[-1], secrets.token_hex(8)))
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        target = folder / parts[-1]
        if os.path.islink(str(target)):
            raise Refused("%s is a link" % text)
        os.replace(str(temporary), str(target))
    except BaseException:
        if os.path.lexists(str(temporary)):
            os.unlink(str(temporary))
        raise
    return "/".join(parts)


# --------------------------------------------------------------------------- shared with release_check.py, word for word

def utf8_text(data: bytes) -> Optional[str]:
    """``data`` as UTF-8 text without a leading byte-order mark, or None when it is not UTF-8."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text[1:] if text.startswith("\ufeff") else text


def test_command_end(text: str) -> Optional[int]:
    """Where the first test command on a line ends, or None. A build tool's command runs to the next ``|``, ``;``, ``&`` or line
    end and ends at its last test goal; once one of a kind finds no test goal there, the rest of that command is not searched
    again for that kind."""
    word = TEST_WORD.search(text)
    best = (word.start(), word.end()) if word else None
    searched = {"mvn": -1, "gradle": -1}
    for tool in BUILD_TOOL.finditer(text):
        if best is not None and tool.start() >= best[0]:
            break
        kind = "mvn" if tool.group(1) else "gradle"
        if tool.start() < searched[kind]:
            continue
        stop = COMMAND_END.search(text, tool.end())
        end = stop.start() if stop else len(text)
        last = None
        for last in BUILD_GOAL[kind].finditer(text, tool.end(), end):
            pass
        if last is not None:
            best = (tool.start(), last.end())
            break
        searched[kind] = end
    return best[1] if best else None


# --------------------------------------------------------------------------- the YAML subset

class Node:
    def __init__(self, line: int) -> None:
        self.line = line


class Scalar(Node):
    """A value. ``lines`` holds ``(line, text)`` of each line of a block value, or the one line of a single-line value."""

    def __init__(self, line: int, value: Optional[str], style: str, lines: Optional[List[Tuple[int, str]]] = None) -> None:
        super().__init__(line)
        self.value = value
        self.style = style
        self.lines = lines if lines is not None else ([(line, value)] if value is not None else [])


class Mapping(Node):
    def __init__(self, line: int) -> None:
        super().__init__(line)
        self.items: List[Tuple[str, int, Node]] = []

    def get(self, key: str) -> Optional[Node]:
        for name, _line, value in self.items:
            if name == key:
                return value
        return None

    def key_line(self, key: str) -> Optional[int]:
        for name, line, _value in self.items:
            if name == key:
                return line
        return None


class Sequence_(Node):
    def __init__(self, line: int) -> None:
        super().__init__(line)
        self.items: List[Node] = []


class Unknown(Node):
    """What the reader could not read: no rule looks inside it."""


class Reader:
    def __init__(self, text: str) -> None:
        self.lines = [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]
        self.i = 0
        self.unreadable: List[Tuple[int, str]] = []
        self.depth = 0

    # ---- lines
    @staticmethod
    def blank(raw: str) -> bool:
        stripped = raw.strip(" ")
        return stripped == "" or stripped.startswith("#")

    @staticmethod
    def indent(raw: str) -> int:
        return len(raw) - len(raw.lstrip(" "))

    def skip_blank(self) -> None:
        while self.i < len(self.lines) and self.blank(self.lines[self.i]):
            self.i += 1

    def refuse(self, reason: str, below: int) -> Unknown:
        """List the current line unreadable and skip it with every following line indented deeper than ``below``."""
        line = self.i + 1
        self.unreadable.append((line, reason))
        self.i += 1
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            if not self.blank(raw) and self.indent(raw) <= below:
                break
            self.i += 1
        return Unknown(line)

    def tab_check(self) -> Optional[str]:
        raw = self.lines[self.i]
        lead = raw[:len(raw) - len(raw.lstrip(" \t"))]
        return "a tab in the indentation" if "\t" in lead else None

    # ---- the document
    def document(self) -> Node:
        self.skip_blank()
        if self.i < len(self.lines) and self.lines[self.i].rstrip() == "---":
            self.i += 1
            self.skip_blank()
        if self.i >= len(self.lines):
            return Mapping(1)
        root = self.collection(self.indent(self.lines[self.i]), -1)
        while True:
            self.skip_blank()
            if self.i >= len(self.lines):
                break
            raw = self.lines[self.i]
            if len(raw) > MAX_LINE:
                self.refuse(LONG_LINE, -1)
                continue
            if raw.rstrip() in ("---", "...") or raw.startswith("%"):
                self.unreadable.append((self.i + 1, "a document marker or directive; the rest of the file is not read"))
                break
            self.refuse("a line outside the document's top-level mapping", -1)
        return root

    def collection(self, indent: int, parent: int) -> Node:
        """The block node whose first line is the current one, at ``indent``."""
        reason = self.tab_check()
        if reason:
            return self.refuse(reason, parent)
        self.depth += 1
        try:
            if self.depth > MAX_DEPTH:
                return self.refuse("nested deeper than %d levels" % MAX_DEPTH, parent)
            content = self.lines[self.i][indent:]
            if content == "-" or content.startswith("- "):
                return self.sequence(indent)
            return self.mapping(indent)
        finally:
            self.depth -= 1

    def mapping(self, indent: int) -> Node:
        node = Mapping(self.i + 1)
        seen = set()
        while True:
            self.skip_blank()
            if self.i >= len(self.lines):
                break
            raw = self.lines[self.i]
            here = self.indent(raw)
            if here < indent:
                break
            if len(raw) > MAX_LINE:
                self.refuse(LONG_LINE, indent)
                continue
            reason = self.tab_check()
            if reason:
                self.refuse(reason, indent)
                continue
            if here > indent:
                self.refuse("a line indented where no value can start", indent)
                continue
            content = raw[indent:]
            if content.rstrip() in ("---", "..."):
                break
            if content == "-" or content.startswith("- "):
                self.refuse("a sequence entry where a key was expected", indent)
                continue
            split = split_key(content)
            if isinstance(split, str):
                self.refuse(split, indent)
                continue
            key, rest = split
            if key in seen:
                self.refuse("a duplicate key %r" % key, indent)
                continue
            seen.add(key)
            line = self.i + 1
            node.items.append((key, line, self.value(rest, indent, line)))
        return node

    def sequence(self, indent: int) -> Node:
        node = Sequence_(self.i + 1)
        while True:
            self.skip_blank()
            if self.i >= len(self.lines):
                break
            raw = self.lines[self.i]
            here = self.indent(raw)
            if here < indent:
                break
            if len(raw) > MAX_LINE:
                self.refuse(LONG_LINE, indent)
                continue
            reason = self.tab_check()
            if reason:
                self.refuse(reason, indent)
                continue
            if here > indent:
                self.refuse("a line indented where no value can start", indent)
                continue
            content = raw[indent:]
            if not (content == "-" or content.startswith("- ")):
                break
            rest = content[1:]
            item = rest.lstrip(" ")
            line = self.i + 1
            if item == "" or item.startswith("#"):
                node.items.append(self.value("", indent, line))
                continue
            if item.startswith("- ") or item == "-":
                node.items.append(self.refuse("a sequence nested on one line", indent))
                continue
            inner = indent + 1 + (len(rest) - len(item))
            if item[0] not in "'\"[{|>&*!" and not isinstance(split_key(item), str):
                self.lines[self.i] = " " * inner + item          # the entry is a mapping that starts on the dash's line
                node.items.append(self.collection(inner, indent))
                continue
            node.items.append(self.value(" " + item, indent, line))
        return node

    def value(self, rest: str, indent: int, line: int) -> Node:
        """The value after ``key:`` or ``-`` on line ``line``, whose parent sits at ``indent``. Leaves the reader after it."""
        text = rest.strip(" ")
        if text == "" or text.startswith("#"):
            self.i += 1
            self.skip_blank()
            if self.i < len(self.lines):
                raw = self.lines[self.i]
                here = self.indent(raw)
                content = raw[here:]
                if here > indent:
                    return self.collection(here, indent)
                if here == indent and (content == "-" or content.startswith("- ")) and self.in_mapping_value(line, indent):
                    return self.sequence(indent)
            return Scalar(line, None, "null")
        head = text[0]
        if head in "&*!":
            return self.refuse("an anchor, an alias or a tag", indent)
        if head in "@`%,":
            return self.refuse("a value that starts with a reserved character", indent)
        if head in "|>":
            return self.block(text, indent, line)
        if head in "'\"":
            parsed = quoted(text)
            if parsed is None:
                return self.refuse("a quoted value continued on another line", indent)
            value, after = parsed
            if after.strip() and not re.match(r"\s+#", after):
                return self.refuse("text after a quoted value", indent)
            return self.single(Scalar(line, value, "quoted"), indent)
        if head in "[{":
            parsed_flow = flow(strip_comment(text))
            if parsed_flow is None:
                return self.refuse("a nested, multi-line or unclosed flow collection", indent)
            return self.single(build_flow(parsed_flow, line), indent)
        value = strip_comment(text)
        if re.search(r":(?:\s|$)", value):
            return self.refuse("': ' inside a plain value", indent)
        return self.single(Scalar(line, value, "plain"), indent)

    def in_mapping_value(self, line: int, indent: int) -> bool:
        """Whether line ``line`` (1-based) is a ``key:`` line at ``indent``: only a key's value may be a sequence at its own indent."""
        raw = self.lines[line - 1]
        content = raw[indent:] if self.indent(raw) == indent else ""
        return bool(content) and not (content == "-" or content.startswith("- "))

    def single(self, node: Node, indent: int) -> Node:
        """A one-line value: the next line must not continue it."""
        self.i += 1
        probe = self.i
        while probe < len(self.lines) and self.blank(self.lines[probe]):
            probe += 1
        if probe < len(self.lines) and self.indent(self.lines[probe]) > indent:
            self.i = node.line - 1
            return self.refuse("a value continued on the next line", indent)
        return node

    def block(self, header: str, indent: int, line: int) -> Node:
        match = re.match(r"([|>])([+-]?)(\s+#.*)?\s*$", header)
        if not match:
            return self.refuse("a block value header with an indentation indicator or text after it", indent)
        style = "literal" if match.group(1) == "|" else "folded"
        self.i += 1
        body: List[Tuple[int, str]] = []
        content_indent: Optional[int] = None
        too_long: Optional[int] = None
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            if raw.strip(" ") == "":
                body.append((self.i + 1, ""))
                self.i += 1
                continue
            here = self.indent(raw)
            if content_indent is None:
                if here <= indent:
                    break
                content_indent = here
            if here < content_indent:
                break
            if len(raw) > MAX_LINE and too_long is None:
                too_long = self.i + 1
            body.append((self.i + 1, raw[content_indent:]))
            self.i += 1
        if too_long is not None:
            self.unreadable.append((too_long, LONG_BLOCK_LINE))
            return Unknown(line)
        while body and body[-1][1] == "":
            body.pop()
        joined = "\n".join(t for _, t in body) if style == "literal" else " ".join(t for _, t in body if t)
        return Scalar(line, joined, style, body)


def strip_comment(text: str) -> str:
    match = re.search(r"\s#", text)
    return (text[:match.start()] if match else text).rstrip(" ")


def quoted(text: str) -> Optional[Tuple[str, str]]:
    """``(value, the text after it)`` of a quoted value that closes on this line, else None."""
    mark = text[0]
    out = []
    i = 1
    while i < len(text):
        c = text[i]
        if mark == "'" and c == "'":
            if text[i + 1:i + 2] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), text[i + 1:]
        if mark == '"' and c == "\\":
            out.append(text[i:i + 2])
            i += 2
            continue
        if mark == '"' and c == '"':
            return "".join(out), text[i + 1:]
        out.append(c)
        i += 1
    return None


def split_key(content: str) -> Any:
    """``(key, the text after its colon)``, or the reason the line holds no key the reader can read."""
    if content.startswith("? ") or content == "?":
        return "a complex key"
    if content.startswith("<<"):
        return "a merge key"
    if content[:1] in "&*!":
        return "an anchor, an alias or a tag"
    if content[:1] in "'\"":
        parsed = quoted(content)
        if parsed is None:
            return "a quoted key continued on another line"
        key, after = parsed
        if not (after.startswith(":") and (after[1:2] in ("", " "))):
            return "a quoted value where a key was expected"
        return key, after[1:]
    if content[:1] in "[{|>%@`#,":
        return "a line that holds no key"
    match = re.search(r":(?: |$)", content)
    if not match:
        return "a line that holds no key"
    key = content[:match.start()].rstrip(" ")
    if not key or " #" in key:
        return "a line that holds no key"
    return key, content[match.end():]


def flow(text: str) -> Optional[Any]:
    """A flat, single-line flow collection: a list of scalars or a dict of scalar values; None for anything else."""
    if len(text) < 2 or text[0] + text[-1] not in ("[]", "{}"):
        return None
    inner = text[1:-1].strip()
    if any(c in inner for c in "[]{}&*!|>"):
        return None
    parts = [p.strip() for p in inner.split(",")] if inner else []
    if text[0] == "[":
        values = []
        for part in parts:
            if not part or ":" in part and re.search(r":(?:\s|$)", part):
                return None
            values.append(scalar_text(part))
            if values[-1] is None:
                return None
        return values
    mapping: Dict[str, str] = {}
    for part in parts:
        match = re.match(r"([^:'\"]+):\s+(.+)$", part) or re.match(r"([^:'\"]+):$", part)
        if not match:
            return None
        key = match.group(1).strip()
        value = scalar_text(match.group(2)) if match.lastindex and match.lastindex >= 2 else ""
        if value is None or key in mapping:
            return None
        mapping[key] = value
    return mapping


def scalar_text(part: str) -> Optional[str]:
    if part[:1] in "'\"":
        parsed = quoted(part)
        return parsed[0] if parsed and not parsed[1].strip() else None
    return part


def build_flow(value: Any, line: int) -> Node:
    if isinstance(value, list):
        node: Node = Sequence_(line)
        node.items.extend(Scalar(line, v, "plain") for v in value)   # type: ignore[attr-defined]
        return node
    mapping = Mapping(line)
    mapping.items.extend((k, line, Scalar(line, v, "plain")) for k, v in value.items())
    return mapping


# --------------------------------------------------------------------------- files

def uri_of(path: Path) -> str:
    absolute = Path(os.path.abspath(str(path)))
    try:
        return absolute.relative_to(Path(os.path.abspath(os.getcwd()))).as_posix()
    except ValueError:
        return absolute.as_posix()


def workflow_files(root: Path, unreadable: List[Dict[str, Any]]) -> List[Path]:
    folder = root / ".github" / "workflows"
    for part in (root / ".github", folder):
        if os.path.islink(str(part)):
            unreadable.append({"path": uri_of(part), "line": None, "reason": "a link, not followed"})
            return []
    if not folder.is_dir():
        return []
    found = []
    real_root = os.path.realpath(str(root))
    for name in sorted(os.listdir(str(folder))):
        if not (name.endswith(".yml") or name.endswith(".yaml")):
            continue
        path = folder / name
        info = os.lstat(str(path))
        if stat.S_ISLNK(info.st_mode):
            unreadable.append({"path": uri_of(path), "line": None, "reason": "a link, not followed"})
        elif not stat.S_ISREG(info.st_mode):
            unreadable.append({"path": uri_of(path), "line": None, "reason": "not a regular file"})
        elif info.st_size > MAX_BYTES:
            unreadable.append({"path": uri_of(path), "line": None, "reason": "over 1 MiB"})
        elif os.path.commonpath([real_root, os.path.realpath(str(path))]) != real_root:
            unreadable.append({"path": uri_of(path), "line": None, "reason": "resolves outside the root"})
        else:
            found.append(path)
    return found


def read_text(path: Path) -> Optional[str]:
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        return None
    return utf8_text(data)


# --------------------------------------------------------------------------- the rules

def result(rule: str, uri: str, line: int, message: str, approximate: bool = False, related: Optional[List[int]] = None) -> Dict[str, Any]:
    return {"rule": rule, "path": uri, "line": line, "message": message, "cwe": RULES[rule]["cwe"], "approximate": approximate,
            "related": related or []}


def scalar(node: Optional[Node]) -> Optional[Scalar]:
    return node if isinstance(node, Scalar) and node.value is not None else None


def mapping(node: Optional[Node]) -> Optional[Mapping]:
    return node if isinstance(node, Mapping) else None


def entries(node: Optional[Node]) -> List[Node]:
    return node.items if isinstance(node, Sequence_) else []


def triggers(doc: Mapping) -> Dict[str, int]:
    on = doc.get("on")
    line = doc.key_line("on") or 1
    if isinstance(on, Scalar) and on.value:
        return {on.value: on.line}
    if isinstance(on, Sequence_):
        return {item.value: item.line for item in on.items if isinstance(item, Scalar) and item.value}
    if isinstance(on, Mapping):
        return {name: key_line for name, key_line, _ in on.items}
    return {} if on is None else {"?": line}


def expressions(content: str) -> List[str]:
    """The text inside each ``${{ ... }}`` of a line, in order; each one closes at the first ``}}`` after its opener."""
    found = []
    position = 0
    while True:
        start = content.find("${{", position)
        if start < 0:
            return found
        end = content.find("}}", start + 3)
        if end < 0:
            return found
        found.append(content[start + 3:end])
        position = end + 2


def secret_expression(text: str) -> bool:
    """Whether a ``${{`` opener is followed by ``secrets.`` before the next ``}``. The first opener of a stretch with no ``}``
    is searched to that stretch's end, which covers every later opener in it."""
    position = 0
    while True:
        start = text.find("${{", position)
        if start < 0:
            return False
        close = text.find("}", start + 3)
        end = len(text) if close < 0 else close
        if SECRETS_WORD.search(text, start + 3, end):
            return True
        if close < 0:
            return False
        position = close + 1


def fetches_pr(content: str) -> bool:
    """``gh pr checkout``, a ``refs/pull/`` reference, or a ``fetch`` with ``pull/`` after it on the line."""
    if PR_CHECKOUT.search(content):
        return True
    fetch = FETCH_WORD.search(content)
    return bool(fetch and PULL_PATH.search(content, fetch.end()))


def secret_env(*scopes: Optional[Node]) -> Dict[str, int]:
    """Variables set from ``secrets`` in the given ``env`` mappings, outermost first (an inner scope overrides)."""
    names: Dict[str, int] = {}
    for scope in scopes:
        env = mapping(scope)
        if env is None:
            continue
        for name, line, value in env.items:
            text = scalar(value)
            if text is not None and secret_expression(text.value or ""):
                names[name] = line
            else:
                names.pop(name, None)
    return names


def is_true(node: Optional[Node]) -> Optional[int]:
    text = scalar(node)
    return text.line if text is not None and text.style == "plain" and text.value.lower() == "true" else None


def expression_problems(text: Scalar) -> List[Tuple[int, str]]:
    found = []
    for line, content in text.lines:
        for inside in expressions(content):
            for read in UNSAFE_CONTEXT.findall(inside):
                if read.startswith("github.event.") and SAFE_FIELD.search(read.rstrip(".")):
                    continue
                found.append((line, read))
                break
    return found


def step_rules(uri: str, step: Mapping, scopes: Tuple[Optional[Node], ...], privileged: Dict[str, int],
               results: List[Dict[str, Any]]) -> bool:
    """The rules that read one step (``scopes``: the workflow's and the job's ``env``); whether it runs a test command."""
    uses = scalar(step.get("uses"))
    if uses is not None:
        unpinned(uri, uses, results)
    run = scalar(step.get("run"))
    with_ = mapping(step.get("with"))
    if uses is not None and privileged and re.match(r"actions/checkout@", uses.value or "") and with_ is not None:
        for name in ("ref", "repository"):
            value = scalar(with_.get(name))
            if value is not None and HEAD_REF.search(value.value or ""):
                trigger, line = sorted(privileged.items(), key=lambda item: item[1])[0]
                results.append(result("pr-target-head-checkout", uri, value.line,
                                      "a %s workflow (trigger at line %d) checks out the pull request's code: %s reads %s"
                                      % (trigger, line, name, value.value), related=[line]))
                break
    scripts = [run] if run is not None else []
    if uses is not None and re.match(r"actions/github-script@", uses.value or "") and with_ is not None and scalar(with_.get("script")):
        scripts.append(scalar(with_.get("script")))   # type: ignore[arg-type]
    for script in scripts:
        for line, read in expression_problems(script):
            results.append(result("expression-in-run", uri, line, "the expression ${{ %s }} is written into the script before the "
                                                                  "shell reads it" % read))
    tests = False
    if run is not None:
        env = secret_env(*(scopes + (step.get("env"),)))
        reported_fetch = False
        for line, content in run.lines:
            end = test_command_end(content)
            if end is not None:
                tests = True
                if MASKED.search(content, end):
                    results.append(result("tests-allowed-to-fail", uri, line, "the test command's failure is masked on its line"))
            if privileged and not reported_fetch and fetches_pr(content):
                trigger, trigger_line = sorted(privileged.items(), key=lambda item: item[1])[0]
                results.append(result("pr-target-head-checkout", uri, line, "a %s workflow (trigger at line %d) fetches or checks "
                                                                            "out a pull request in a run script" % (trigger, trigger_line),
                                      related=[trigger_line]))
                reported_fetch = True
            echoed = secret_printed(content, env)
            if echoed:
                results.append(result("secret-echoed", uri, line, echoed))
        flag = is_true(step.get("continue-on-error"))
        if tests and flag is not None:
            results.append(result("tests-allowed-to-fail", uri, flag, "continue-on-error is true on a step that runs the tests"))
    return tests


def secret_printed(content: str, env: Dict[str, int]) -> Optional[str]:
    """What a print command on the line writes to the log from a secret, or None. A print command runs from ``echo`` or
    ``printf`` to the next command separator; a later print word inside that command is part of it and is not read again."""
    covered = -1
    for match in PRINTS.finditer(content):
        if match.start() < covered:
            continue
        stop = PRINT_STOP.search(content, match.end())
        covered = stop.start() if stop else len(content)
        command = content[match.start():covered]
        if secret_expression(command):
            return "a secrets expression is printed to the log"
        named = {braced or bare for braced, bare in VARIABLE.findall(command)}
        for name in env:
            if name in named:
                return "the variable %s, set from a secret, is printed to the log" % name
    if env and DUMPS.search(content):
        return "the whole environment, which holds %s set from a secret, is printed to the log" % ", ".join(sorted(env))
    return None


def unpinned(uri: str, uses: Scalar, results: List[Dict[str, Any]]) -> None:
    ref = uses.value or ""
    if ref.startswith("./") or ref.startswith("../"):
        return
    if ref.startswith("docker://"):
        if not DIGEST.search(ref):
            results.append(result("unpinned-action", uri, uses.line, "the container image %s is not pinned by an @sha256 digest" % ref[9:]))
        return
    name, _, version = ref.rpartition("@")
    if not name:
        results.append(result("unpinned-action", uri, uses.line, "%s names no version at all" % ref))
    elif not SHA.fullmatch(version):
        results.append(result("unpinned-action", uri, uses.line, "%s is referenced by %s, a tag or branch that can be moved, not by a "
                                                                 "full commit SHA" % (name, version)))


def permission_rules(uri: str, doc: Mapping, jobs: Optional[Mapping], privileged: Dict[str, int], results: List[Dict[str, Any]]) -> None:
    def writes(node: Optional[Node]) -> List[Tuple[str, int]]:
        perms = mapping(node)
        if perms is None:
            return []
        return [(name, value.line) for name, _l, value in perms.items if scalar(value) and scalar(value).value == "write"]  # type: ignore[union-attr]

    top = doc.get("permissions")
    if scalar(top) is not None and scalar(top).value in ("write-all",):   # type: ignore[union-attr]
        results.append(result("excessive-permissions", uri, top.line, "permissions: write-all gives every job every write scope"))  # type: ignore[union-attr]
    for name, line in writes(top):
        why = "in a %s workflow" % "/".join(sorted(privileged)) if privileged else "at the workflow level, so every job gets it"
        results.append(result("excessive-permissions", uri, line, "the %s write scope is granted %s" % (name, why)))
    if jobs is None:
        return
    for job_name, job_line, job in jobs.items:
        job_map = mapping(job)
        if job_map is None:
            continue
        own = job_map.get("permissions")
        if scalar(own) is not None and scalar(own).value == "write-all":   # type: ignore[union-attr]
            results.append(result("excessive-permissions", uri, own.line, "permissions: write-all gives job %s every write scope" % job_name))  # type: ignore[union-attr]
        if privileged:
            for name, line in writes(own):
                results.append(result("excessive-permissions", uri, line, "the %s write scope is granted in a %s workflow"
                                      % (name, "/".join(sorted(privileged)))))
        if top is None and own is None and job_map.get("uses") is None:
            results.append(result("excessive-permissions", uri, job_line, "job %s declares no permissions and the workflow none, so "
                                                                         "it gets the repository's default token permissions" % job_name,
                                  approximate=True))


def scan_workflow(uri: str, doc: Node, results: List[Dict[str, Any]]) -> None:
    if not isinstance(doc, Mapping):
        return
    privileged = {name: line for name, line in triggers(doc).items() if name in PRIVILEGED_TRIGGERS}
    jobs = mapping(doc.get("jobs"))
    permission_rules(uri, doc, jobs, privileged, results)
    for _name, _line, job in (jobs.items if jobs is not None else []):
        job_map = mapping(job)
        if job_map is None:
            continue
        uses = scalar(job_map.get("uses"))
        if uses is not None:
            unpinned(uri, uses, results)
        runs_tests = False
        for step in entries(job_map.get("steps")):
            step_map = mapping(step)
            if step_map is not None:
                runs_tests = step_rules(uri, step_map, (doc.get("env"), job_map.get("env")), privileged, results) or runs_tests
        flag = is_true(job_map.get("continue-on-error"))
        if runs_tests and flag is not None:
            results.append(result("tests-allowed-to-fail", uri, flag, "continue-on-error is true on a job that runs the tests"))


def scan(root: Path) -> Dict[str, Any]:
    unreadable: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []
    read = 0
    for path in workflow_files(root, unreadable):
        uri = uri_of(path)
        try:
            text = read_text(path)
        except OSError as exc:
            unreadable.append({"path": uri, "line": None, "reason": "not opened: %s" % (exc.strerror or type(exc).__name__)})
            continue
        if text is None:
            unreadable.append({"path": uri, "line": None, "reason": "not a regular UTF-8 file of at most 1 MiB"})
            continue
        reader = Reader(text)
        try:
            doc = reader.document()
        except RecursionError:
            unreadable.append({"path": uri, "line": None, "reason": "nested too deeply"})
            continue
        unreadable += [{"path": uri, "line": line, "reason": reason} for line, reason in reader.unreadable]
        read += 1
        scan_workflow(uri, doc, results)
    unique = {(r["rule"], r["path"], r["line"]): r for r in results}
    ordered = sorted(unique.values(), key=lambda r: (r["path"], r["line"], r["rule"]))
    return {"results": ordered, "unreadable": unreadable, "files": read}


# --------------------------------------------------------------------------- output

def sarif(found: Dict[str, Any]) -> Dict[str, Any]:
    rules = [{"id": rule, "shortDescription": {"text": info["text"]},
              "properties": {"category": CATEGORY, "cwe": info["cwe"], "tags": [CATEGORY] + info["cwe"]}} for rule, info in RULES.items()]
    results = []
    for r in found["results"]:
        item = {"ruleId": r["rule"], "level": "warning", "message": {"text": r["message"]},
                "locations": [{"physicalLocation": {"artifactLocation": {"uri": r["path"]}, "region": {"startLine": r["line"]}}}],
                "properties": {"category": CATEGORY, "cwe": r["cwe"], "approximate": r["approximate"]}}
        if r["related"]:
            item["relatedLocations"] = [{"id": n, "physicalLocation": {"artifactLocation": {"uri": r["path"]}, "region": {"startLine": line}}}
                                        for n, line in enumerate(r["related"])]
        results.append(item)
    notes = [{"level": "warning", "message": {"text": "unreadable: %s%s: %s" % (u["path"], ":%d" % u["line"] if u["line"] else "", u["reason"])}}
             for u in found["unreadable"]]
    return {"version": "2.1.0", "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [{"tool": {"driver": {"name": DRIVER, "version": VERSION, "rules": rules}},
                      "invocations": [{"executionSuccessful": True, "toolExecutionNotifications": notes}],
                      "results": results,
                      "properties": {"category": CATEGORY, "unreadable": found["unreadable"], "files": found["files"]}}]}


def listed(found: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"rule": r["rule"], "at": "%s:%d" % (r["path"], r["line"]), "cwe": r["cwe"], "approximate": r["approximate"],
             "related": ["%s:%d" % (r["path"], line) for line in r["related"]], "message": r["message"]} for r in found["results"]]


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root")
    parser.add_argument("--out")
    parser.add_argument("--copy-to-work", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.copy_to_work:
            if args.root or args.out:
                raise Refused("--copy-to-work takes no other option")
            data = Path(__file__).read_bytes()
            written = write_new(WORK_DIR + "/workflow_scan.py", data)
            print(json.dumps({"ok": True, "written": written, "sha256": sha256(data),
                              "input": "/work/.qa-input/release/workflow_scan.py"}, sort_keys=True))
            return 0
        if not args.root:
            raise Refused("--root DIR is required")
        root = Path(args.root)
        mode = os.lstat(str(root)).st_mode if os.path.lexists(str(root)) else None
        if mode is None or stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise Refused("--root must be a directory that is not a link")
        found = scan(root)
        data = (json.dumps(sarif(found), indent=1, sort_keys=True) + "\n").encode("utf-8")
        written = write_new(args.out, data) if args.out else None
    except (Refused, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, Refused) else "%s: %s" % (type(exc).__name__, exc)},
                         sort_keys=True))
        return 2
    by_rule: Dict[str, int] = {}
    for item in found["results"]:
        by_rule[item["rule"]] = by_rule.get(item["rule"], 0) + 1
    print(json.dumps({"ok": True, "files": found["files"], "results": len(found["results"]), "by_rule": by_rule, "listed": listed(found),
                      "unreadable": found["unreadable"], "out": written, "sha256": sha256(data) if written else None}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
