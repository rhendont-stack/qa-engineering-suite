"""Checks a project's release process beyond its GitHub workflows: rollback documentation, base images pinned by digest, and
generic checks of other CI systems' files, as SARIF results of category ``pipeline``.

Usage: python3 release_check.py --root DIR [--out PATH]
       python3 release_check.py --copy-to-work

It walks ``DIR`` with its own stack, never by recursion. No link is followed. Tool, dependency, cache and VCS directories are
skipped. Build output, vendored code and coverage directories (``build``, ``dist``, ``target``, ``vendor``, ``coverage``) are
skipped with a note naming each. A directory more than 64 levels below ``DIR`` is listed ``unreadable``. It reads:

* ``rollback-documentation``: the documentation (``*.md``, ``*.rst``, ``*.adoc``, ``*.txt``). A deploy section is a heading
  whose text names a deploy, a release, a rollout or shipping and under which, before the next heading, at least two list
  items stand (the steps). When no documentation, workflow or CI file in the project describes a way back (roll back,
  rollback, a revert or a downgrade of a release, deployment, version or image, a previous or last known good release), each
  deploy section is a result at its heading. Approximate: it is read as text.
* ``base-image-digest``: every ``Dockerfile``, ``Containerfile``, ``*.Dockerfile`` and ``*.dockerfile`` (also
  ``Dockerfile.*`` and ``Containerfile.*``). Each ``FROM`` whose image is not pinned by ``@sha256:`` plus 64 hex digits is a
  result at its line; ``scratch`` and a stage named earlier with ``AS`` are not. An image set by a build argument (``$``) is
  not judged: it goes to ``notes``.
* Generic checks, each result labelled ``generic`` and approximate (read as text, no CI system's own parser), for
  ``.gitlab-ci.yml``, ``Jenkinsfile``, ``azure-pipelines.yml`` (or ``.azure-pipelines.yml``) and ``.circleci/config.yml``:
  ``generic-masked-gate`` (GitLab ``allow_failure: true``, Azure ``continueOnError: true``, Jenkins ``catchError`` with
  ``buildResult: 'SUCCESS'`` or ``propagate: false``, and in any of them a test command followed on its line by ``|| true``,
  ``|| :`` or ``|| exit 0``); ``generic-unpinned-image`` (an ``image:`` value, an Azure ``container:`` value, a Jenkins
  ``image '...'`` or ``docker.image('...')`` not pinned by digest); ``generic-secret-echoed`` (an ``echo``, ``printf`` or
  ``Write-Host`` line that prints a variable whose name says token, secret, password, api key, private key or credential).
  The stage inventory (stages and jobs with their lines) is listed under ``inventory``; it is not a result.

Results carry CWE references where a class applies (CWE-829 for an unpinned image, CWE-532 for a printed secret). Files
over 1 MiB, links, files that are not regular or not UTF-8, and paths that resolve outside ``DIR`` are listed ``unreadable``
with the reason and no line; nothing is reported from them. A line over 4096 characters (and a Dockerfile instruction
continued past that length) is listed ``unreadable`` with its line before any pattern reads it, and is read as an empty line.
A leading UTF-8 byte-order mark is dropped. Each check reads a line in one pass, so the time a check takes grows with the
size of the files, never faster.

Paths are relative to the current directory when the file lies under it (run it from the project root). ``--out PATH`` writes
SARIF 2.1.0 to a new file under ``.qa/work/release/`` or, inside a container run, ``/qa-evidence/``; without it nothing is
written. ``--copy-to-work`` writes a copy of this script to ``.qa/work/release/release_check.py`` for a container run's
``--input``. Files are written by exclusive create through no link. Prints one JSON summary. Exit 0 when the check ran
(results or not), 2 on bad arguments or a refused path. Standard library only; nothing it reads is run or followed.
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
DRIVER = "qa-release-check"
CATEGORY = "pipeline"
WORK_DIR = ".qa/work/release"
EVIDENCE_DIR = "/qa-evidence"
MAX_BYTES = 1024 * 1024
MAX_LINE = 4096
MAX_FILES = 5000
MAX_DEPTH = 64
MAX_SKIP_NOTES = 50
LONG_LINE = "a line over %d characters; it is not read" % MAX_LINE
LONG_INSTRUCTION = "an instruction continued past %d characters; it is not read" % MAX_LINE
# Skipped without a note: version control, the suite's own state, agent configuration, caches, environments and installed
# dependencies. Skipped with a note: directories of the project that hold build output, vendored code or coverage reports.
TOOL_DIRS = {".git", ".hg", ".svn", ".qa", ".qa-input", ".claude", "node_modules", "bower_components", "__pycache__", ".venv",
             "venv", ".tox", ".nox", ".idea", ".gradle", ".terraform", ".mypy_cache", ".pytest_cache"}
NOTED_DIRS = {"build", "dist", "target", "vendor", "coverage"}
RULES: Dict[str, Dict[str, Any]] = {
    "rollback-documentation": {"cwe": [], "generic": False,
                               "text": "The deploy steps have no documented way back to the previous release"},
    "base-image-digest": {"cwe": ["CWE-829"], "generic": False, "text": "A base image is pinned by a tag, not by a digest"},
    "generic-masked-gate": {"cwe": [], "generic": True, "text": "A CI step's failure does not fail the pipeline"},
    "generic-unpinned-image": {"cwe": ["CWE-829"], "generic": True, "text": "A CI job's image is pinned by a tag, not by a digest"},
    "generic-secret-echoed": {"cwe": ["CWE-532"], "generic": True, "text": "A CI step prints a secret to the job log"},
}
DOC_SUFFIXES = (".md", ".rst", ".adoc", ".txt")
CI_FILES = {".gitlab-ci.yml": "gitlab", "Jenkinsfile": "jenkins", "azure-pipelines.yml": "azure", ".azure-pipelines.yml": "azure",
            ".circleci/config.yml": "circleci"}
DEPLOY_HEADING = re.compile(r"\b(?:deploy\w*|releas\w*|roll[ -]?out\w*|ship(?:ping)?|go[ -]live|publish\w*)\b", re.I)
ROLLBACK = re.compile(r"\broll(?:ing|ed)?[ -]?backs?\b|\brollbacks?\b|\brevert(?:ing|s|ed)?\s+(?:the\s+|a\s+|to\s+(?:the\s+)?)?"
                      r"(?:release|deploy\w*|version|image|previous)\b|\bdowngrad(?:e|es|ed|ing)\b|"
                      r"\b(?:previous|prior|last\s+known\s+good)\s+(?:release|version|image|deployment|tag)\b", re.I)
LIST_ITEM = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,3}[.)])\s+\S")
MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
ADOC_HEADING = re.compile(r"^(={1,6})\s+(.*)$")
RST_UNDERLINE = re.compile(r"^([=\-~^\"'`#*+])\1{2,}\s*$")
FROM = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?\s*$", re.I)
PINNED = re.compile(r"@sha256:[0-9a-f]{64}$")
# The test command, shared word for word with workflow_scan.py (a test asserts it): a runner's word, or a build tool with a test
# goal later in the same command. test_command_end reads a line in one pass.
TEST_WORD = re.compile(
    r"\b(?:py\.test|pytest|unittest|nose2|tox|nox|jest|vitest|mocha|karma|ava|rspec|phpunit|ctest|"
    r"playwright\s+test|cypress\s+run|go\s+test|cargo\s+test|dotnet\s+test|swift\s+test|mix\s+test|rake\s+(?:test|spec)|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test|make\s+(?:test|check))\b")
BUILD_TOOL = re.compile(r"\b(?:(mvnw?)|(gradlew?))\b")
BUILD_GOAL = {"mvn": re.compile(r"\b(?:test|verify)\b"), "gradle": re.compile(r"\b(?:test|check)\b")}
COMMAND_END = re.compile(r"[|;&\n]")
MASKED = re.compile(r"\|\|\s*(?:true\b|:(?=\s|$|;|['\"])|exit\s+0\b)")
SECRET_WORD = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIALS?", re.I)
PRINT_WORD = re.compile(r"\b(?:echo|printf|Write-Host)\b", re.I)
PRINT_END = re.compile(r"[\n;|&]")
VARIABLE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\b|\$\(([A-Za-z_][A-Za-z0-9_]*)\)|\benv\.([A-Za-z_][A-Za-z0-9_]*)\b", re.I)
CATCH_ERROR = re.compile(r"catchError\s*\(")
SUCCESS_RESULT = re.compile(r"buildResult\s*:\s*['\"]SUCCESS['\"]")
AZURE_ENTRY = re.compile(r"^\s*-\s*(stage|job|deployment):\s*(.*)$")
YAML_IMAGE = re.compile(r"^\s*(?:-\s+)?(?:image|container):\s*['\"]?([^\s'\"#{}\[\]]+)['\"]?\s*(?:#.*)?$")
JENKINS_IMAGE = re.compile(r"\bimage\s+['\"]([^'\"]+)['\"]|\bdocker\.image\(\s*['\"]([^'\"]+)['\"]")
GITLAB_RESERVED = {"stages", "variables", "default", "include", "workflow", "image", "services", "before_script", "after_script",
                   "cache", "pages"}


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


# --------------------------------------------------------------------------- shared with workflow_scan.py, word for word

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


# --------------------------------------------------------------------------- files

def uri_of(path: Path) -> str:
    absolute = Path(os.path.abspath(str(path)))
    try:
        return absolute.relative_to(Path(os.path.abspath(os.getcwd()))).as_posix()
    except ValueError:
        return absolute.as_posix()


def kind_of(relative: str) -> Optional[str]:
    name = relative.rsplit("/", 1)[-1]
    if relative in CI_FILES or name in ("Jenkinsfile",):
        return "ci:" + CI_FILES.get(relative, "jenkins")
    if relative.startswith(".github/workflows/") and relative.count("/") == 2 and name.endswith((".yml", ".yaml")):
        return "workflow"
    if name in ("Dockerfile", "Containerfile") or name.startswith(("Dockerfile.", "Containerfile.")) \
            or name.endswith((".Dockerfile", ".dockerfile")):
        return "image"
    if name.lower().endswith(DOC_SUFFIXES):
        return "doc"
    return None


def unreadable_entry(path: Path, reason: str, line: Optional[int] = None) -> Dict[str, Any]:
    return {"path": uri_of(path), "line": line, "reason": reason}


def project_files(root: Path, unreadable: List[Dict[str, Any]], notes: List[str]) -> List[Tuple[Path, str]]:
    """The files the checks read, by a walk with its own stack (top-down, each directory's entries in name order)."""
    real_root = os.path.realpath(str(root))
    found: List[Tuple[Path, str]] = []
    noted = 0
    stack: List[Tuple[Path, int]] = [(root, 0)]
    while stack:
        here, depth = stack.pop()
        try:
            names = sorted(os.listdir(str(here)))
        except OSError as exc:
            unreadable.append(unreadable_entry(here, "not listed: %s" % (exc.strerror or type(exc).__name__)))
            continue
        folders: List[Path] = []
        for name in names:
            path = here / name
            try:
                info = os.lstat(str(path))
            except OSError as exc:
                unreadable.append(unreadable_entry(path, "not opened: %s" % (exc.strerror or type(exc).__name__)))
                continue
            link = stat.S_ISLNK(info.st_mode)
            if stat.S_ISDIR(info.st_mode) or (link and os.path.isdir(str(path))):
                if name in TOOL_DIRS or name.startswith(".venv"):
                    continue
                if link:
                    unreadable.append(unreadable_entry(path, "a link, not followed"))
                elif name in NOTED_DIRS:
                    noted += 1
                    if noted <= MAX_SKIP_NOTES:
                        notes.append("%s/: skipped (build output, vendored code or a coverage report); a Dockerfile, CI file or "
                                     "document kept there is not checked" % uri_of(path))
                elif depth + 1 > MAX_DEPTH:
                    unreadable.append(unreadable_entry(path, "more than %d directories below the root; not walked" % MAX_DEPTH))
                else:
                    folders.append(path)
                continue
            kind = kind_of(path.relative_to(root).as_posix())
            if kind is None:
                continue
            if link:
                unreadable.append(unreadable_entry(path, "a link, not followed"))
            elif not stat.S_ISREG(info.st_mode):
                unreadable.append(unreadable_entry(path, "not a regular file"))
            elif info.st_size > MAX_BYTES:
                unreadable.append(unreadable_entry(path, "over 1 MiB"))
            elif os.path.commonpath([real_root, os.path.realpath(str(path))]) != real_root:
                unreadable.append(unreadable_entry(path, "resolves outside the root"))
            elif len(found) >= MAX_FILES:
                unreadable.append(unreadable_entry(path, "beyond the first %d files" % MAX_FILES))
            else:
                found.append((path, kind))
        stack.extend((folder, depth + 1) for folder in reversed(folders))
    if noted > MAX_SKIP_NOTES:
        notes.append("%d more skipped directories of build output, vendored code or coverage reports are not named"
                     % (noted - MAX_SKIP_NOTES))
    return sorted(found)


def read_text(path: Path) -> str:
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("not a regular file")
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("over 1 MiB")
    text = utf8_text(data)
    if text is None:
        raise ValueError("not UTF-8")
    return text


# --------------------------------------------------------------------------- the checks

def result(rule: str, uri: str, line: int, message: str, approximate: bool = False) -> Dict[str, Any]:
    info = RULES[rule]
    return {"rule": rule, "path": uri, "line": line, "message": message, "cwe": info["cwe"], "generic": info["generic"],
            "approximate": approximate or info["generic"]}


def fenced_lines(lines: List[str]) -> set:
    """Indexes of the lines inside a fenced code block, the fences included: neither a heading nor a step."""
    found = set()
    inside = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            inside = not inside
            found.add(index)
        elif inside:
            found.add(index)
    return found


def headings(lines: List[str]) -> List[Tuple[int, str]]:
    """``(line, text)`` of each heading: Markdown ``#``, AsciiDoc ``=``, or reStructuredText text over an underline."""
    found = []
    fenced = fenced_lines(lines)
    for index, line in enumerate(lines):
        if index in fenced:
            continue
        markdown = MD_HEADING.match(line)
        asciidoc = None if markdown else ADOC_HEADING.match(line)
        if markdown:   # the closing run of # and the blanks around it are cut in code, not by the pattern
            found.append((index + 1, markdown.group(2).rstrip().rstrip("#").rstrip()))
        elif asciidoc:
            found.append((index + 1, asciidoc.group(2)))
        elif index and RST_UNDERLINE.match(line) and lines[index - 1].strip() and not RST_UNDERLINE.match(lines[index - 1]) \
                and len(line.strip()) >= len(lines[index - 1].strip()):
            found.append((index, lines[index - 1].strip()))
    return found


def deploy_sections(lines: List[str]) -> List[Tuple[int, str]]:
    """Deploy headings with at least two list items under them (outside code blocks) before the next heading."""
    marks = headings(lines)
    fenced = fenced_lines(lines)
    found = []
    for position, (line, text) in enumerate(marks):
        if not DEPLOY_HEADING.search(text):
            continue
        end = marks[position + 1][0] - 1 if position + 1 < len(marks) else len(lines)
        steps = [l for index, l in enumerate(lines[line:end], line) if index not in fenced and LIST_ITEM.match(l)]
        if len(steps) >= 2:
            found.append((line, text))
    return found


def image_results(path: Path, lines: List[str], notes: List[str], unreadable: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    uri = uri_of(path)
    found = []
    stages = set()
    logical: List[Tuple[int, str]] = []
    parts: List[str] = []
    size, start = 0, 0
    for index, raw in enumerate(lines):
        if not parts:
            start, size = index + 1, 0
        stripped = raw.rstrip()
        continued = stripped.endswith("\\")
        parts.append(stripped[:-1] + " " if continued else stripped)
        size += len(parts[-1])
        if continued:
            continue
        if size > MAX_LINE:
            unreadable.append(unreadable_entry(path, LONG_INSTRUCTION, start))
        else:
            logical.append((start, "".join(parts)))
        parts = []
    for line, text in logical:
        match = FROM.match(text)
        if not match:
            continue
        image, alias = match.group(1), match.group(2)
        if alias:
            stages.add(alias.lower())
        if image.lower() == "scratch" or image.lower() in stages:
            continue
        if "$" in image:
            notes.append("%s:%d: the base image is set by a build argument (%s); not judged" % (uri, line, image))
            continue
        if not PINNED.search(image):
            found.append(result("base-image-digest", uri, line, "FROM %s names the image by a tag, which can be moved; pin it by "
                                                                "@sha256 digest" % image))
    return found


def pinned_image(value: str) -> bool:
    return bool(PINNED.search(value))


def caught_as_success(text: str) -> bool:
    """A ``catchError(`` whose arguments, up to the next ``)``, set ``buildResult: 'SUCCESS'``. The first opener of a stretch
    with no ``)`` is searched to that stretch's end, which covers every later opener in it."""
    covered = -1
    for opener in CATCH_ERROR.finditer(text):
        if opener.start() < covered:
            continue
        close = text.find(")", opener.end())
        covered = len(text) if close < 0 else close
        if SUCCESS_RESULT.search(text, opener.end(), covered):
            return True
    return False


def printed_secret(raw: str) -> Optional[str]:
    """The last variable named like a secret in the first print command on the line that prints one, or None. A print
    command runs from ``echo``, ``printf`` or ``Write-Host`` to the next separator; a later print word inside it is part of it
    and is not read again."""
    covered = -1
    for word in PRINT_WORD.finditer(raw):
        if word.start() < covered:
            continue
        stop = PRINT_END.search(raw, word.end())
        covered = stop.start() if stop else len(raw)
        names = [name for match in VARIABLE.finditer(raw, word.end(), covered) for name in match.groups() if name]
        secret = [name for name in names if SECRET_WORD.search(name)]
        if secret:
            return secret[-1]
    return None


def ci_results(uri: str, system: str, lines: List[str], notes: List[str], inventory: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    found = []
    for index, raw in enumerate(lines):
        line = index + 1
        text = raw.split("#", 1)[0] if system != "jenkins" else raw.split("//", 1)[0]
        end = test_command_end(text)
        if end is not None and MASKED.search(text, end):
            found.append(result("generic-masked-gate", uri, line, "the test command's failure is masked on its line"))
        if system == "gitlab" and re.match(r"^\s*allow_failure:\s*true\s*$", text):
            found.append(result("generic-masked-gate", uri, line, "allow_failure: true lets the job fail without failing the pipeline"))
        if system == "azure" and re.match(r"^\s*continueOnError:\s*true\s*$", text):
            found.append(result("generic-masked-gate", uri, line, "continueOnError: true lets the step fail without failing the run"))
        if system == "jenkins" and (caught_as_success(text) or re.search(r"\bpropagate\s*:\s*false\b", text)):
            found.append(result("generic-masked-gate", uri, line, "the stage's failure is caught and the build still reads as a success"))
        images = []
        if system == "jenkins":
            images = [m.group(1) or m.group(2) for m in JENKINS_IMAGE.finditer(text)]
        else:
            match = YAML_IMAGE.match(text)
            if match:
                images = [match.group(1)]
        for image in images:
            if "$" in image:
                notes.append("%s:%d: the image is set by a variable (%s); not judged" % (uri, line, image))
            elif not pinned_image(image):
                found.append(result("generic-unpinned-image", uri, line, "the image %s is named by a tag, not an @sha256 digest" % image))
        name = printed_secret(raw)
        if name:
            found.append(result("generic-secret-echoed", uri, line, "the variable %s, which by its name holds a secret, is printed" % name))
        inventory.extend(stage_entries(uri, system, raw, line))
    return found


def stage_entries(uri: str, system: str, raw: str, line: int) -> List[Dict[str, Any]]:
    if system == "jenkins":
        return [{"path": uri, "line": line, "kind": "stage", "name": m.group(1)}
                for m in re.finditer(r"\bstage\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", raw)]
    if system == "azure":
        match = AZURE_ENTRY.match(raw)
        name = azure_name(match.group(2)) if match else None
        return [{"path": uri, "line": line, "kind": match.group(1), "name": name}] if match and name else []
    if system == "gitlab":
        match = re.match(r"^([A-Za-z0-9_.\-][^:#]*):\s*(?:#.*)?$", raw)
        if match and match.group(1) not in GITLAB_RESERVED and not match.group(1).startswith("."):
            return [{"path": uri, "line": line, "kind": "job", "name": match.group(1)}]
        stage = re.match(r"^\s+stage:\s*['\"]?([^'\"#\s]+)", raw)
        return [{"path": uri, "line": line, "kind": "stage", "name": stage.group(1)}] if stage else []
    if system == "circleci":
        match = re.match(r"^  ([A-Za-z0-9_.\-]+):\s*(?:#.*)?$", raw)
        return [{"path": uri, "line": line, "kind": "job-or-workflow", "name": match.group(1)}] if match else []
    return []


def azure_name(rest: str) -> Optional[str]:
    """The name after ``stage:``, ``job:`` or ``deployment:``: the text before a comment, one quote cut at each end, and no quote
    left inside; None when nothing is left."""
    body = rest.split("#", 1)[0].rstrip()
    if body.startswith(("'", '"')):
        body = body[1:]
    if body.endswith(("'", '"')):
        body = body[:-1]
    return body if body and "'" not in body and '"' not in body else None


def scan(root: Path) -> Dict[str, Any]:
    unreadable: List[Dict[str, Any]] = []
    notes: List[str] = []
    inventory: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []
    counts = {"doc": 0, "image": 0, "workflow": 0, "ci": 0}
    texts: Dict[Path, Tuple[str, List[str]]] = {}
    for path, kind in project_files(root, unreadable, notes):
        try:
            text = read_text(path)
        except (OSError, ValueError) as exc:
            unreadable.append(unreadable_entry(path, str(exc) if isinstance(exc, ValueError) else "not opened: %s" % type(exc).__name__))
            continue
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if len(line) > MAX_LINE:   # listed before any pattern reads it, then read as an empty line
                unreadable.append(unreadable_entry(path, LONG_LINE, index + 1))
                lines[index] = ""
        texts[path] = (kind, lines)
        counts[kind.split(":")[0]] += 1
    rollback = any(ROLLBACK.search("\n".join(lines)) for kind, lines in texts.values() if kind == "doc" or kind == "workflow"
                   or kind.startswith("ci:"))
    for path, (kind, lines) in sorted(texts.items()):
        uri = uri_of(path)
        if kind == "doc" and not rollback:
            for line, text in deploy_sections(lines):
                results.append(result("rollback-documentation", uri, line, "the section %r lists deploy steps, and no document, workflow "
                                                                           "or CI file in the project describes a way back to the "
                                                                           "previous release" % text, approximate=True))
        elif kind == "image":
            results += image_results(path, lines, notes, unreadable)
        elif kind.startswith("ci:"):
            results += ci_results(uri, kind[3:], lines, notes, inventory)
    results.sort(key=lambda r: (r["path"], r["line"], r["rule"]))
    return {"results": results, "unreadable": unreadable, "notes": notes, "inventory": inventory, "files": counts,
            "rollback_documented": rollback}


# --------------------------------------------------------------------------- output

def sarif(found: Dict[str, Any]) -> Dict[str, Any]:
    rules = [{"id": rule, "shortDescription": {"text": info["text"]},
              "properties": {"category": CATEGORY, "cwe": info["cwe"], "generic": info["generic"],
                             "tags": [CATEGORY] + (["generic"] if info["generic"] else []) + info["cwe"]}} for rule, info in RULES.items()]
    results = [{"ruleId": r["rule"], "level": "warning", "message": {"text": r["message"]},
                "locations": [{"physicalLocation": {"artifactLocation": {"uri": r["path"]}, "region": {"startLine": r["line"]}}}],
                "properties": {"category": CATEGORY, "cwe": r["cwe"], "generic": r["generic"], "approximate": r["approximate"]}}
               for r in found["results"]]
    notes = [{"level": "warning", "message": {"text": "unreadable: %s%s: %s" % (u["path"], ":%d" % u["line"] if u["line"] else "", u["reason"])}}
             for u in found["unreadable"]]
    notes += [{"level": "note", "message": {"text": n}} for n in found["notes"]]
    return {"version": "2.1.0", "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [{"tool": {"driver": {"name": DRIVER, "version": VERSION, "rules": rules}},
                      "invocations": [{"executionSuccessful": True, "toolExecutionNotifications": notes}],
                      "results": results,
                      "properties": {"category": CATEGORY, "unreadable": found["unreadable"], "notes": found["notes"],
                                     "inventory": found["inventory"], "files": found["files"]}}]}


def listed(found: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"rule": r["rule"], "at": "%s:%d" % (r["path"], r["line"]), "cwe": r["cwe"], "generic": r["generic"],
             "approximate": r["approximate"], "message": r["message"]} for r in found["results"]]


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
            written = write_new(WORK_DIR + "/release_check.py", data)
            print(json.dumps({"ok": True, "written": written, "sha256": sha256(data),
                              "input": "/work/.qa-input/release/release_check.py"}, sort_keys=True))
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
                      "unreadable": found["unreadable"], "notes": found["notes"], "inventory": found["inventory"],
                      "rollback_documented": found["rollback_documented"], "out": written, "sha256": sha256(data) if written else None},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
