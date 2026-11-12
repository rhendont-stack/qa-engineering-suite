"""The skill catalog: which suite skills sit next to this one, and what each declares (§7.5).

``list_skills`` reads the frontmatter of ``<skills_dir>/<skill>/SKILL.md`` and nothing else. It never runs, imports or
evaluates anything it finds, and it follows no link: a skill directory or a ``SKILL.md`` that is a symlink is skipped,
as is anything that is not a regular file or is over ``_MAX_BYTES``. Only skills whose ``metadata.suite`` is
``qa-engineering-suite`` are listed, so a neighbour from another publisher never joins a plan.

The kernel has no YAML dependency, so the frontmatter is read by a line parser that understands exactly what the suite
writes: top-level ``key: value`` lines and one level of indented ``key: value`` lines under ``metadata:``. Anything else
is ignored.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional

from .errors import QaError
from .workspace import read_limited

# QaError codes raised here: skills_dir_not_found.

SUITE = "qa-engineering-suite"
_MAX_BYTES = 256 * 1024
_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_KEY = re.compile(r"([A-Za-z0-9_-]+):(?:\s+(.*))?$")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> Optional[Dict[str, object]]:
    """``{"name": ..., "metadata": {...}, ...}`` from a ``---`` fenced block on the first line, else None."""
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        return None
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].rstrip() == "---")
    except StopIteration:
        return None
    fields: Dict[str, object] = {}
    nested: Optional[Dict[str, str]] = None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indented = line[0] in " \t"
        found = _KEY.match(line.strip())
        if found is None:
            continue
        key, value = found.group(1), _unquote(found.group(2) or "")
        if indented:
            if nested is not None:
                nested[key] = value
        elif key == "metadata" and not value:
            nested = fields["metadata"] = {}
        else:
            nested = None
            fields[key] = value
    return fields


def _words(value: str) -> List[str]:
    return [word for word in (part.strip() for part in value.split(",")) if word]


def _entry(folder: str, text: str) -> Optional[dict]:
    fields = parse_frontmatter(text)
    metadata = fields.get("metadata") if fields is not None else None
    if not isinstance(metadata, dict) or metadata.get("suite") != SUITE:
        return None
    name = fields.get("name")
    if not isinstance(name, str) or len(name) > 64 or not _NAME.fullmatch(name):
        return None
    return {"name": name, "dir": folder, "role": metadata.get("role", ""),
            "consumes": _words(metadata.get("consumes", "")), "produces": _words(metadata.get("produces", ""))}


def list_skills(skills_dir: Path) -> List[dict]:
    """The suite skills in ``skills_dir``, sorted by name: name, directory, role, and the artifact types each reads and writes."""
    skills_dir = Path(skills_dir)
    if not skills_dir.is_dir():
        raise QaError("skills_dir_not_found", "the skills directory does not exist", path=str(skills_dir))
    found: List[dict] = []
    try:
        entries = sorted(os.scandir(str(skills_dir)), key=lambda entry: entry.name)
    except OSError as exc:
        raise QaError("skills_dir_not_found", "the skills directory cannot be read", path=str(skills_dir), error=type(exc).__name__)
    for folder in entries:
        try:
            if not folder.is_dir(follow_symlinks=False):
                continue
            text = read_limited(Path(folder.path) / "SKILL.md", _MAX_BYTES).decode("utf-8")
        except (OSError, UnicodeDecodeError):  # a link, a FIFO, a device, too large, or unreadable: not a listed skill
            continue
        entry = _entry(folder.name, text)
        if entry is not None:
            found.append(entry)
    return sorted(found, key=lambda entry: (entry["name"], entry["dir"]))


def default_skills_dir(kernel_file: Optional[str] = None) -> Optional[Path]:
    """The directory holding the running skill's siblings, or None when this layout does not say.

    Installed, the kernel is ``<skill-dir>/scripts/_kernel/qa/`` and the siblings are in ``<skill-dir>/..``. In the
    source tree it is ``shared/kernel/qa/`` and the skills are in ``skills/`` at the repository root. None means
    unknown, not empty: a caller must not treat every skill as missing.
    """
    here = Path(kernel_file if kernel_file is not None else __file__).resolve().parent  # .../qa
    kernel = here.parent
    if kernel.name == "_kernel" and kernel.parent.name == "scripts":
        found = kernel.parent.parent.parent
    elif kernel.name == "kernel" and kernel.parent.name == "shared":
        found = kernel.parent.parent / "skills"
    else:
        return None
    return found if found.is_dir() else None
