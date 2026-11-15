"""The commands that test a project, ranked by how well the project itself vouches for them: CI, then documentation, then inference."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from . import stack

KINDS = ("ci", "documented", "inferred")
MAX_COMMAND = 300
# framework -> (pattern a command line matches, the JUnit or CTRF flag; None when the tool writes its report on its own or has none)
FRAMEWORKS: Dict[str, Tuple["re.Pattern[str]", Optional[str]]] = {
    "playwright": (re.compile(r"\bplaywright\s+test\b"), "--reporter=junit"),
    "cypress": (re.compile(r"\bcypress\s+run\b"), "--reporter junit --reporter-options mochaFile={evidence}/junit.xml"),
    "vitest": (re.compile(r"(?<![\w-])vitest(?![\w-])"), "--reporter=junit --outputFile.junit={evidence}/junit.xml"),
    "jest": (re.compile(r"(?<![\w-])jest(?![\w-])"), "--reporters=default --reporters=jest-junit"),
    "pytest": (re.compile(r"(?:^|\s)(?:pytest|py\.test)\b|\bpython3?\s+-m\s+pytest\b"), "--junitxml={evidence}/junit.xml"),
    "gradle": (re.compile(r"\bgradlew?\s+(?:\S+\s+)*?(?:test|check|build)\b|\./gradlew\s+(?:\S+\s+)*?(?:test|check|build)\b"), None),
    "maven": (re.compile(r"\bmvnw?\s+(?:\S+\s+)*?(?:test|verify)\b"), None),
    "go test": (re.compile(r"\bgo\s+test\b"), None),
    "cargo test": (re.compile(r"\bcargo\s+(?:\+\S+\s+)?test\b"), None),
}
_PACKAGE_TEST = re.compile(r"\b(?:npm|yarn|pnpm)\s+(?:run\s+)?test\b")
# A segment that installs or removes a package names the tool without running it.
_INSTALLER = re.compile(
    r"^(?:sudo\s+)?(?:(?:pip3?|pipx|uv\s+pip|python3?\s+-m\s+pip|poetry|conda|uv)\s+(?:install|add|remove|uninstall)"
    r"|(?:npm|pnpm|bun)\s+(?:install|i|add|ci|uninstall|remove|rm)|yarn\s+(?:add|install|remove)"
    r"|apt(?:-get)?\s+(?:install|remove)|brew\s+install|cargo\s+install|go\s+install|gem\s+install)\b")
_SEGMENTS = re.compile(r"&&|\|\||;")
_RUN = re.compile(r"^(\s*)(?:-\s+)?run:\s*(.*?)\s*$")
_BLOCK = re.compile(r"^[|>][+-]?\d*$")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_DOC_NAME = re.compile(r"^(readme|contributing)[^/]*$", re.IGNORECASE)
_PROMPT = re.compile(r"^(?:\$|>)\s+")
_JS_INFERRED = (("playwright", "@playwright/test", "npx playwright test"), ("cypress", "cypress", "npx cypress run"),
                ("vitest", "vitest", "npx vitest run"), ("jest", "jest", "npx jest"))


def _candidate(framework: str, command: str, kind: str, path: str, line: Optional[int]) -> dict:
    return {"framework": framework, "command": command, "report_flag": FRAMEWORKS[framework][1],
            "source": "%s:%d" % (path, line) if line else path, "source_kind": kind, "cited_path": path}


def _package_scripts(root: Path) -> Dict[str, str]:
    try:
        data = json.loads(stack.read(root, "package.json").decode("utf-8"))
    except (OSError, ValueError):
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return {k: v for k, v in scripts.items() if isinstance(v, str)} if isinstance(scripts, dict) else {}


def _match(line: str, scripts: Dict[str, str]) -> Iterator[Tuple[str, str]]:
    """(framework, command) for each segment of ``line`` that runs a test tool."""
    for segment in _SEGMENTS.split(line):
        segment = _PROMPT.sub("", segment.strip())
        if not segment or segment.startswith("#") or len(segment) > MAX_COMMAND or not segment.isprintable():
            continue
        if _INSTALLER.match(segment):
            continue
        target = scripts.get("test", "") if _PACKAGE_TEST.search(segment) else segment
        for framework, (pattern, _flag) in FRAMEWORKS.items():
            if pattern.search(target):
                yield framework, segment
                break


def _ci_lines(text: str) -> Iterator[Tuple[int, str]]:
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        found = _RUN.match(lines[index])
        index += 1
        if not found:
            continue
        column = lines[index - 1].index("run:")
        value = found.group(2)
        if _BLOCK.match(value):
            while index < len(lines):
                body = lines[index]
                if body.strip() and len(body) - len(body.lstrip()) <= column:
                    break
                index += 1
                if body.strip():
                    yield index, body.strip()
        elif value:
            yield index, value.strip("\"'")


def _documented_lines(text: str) -> Iterator[Tuple[int, str]]:
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif fenced:
            yield number, line.strip()
        else:
            for span in _INLINE_CODE.findall(line):
                yield number, span


def _from_files(root: Path, paths: List[str], kind: str, lines, scripts: Dict[str, str]) -> Iterator[dict]:
    for rel in paths:
        try:
            text = stack.read(root, rel).decode("utf-8")
        except (OSError, ValueError):
            continue
        for number, line in lines(text):
            for framework, command in _match(line, scripts):
                yield _candidate(framework, command, kind, rel, number)


def _inferred(root: Path) -> Iterator[dict]:
    def has(name: str) -> bool:
        return (root / name).is_file() and not (root / name).is_symlink()

    def text(name: str) -> str:
        try:
            return stack.read(root, name).decode("utf-8")
        except (OSError, ValueError):
            return ""

    for name, marker in (("pyproject.toml", "[tool.pytest"), ("setup.cfg", "[tool:pytest]"), ("tox.ini", "[pytest]")):
        if marker in text(name):
            yield _candidate("pytest", "python -m pytest", "inferred", name, None)
    for name in ("pytest.ini", "conftest.py"):  # presence only: conftest.py is never read, let alone imported
        if has(name):
            yield _candidate("pytest", "python -m pytest", "inferred", name, None)
    if has("package.json"):
        try:
            data = json.loads(text("package.json"))
        except ValueError:
            data = {}
        declared = {}
        for table in ("dependencies", "devDependencies"):
            if isinstance(data, dict) and isinstance(data.get(table), dict):
                declared.update(data[table])
        for framework, package, command in _JS_INFERRED:
            if package in declared:
                yield _candidate(framework, command, "inferred", "package.json", None)
    for name in ("build.gradle", "build.gradle.kts"):
        if has(name):
            yield _candidate("gradle", "./gradlew test" if has("gradlew") else "gradle test", "inferred", name, None)
    if has("pom.xml") and "<project" in text("pom.xml"):
        yield _candidate("maven", "./mvnw test" if has("mvnw") else "mvn test", "inferred", "pom.xml", None)
    if "module" in text("go.mod"):
        yield _candidate("go test", "go test ./...", "inferred", "go.mod", None)
    if "[package]" in text("Cargo.toml") or "[workspace]" in text("Cargo.toml"):
        yield _candidate("cargo test", "cargo test", "inferred", "Cargo.toml", None)


def detect(root: Path) -> List[dict]:
    """One candidate per framework, from the best source that names it; CI first, then documentation, then inference."""
    root = Path(root)
    scripts = _package_scripts(root)
    ci_paths = stack.workflows(root)
    doc_paths = sorted(p for p in stack.walk(root) if "/" not in p and _DOC_NAME.match(p))
    ordered = list(_from_files(root, ci_paths, "ci", _ci_lines, scripts))
    ordered += _from_files(root, doc_paths, "documented", _documented_lines, scripts)
    ordered += _inferred(root)
    best: Dict[str, dict] = {}
    for found in sorted(ordered, key=lambda c: KINDS.index(c["source_kind"])):  # stable: file order decides within a kind
        best.setdefault(found["framework"], found)
    return sorted(best.values(), key=lambda c: KINDS.index(c["source_kind"]))
