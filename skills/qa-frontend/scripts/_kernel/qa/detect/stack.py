"""Which languages, manifests, lockfiles and CI files a project has, each cited by its path under the root."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Tuple

from qa.workspace import read_limited

MAX_FILE_BYTES = 1 << 20
MAX_DEPTH = 2  # directories below the root that are searched for manifests
SKIPPED_DIRS = frozenset({".git", ".qa", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", "target", "build",
                          "dist", ".tox", ".gradle", ".idea", "vendor"})
LOCKFILES = frozenset({"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "poetry.lock", "uv.lock", "Pipfile.lock",
                       "Cargo.lock", "go.sum", "gradle.lockfile", "composer.lock", "Gemfile.lock"})
CI_FILES = (".gitlab-ci.yml", "Jenkinsfile", "azure-pipelines.yml", ".circleci/config.yml")
WORKFLOWS = ".github/workflows"


def _text(data: bytes) -> str:
    text = data.decode("utf-8")
    if "\x00" in text:
        raise ValueError("binary content")
    return text


def _json_object(data: bytes) -> None:
    if not isinstance(json.loads(_text(data)), dict):
        raise ValueError("not a JSON object")


def _matches(pattern: str) -> Callable[[bytes], None]:
    regex = re.compile(pattern, re.MULTILINE)

    def check(data: bytes) -> None:
        if not regex.search(_text(data)):
            raise ValueError("no recognizable content")
    return check


_TOML = r"^\s*(\[[^\]\n]+\]|[A-Za-z0-9_.\"'-]+\s*=)"
# file name -> (language, check that raises when the content cannot be read as that manifest)
MANIFESTS: Dict[str, Tuple[str, Callable[[bytes], None]]] = {
    "pyproject.toml": ("python", _matches(_TOML)),
    "requirements.txt": ("python", _matches(r"\S")),
    "package.json": ("javascript", _json_object),
    "build.gradle": ("java", _matches(r"\S")),
    "build.gradle.kts": ("kotlin", _matches(r"\S")),
    "pom.xml": ("java", _matches(r"<project\b")),
    "go.mod": ("go", _matches(r"^module\s+\S")),
    "Cargo.toml": ("rust", _matches(r"^\s*\[(package|workspace)\]")),
}


def walk(root: Path) -> Iterator[str]:
    """Sorted POSIX paths, relative to ``root``, of the files down to ``MAX_DEPTH`` directories, without dependency trees.

    Symlinked directories are not entered; symlinked files are listed so that the caller can refuse them by name.
    """
    root = Path(root)
    for here, dirs, files in os.walk(str(root)):
        rel = Path(here).relative_to(root)
        depth = len(rel.parts)
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRS and not (Path(here) / d).is_symlink() and depth < MAX_DEPTH)
        for name in sorted(files):
            yield (rel / name).as_posix()


def read(root: Path, rel: str) -> bytes:
    """The bytes of a regular file under ``root``; a symlink, a device or a huge file is an ``OSError``."""
    return read_limited(Path(root) / rel, MAX_FILE_BYTES)


def workflows(root: Path) -> List[str]:
    folder = Path(root) / WORKFLOWS
    if not folder.is_dir() or folder.is_symlink():
        return []
    return sorted("%s/%s" % (WORKFLOWS, p.name) for p in folder.iterdir() if p.suffix in (".yml", ".yaml") and not p.is_dir())


def detect(root: Path) -> dict:
    root = Path(root)
    languages = set()
    manifests: List[dict] = []
    unknowns: List[dict] = []
    lockfiles: List[str] = []
    for rel in walk(root):
        name = rel.rsplit("/", 1)[-1]
        if name in LOCKFILES:
            lockfiles.append(rel)
        if name not in MANIFESTS:
            continue
        language, check = MANIFESTS[name]
        try:
            check(read(root, rel))
        except (OSError, ValueError) as exc:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
            unknowns.append({"path": rel, "reason": "%s: %s" % (type(exc).__name__, str(exc)[:120])})
            continue
        manifests.append({"path": rel, "language": language})
        languages.add(language)
    if "javascript" in languages and (root / "tsconfig.json").is_file():
        languages.add("typescript")
    ci = workflows(root) + [p for p in CI_FILES if (root / p).is_file()]
    return {"languages": sorted(languages), "manifests": manifests, "lockfiles": lockfiles, "ci": ci, "unknowns": unknowns}
