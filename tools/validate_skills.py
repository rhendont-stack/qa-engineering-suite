"""Structural checks (L0) for every skill under ``skills/`` and every agent under ``agents/``.

Usage: python3 -I tools/validate_skills.py [root]      (root defaults to the current directory)

Per skill: the frontmatter uses block style and only the fields of the Agent Skills specification, ``name`` equals the
directory name, ``description`` is one line of at most 300 characters, every ``metadata`` value is a string, ``SKILL.md``
has at most 500 lines and the twelve body sections in order, every relative link resolves to a file inside the skill, and
no file is empty. No text under ``skills/`` (outside the vendored kernel) or ``agents/`` cites a ``§`` section number.

The frontmatter is read with ``qa.catalog.parse_frontmatter``, the parser the kernel itself uses, so this check and the
runtime cannot disagree about what a field holds. That parser ignores what it does not understand, so this script also
reports every frontmatter line it would ignore.

Prints one line per problem, ``<path>: <kind>: <detail>``, sorted, then a count. Exit 1 on any problem. With no skills it
says so and exits 0.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# ``-I`` ignores PYTHONPATH: use the kernel of the repository this script belongs to, whichever root it checks.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared" / "kernel"))

from qa import catalog  # noqa: E402

ALLOWED_FIELDS = ("name", "description", "license", "compatibility", "metadata", "allowed-tools")
REQUIRED_METADATA = ("suite", "suite-version", "role", "consumes", "produces")
SECTIONS = ("Mission", "Boundaries", "Inputs", "Tools", "Safety rules", "Procedure", "Outputs", "Acceptance criteria",
            "When things go wrong", "Red flags", "References", "Example")
MAX_DESCRIPTION = 300
MAX_COMPATIBILITY = 500
MAX_LINES = 500
SECTION_SIGN = "§"
# A YAML scalar that is not a string when written bare.
NON_STRING = re.compile(r"(-?\d+(\.\d+)?([eE][-+]?\d+)?|0x[0-9a-fA-F]+|true|false|yes|no|on|off|null|~)", re.I)
LINK = re.compile(r"(?<!\!)\[[^\]\n]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HEADING = re.compile(r"##\s+(.*?)\s*#*\s*$")
KEY = re.compile(r"([A-Za-z0-9_-]+):(?:\s+(.*))?$")

Problem = Tuple[str, str, str]


def _frontmatter_lines(lines: List[str]) -> Optional[Tuple[List[str], int]]:
    if not lines or lines[0].rstrip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].rstrip() == "---":
            return lines[1:index], index + 1
    return None


def check_frontmatter(rel: str, directory: str, front: List[str]) -> List[Problem]:
    found: List[Problem] = []
    fields = catalog.parse_frontmatter("---\n" + "\n".join(front) + "\n---\n") or {}
    in_metadata = False
    for line in front:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indented = line[0] in " \t"
        match = KEY.match(line.strip())
        if match is None:
            found.append((rel, "frontmatter_unparsed", "line is not `key: value` block style: %r" % line.strip()[:60]))
            continue
        key, raw = match.group(1), (match.group(2) or "").strip()
        if indented:
            if not in_metadata:
                found.append((rel, "frontmatter_unparsed", "nested line outside metadata: %r" % line.strip()[:60]))
            elif raw[:1] in ("[", "{"):
                found.append((rel, "flow_style", "metadata.%s uses flow style" % key))
            elif raw[:1] in ("|", ">"):
                found.append((rel, "metadata_not_string", "metadata.%s is a block scalar" % key))
            elif raw and raw[0] not in "'\"" and NON_STRING.fullmatch(raw):
                found.append((rel, "metadata_not_string", "metadata.%s is not quoted, so YAML reads it as a non-string" % key))
            continue
        in_metadata = key == "metadata" and not raw
        if key not in ALLOWED_FIELDS:
            found.append((rel, "unknown_field", "frontmatter field %r is not in the Agent Skills specification" % key))
        if raw[:1] in ("[", "{"):
            found.append((rel, "flow_style", "%s uses flow style, which the reference validator rejects" % key))
        if key == "description" and (raw[:1] in ("|", ">") or not raw):
            found.append((rel, "description_not_single_line", "description must be one line of plain text"))
    name = fields.get("name")
    if name != directory:
        found.append((rel, "wrong_name", "name %r does not equal the directory name %r" % (name, directory)))
    elif len(name) > 64 or not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
        found.append((rel, "wrong_name", "name %r is not lowercase letters, digits and single hyphens (at most 64)" % name))
    description = fields.get("description")
    if not isinstance(description, str) or not description.strip():
        found.append((rel, "missing_field", "description is required"))
    elif len(description) > MAX_DESCRIPTION:
        found.append((rel, "long_description", "description is %d characters; the limit is %d" % (len(description), MAX_DESCRIPTION)))
    compatibility = fields.get("compatibility")
    if isinstance(compatibility, str) and len(compatibility) > MAX_COMPATIBILITY:
        found.append((rel, "long_compatibility", "compatibility is %d characters; the limit is %d" % (len(compatibility), MAX_COMPATIBILITY)))
    metadata = fields.get("metadata")
    if not isinstance(metadata, dict):
        found.append((rel, "missing_field", "metadata is required"))
    else:
        for key in REQUIRED_METADATA:
            if key not in metadata:
                found.append((rel, "missing_field", "metadata.%s is required" % key))
        if metadata.get("suite", catalog.SUITE) != catalog.SUITE:
            found.append((rel, "wrong_suite", "metadata.suite must be %r" % catalog.SUITE))
    return found


def body_headings(body: List[str]) -> List[str]:
    """The level-2 headings outside code fences, in order."""
    found: List[str] = []
    fence: Optional[str] = None
    for line in body:
        marker = re.match(r"\s*(```+|~~~+)", line)
        if marker:
            fence = None if fence is not None and marker.group(1).startswith(fence[0]) else (fence or marker.group(1))
            continue
        if fence is None:
            match = HEADING.fullmatch(line.rstrip())
            if match and not line.startswith("###"):
                found.append(match.group(1))
    return found


def check_sections(rel: str, body: List[str]) -> List[Problem]:
    headings = body_headings(body)
    found: List[Problem] = []
    for name in SECTIONS:
        if name not in headings:
            found.append((rel, "missing_section", "the section `## %s` is missing" % name))
    for name in headings:
        if name not in SECTIONS:
            found.append((rel, "unexpected_section", "`## %s` is not one of the twelve sections (use ### for sub-headings)" % name))
    if not found and headings != list(SECTIONS):
        found.append((rel, "section_out_of_order", "the sections must appear once each, as: %s" % ", ".join(SECTIONS)))
    return found


def check_links(rel: str, skill: Path, body: List[str]) -> List[Problem]:
    found: List[Problem] = []
    fence: Optional[str] = None
    base = skill.resolve()
    for line in body:
        marker = re.match(r"\s*(```+|~~~+)", line)
        if marker:
            fence = None if fence is not None and marker.group(1).startswith(fence[0]) else (fence or marker.group(1))
            continue
        if fence is not None:
            continue
        for target in LINK.findall(line):
            if re.match(r"([A-Za-z][A-Za-z0-9+.-]*:|#|//)", target):
                continue
            path = target.split("#", 1)[0].split("?", 1)[0]
            if not path:
                continue
            resolved = (skill / path).resolve()
            if base != resolved and base not in resolved.parents:
                found.append((rel, "link_escapes_skill", "%s points outside the skill directory" % target))
            elif not resolved.is_file():
                found.append((rel, "broken_link", "%s does not resolve to a file" % target))
    return found


def text_files(directory: Path, skip_kernel: bool) -> List[Path]:
    found: List[Path] = []
    for here, directories, names in os.walk(str(directory), followlinks=False):
        directories[:] = sorted(d for d in directories if d != "__pycache__" and not (skip_kernel and d == "_kernel"))
        found.extend(Path(here) / name for name in sorted(names) if name != ".DS_Store")
    return found


def check_files(root: Path, directory: Path, skip_kernel: bool) -> List[Problem]:
    found: List[Problem] = []
    for path in text_files(directory, skip_kernel=False):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink() or not path.is_file():
            found.append((rel, "not_regular", "only regular files belong in a skill"))
        elif path.stat().st_size == 0:
            found.append((rel, "empty_file", "the file has no bytes"))
    for path in text_files(directory, skip_kernel):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if SECTION_SIGN in text:
            found.append((path.relative_to(root).as_posix(), "section_sign", "text shipped to users must not cite design-document section numbers"))
    return found


def check_skill(root: Path, skill: Path) -> List[Problem]:
    rel = skill.relative_to(root).as_posix() + "/SKILL.md"
    found = check_files(root, skill, skip_kernel=True)
    path = skill / "SKILL.md"
    if not path.is_file() or path.is_symlink():
        return found + [(rel, "missing_skill_md", "the skill has no regular SKILL.md")]
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return found + [(rel, "not_utf8", "SKILL.md is not UTF-8")]
    if not text.strip():
        return found  # reported as empty_file
    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        found.append((rel, "over_500_lines", "SKILL.md has %d lines; the limit is %d" % (len(lines), MAX_LINES)))
    split = _frontmatter_lines(lines)
    if split is None:
        return found + [(rel, "no_frontmatter", "SKILL.md must start with a `---` fenced frontmatter block")]
    front, body_start = split
    body = lines[body_start:]
    found += check_frontmatter(rel, skill.name, front)
    found += check_sections(rel, body)
    found += check_links(rel, skill, body)
    return found


def check(root: Path) -> Tuple[int, List[Problem]]:
    found: List[Problem] = []
    skills_dir = root / "skills"
    skills = sorted(p for p in skills_dir.iterdir() if p.is_dir() and not p.name.startswith(".")) if skills_dir.is_dir() else []
    for skill in skills:
        found += check_skill(root, skill)
    agents = root / "agents"
    if agents.is_dir():
        found += check_files(root, agents, skip_kernel=False)
    return len(skills), sorted(set(found))


def main(argv: List[str]) -> int:
    sys.stdout.reconfigure(errors="backslashreplace")
    if len(argv) > 1:
        print("usage: validate_skills.py [root]")
        return 2
    root = Path(argv[0]) if argv else Path(".")
    if not root.is_dir():
        print("validate_skills.py: not a directory: %s" % root)
        return 2
    count, found = check(root)
    for rel, kind, detail in found:
        print("%s: %s: %s" % (rel, kind, detail))
    print("%d skill(s) checked, %d problem(s)" % (count, len(found)))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
