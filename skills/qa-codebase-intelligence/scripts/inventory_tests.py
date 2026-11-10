"""Inventories the existing automated tests of a repository without running any of them.

Usage: python3 inventory_tests.py [--root DIR]

Test files are found by convention (``test_*.py``, ``*_test.py``, ``*.test.js``, ``*.spec.ts``, ``*Test.java``, ``*_test.go``
and files under ``tests``, ``test`` or ``__tests__`` that match those patterns' language); tests are counted by pattern, so a
count is an estimate that parametrized or generated tests can exceed. The framework comes from configuration files and
manifests when there are any, and the command from a script the project declares, else from the framework's usual command,
which is marked ``inferred``.

Prints one JSON object: ``{"suites": [{"framework", "command", "command_source", "config", "files", "test_count"}],
"limitations": [...]}``. It only reads; exit 0.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import _repo  # noqa: E402

TEST_FILE = {
    "python": re.compile(r"(^|/)(test_[^/]*|[^/]*_test)\.py$"),
    "javascript": re.compile(r"(^|/)([^/]*\.(test|spec)\.(js|jsx|mjs|cjs)|__tests__/[^/]*\.(js|jsx|mjs|cjs))$"),
    "typescript": re.compile(r"(^|/)([^/]*\.(test|spec)\.(ts|tsx)|__tests__/[^/]*\.(ts|tsx))$"),
    "java": re.compile(r"(^|/)[^/]*(Test|Tests|IT)\.java$"),
    "kotlin": re.compile(r"(^|/)[^/]*(Test|Tests)\.kt$"),
    "go": re.compile(r"(^|/)[^/]*_test\.go$"),
}
COUNT = {
    "python": re.compile(r"^\s*(?:async\s+)?def\s+test\w*\s*\(", re.M),
    "javascript": re.compile(r"^\s*(?:it|test)(?:\.\w+)*\s*\(", re.M),
    "typescript": re.compile(r"^\s*(?:it|test)(?:\.\w+)*\s*\(", re.M),
    "java": re.compile(r"^\s*@(?:Test|ParameterizedTest|RepeatedTest)\b", re.M),
    "kotlin": re.compile(r"^\s*@(?:Test|ParameterizedTest)\b", re.M),
    "go": re.compile(r"^func\s+Test\w*\s*\(", re.M),
}
JS_FRAMEWORKS = (("vitest", "vitest"), ("jest", "jest"), ("mocha", "mocha"), ("@playwright/test", "playwright"), ("cypress", "cypress"))


def declared_scripts(root: Path, names: List[str]) -> Dict[str, Any]:
    """What package.json files declare: their dependency names, their ``test`` script, and the file they came from."""
    found: Dict[str, Any] = {}
    for rel in names:
        if rel.rsplit("/", 1)[-1] != "package.json":
            continue
        try:
            doc = json.loads(_repo.read_text(root, rel) or "")
        except ValueError:
            continue
        if isinstance(doc, dict):
            deps = {}
            for key in ("dependencies", "devDependencies"):
                if isinstance(doc.get(key), dict):
                    deps.update(doc[key])
            scripts = doc.get("scripts") if isinstance(doc.get("scripts"), dict) else {}
            found[rel] = {"deps": sorted(deps), "test": scripts.get("test")}
    return found


def python_framework(root: Path, names: List[str], test_files: List[str]) -> Dict[str, Any]:
    config = [n for n in names if n.rsplit("/", 1)[-1] in ("pytest.ini", "conftest.py", "tox.ini")]
    for rel in names:
        base = rel.rsplit("/", 1)[-1]
        if base in ("pyproject.toml", "setup.cfg", "requirements.txt", "requirements-dev.txt"):
            text = _repo.read_text(root, rel) or ""
            if "pytest" in text:
                config.append(rel)
    if config:
        return {"framework": "pytest", "config": sorted(set(config)), "command": "python3 -m pytest", "source": "inferred"}
    for rel in test_files:
        if re.search(r"^\s*(?:import unittest|from unittest)", _repo.read_text(root, rel) or "", re.M):
            return {"framework": "unittest", "config": [], "command": "python3 -m unittest", "source": "inferred"}
    return {"framework": "python-tests", "config": [], "command": None, "source": "none"}


def inventory(root: Path) -> Dict[str, Any]:
    root = Path(root)
    names = _repo.files(root)
    limitations: List[str] = []
    by_language: Dict[str, List[str]] = {}
    for rel in names:
        for language, pattern in TEST_FILE.items():
            if _repo.language_of(rel) == language and pattern.search(rel):
                by_language.setdefault(language, []).append(rel)
    scripts = declared_scripts(root, names)
    suites: List[Dict[str, Any]] = []
    for language, files in sorted(by_language.items()):
        if language == "python":
            info = python_framework(root, names, files)
        elif language in ("javascript", "typescript"):
            owner = next((rel for rel, d in sorted(scripts.items()) if any(f.startswith(rel.rsplit("/", 1)[0] + "/") or "/" not in rel for f in files)), None)
            deps = scripts.get(owner, {}).get("deps", []) if owner else []
            framework = next((label for dep, label in JS_FRAMEWORKS if dep in deps), "{}-tests".format(language))
            declared = scripts.get(owner, {}).get("test") if owner else None
            where = owner.rsplit("/", 1)[0] if owner and "/" in owner else None
            prefix = "cd %s && " % where if where else ""
            info = {"framework": framework, "config": [owner] if owner else [], "source": "declared" if declared else "none",
                    "command": (prefix + "npm test") if declared else None}
        elif language in ("java", "kotlin"):
            gradle = next((n for n in names if n.rsplit("/", 1)[-1] in ("build.gradle", "build.gradle.kts")), None)
            pom = next((n for n in names if n.rsplit("/", 1)[-1] == "pom.xml"), None)
            build = gradle or pom
            text = (_repo.read_text(root, build) or "") if build else ""
            framework = "junit" if "junit" in text.lower() else ("testng" if "testng" in text.lower() else "{}-tests".format(language))
            wrapper = next((n for n in names if n.rsplit("/", 1)[-1] == "gradlew"), None)
            if gradle:
                folder = gradle.rsplit("/", 1)[0] if "/" in gradle else None
                command = ("cd %s && " % folder if folder else "") + ("./gradlew test" if wrapper else "gradle test")
            else:
                command = "mvn test" if pom else None
            info = {"framework": framework, "config": [build] if build else [], "command": command, "source": "inferred" if build else "none"}
        else:
            info = {"framework": "go test", "config": [n for n in names if n.endswith("go.mod")][:1], "command": "go test ./...", "source": "inferred"}
        count = sum(len(COUNT[language].findall(_repo.read_text(root, f) or "")) for f in files)
        suites.append({"framework": info["framework"], "command": info["command"], "command_source": info["source"],
                       "config": info["config"], "files": files, "test_count": count})
    if not suites:
        limitations.append("no test files found by convention")
    limitations.append("test counts are pattern counts: parametrized or generated tests are not expanded")
    return {"suites": suites, "limitations": limitations}


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.root):
        print("error: --root must be a directory", file=sys.stderr)
        return 2
    print(json.dumps(inventory(Path(args.root)), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
