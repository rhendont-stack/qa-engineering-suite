"""Checks a project's release process beyond its GitHub workflows: rollback documentation, base images pinned by digest, and
generic checks of other CI systems' files, as SARIF results of category ``pipeline``.

Usage: python3 release_check.py --root DIR [--out PATH]
       python3 release_check.py --copy-to-work

It walks ``DIR`` (no link is followed; dependency, build, tool and VCS directories are skipped) and reads:

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
with the reason; nothing is reported from them.

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
MAX_FILES = 5000
SKIP_DIRS = {".git", ".qa", ".qa-input", ".claude", "node_modules", "__pycache__", "vendor", ".venv", "venv", "dist", "build",
             ".tox", ".nox", ".idea", "coverage", "target", "bower_components", ".gradle", ".terraform"}
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
MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
ADOC_HEADING = re.compile(r"^(={1,6})\s+(.*)$")
RST_UNDERLINE = re.compile(r"^([=\-~^\"'`#*+])\1{2,}\s*$")
FROM = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?\s*$", re.I)
PINNED = re.compile(r"@sha256:[0-9a-f]{64}$")
TEST_COMMAND = re.compile(
    r"\b(?:py\.test|pytest|unittest|nose2|tox|nox|jest|vitest|mocha|karma|ava|rspec|phpunit|ctest|"
    r"playwright\s+test|cypress\s+run|go\s+test|cargo\s+test|dotnet\s+test|swift\s+test|mix\s+test|rake\s+(?:test|spec)|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test|make\s+(?:test|check)|mvnw?\b[^|;&\n]*\b(?:test|verify)|"
    r"gradlew?\b[^|;&\n]*\b(?:test|check))\b")
MASKED = re.compile(r"\|\|\s*(?:true\b|:(?=\s|$|;|['\"])|exit\s+0\b)")
SECRET_NAME = r"(?:[A-Za-z_][A-Za-z0-9_]*)?(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIALS?)[A-Za-z0-9_]*"
PRINTED_SECRET = re.compile(r"\b(?:echo|printf|Write-Host)\b[^\n;|&]*(?:\$\{?(%s)\b|\$\((%s)\)|\benv\.(%s)\b)" % ((SECRET_NAME,) * 3), re.I)
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


def project_files(root: Path, unreadable: List[Dict[str, Any]]) -> List[Tuple[Path, str]]:
    real_root = os.path.realpath(str(root))
    found: List[Tuple[Path, str]] = []
    for here, directories, names in os.walk(str(root)):
        kept = []
        for name in sorted(directories):
            if name in SKIP_DIRS or name.startswith(".venv"):
                continue
            if os.path.islink(os.path.join(here, name)):
                unreadable.append({"path": uri_of(Path(here) / name), "reason": "a link, not followed"})
                continue
            kept.append(name)
        directories[:] = kept
        for name in sorted(names):
            path = Path(here) / name
            relative = path.relative_to(root).as_posix()
            kind = kind_of(relative)
            if kind is None:
                continue
            info = os.lstat(str(path))
            if stat.S_ISLNK(info.st_mode):
                unreadable.append({"path": uri_of(path), "reason": "a link, not followed"})
            elif not stat.S_ISREG(info.st_mode):
                unreadable.append({"path": uri_of(path), "reason": "not a regular file"})
            elif info.st_size > MAX_BYTES:
                unreadable.append({"path": uri_of(path), "reason": "over 1 MiB"})
            elif os.path.commonpath([real_root, os.path.realpath(str(path))]) != real_root:
                unreadable.append({"path": uri_of(path), "reason": "resolves outside the root"})
            elif len(found) >= MAX_FILES:
                unreadable.append({"path": uri_of(path), "reason": "beyond the first %d files" % MAX_FILES})
            else:
                found.append((path, kind))
    return sorted(found)


def read_text(path: Path) -> str:
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("not a regular file")
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("over 1 MiB")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("not UTF-8")


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
        match = MD_HEADING.match(line) or ADOC_HEADING.match(line)
        if match:
            found.append((index + 1, match.group(2)))
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


def image_results(uri: str, lines: List[str], notes: List[str]) -> List[Dict[str, Any]]:
    found = []
    stages = set()
    logical: List[Tuple[int, str]] = []
    buffer, start = "", 0
    for index, raw in enumerate(lines):
        if not buffer:
            start = index + 1
        stripped = raw.rstrip()
        if stripped.endswith("\\"):
            buffer += stripped[:-1] + " "
            continue
        logical.append((start, buffer + stripped))
        buffer = ""
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


def ci_results(uri: str, system: str, lines: List[str], notes: List[str], inventory: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    found = []
    for index, raw in enumerate(lines):
        line = index + 1
        text = raw.split("#", 1)[0] if system != "jenkins" else raw.split("//", 1)[0]
        test = TEST_COMMAND.search(text)
        if test and MASKED.search(text[test.end():]):
            found.append(result("generic-masked-gate", uri, line, "the test command's failure is masked on its line"))
        if system == "gitlab" and re.match(r"^\s*allow_failure:\s*true\s*$", text):
            found.append(result("generic-masked-gate", uri, line, "allow_failure: true lets the job fail without failing the pipeline"))
        if system == "azure" and re.match(r"^\s*continueOnError:\s*true\s*$", text):
            found.append(result("generic-masked-gate", uri, line, "continueOnError: true lets the step fail without failing the run"))
        if system == "jenkins" and (re.search(r"catchError\s*\([^)]*buildResult\s*:\s*['\"]SUCCESS['\"]", text)
                                    or re.search(r"\bpropagate\s*:\s*false\b", text)):
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
        secret = PRINTED_SECRET.search(raw)
        if secret:
            name = next(g for g in secret.groups() if g)
            found.append(result("generic-secret-echoed", uri, line, "the variable %s, which by its name holds a secret, is printed" % name))
        inventory.extend(stage_entries(uri, system, raw, line))
    return found


def stage_entries(uri: str, system: str, raw: str, line: int) -> List[Dict[str, Any]]:
    if system == "jenkins":
        return [{"path": uri, "line": line, "kind": "stage", "name": m.group(1)}
                for m in re.finditer(r"\bstage\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", raw)]
    if system == "azure":
        match = re.match(r"^\s*-\s*(stage|job|deployment):\s*['\"]?([^'\"#]+?)['\"]?\s*(?:#.*)?$", raw)
        return [{"path": uri, "line": line, "kind": match.group(1), "name": match.group(2)}] if match else []
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


def scan(root: Path) -> Dict[str, Any]:
    unreadable: List[Dict[str, Any]] = []
    notes: List[str] = []
    inventory: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []
    counts = {"doc": 0, "image": 0, "workflow": 0, "ci": 0}
    texts: Dict[Path, Tuple[str, List[str]]] = {}
    for path, kind in project_files(root, unreadable):
        try:
            text = read_text(path)
        except (OSError, ValueError) as exc:
            unreadable.append({"path": uri_of(path), "reason": str(exc) if isinstance(exc, ValueError) else "not opened: %s" % type(exc).__name__})
            continue
        texts[path] = (kind, text.splitlines())
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
            results += image_results(uri, lines, notes)
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
    notes = [{"level": "warning", "message": {"text": "unreadable: %s: %s" % (u["path"], u["reason"])}} for u in found["unreadable"]]
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
