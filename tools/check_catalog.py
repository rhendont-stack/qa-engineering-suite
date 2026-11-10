"""Checks ``catalog.json``: the skills it lists exist, and every responsibility key has exactly one owner.

Usage: python3 -I tools/check_catalog.py [root]      (root defaults to the current directory)

``catalog.json`` is ``{"version": 1, "skills": [<name>...], "responsibilities": [{"key": ..., "owner": <skill>}...]}``.
``skills`` lists the skills that are built, and must equal the suite skills ``qa.catalog.list_skills`` finds under
``skills/``: a listed skill with no directory, or a built skill that is not listed, is a problem. A key listed twice
(under one owner or two), a key with no owner, and an owner that is not a listed skill are problems too.

Prints one line per problem, ``catalog.json: <kind>: <detail>``, then a count. Exit 1 on any problem.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List, Tuple

# ``-I`` ignores PYTHONPATH: use the kernel of the repository this script belongs to, whichever root it checks.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared" / "kernel"))

from qa import catalog  # noqa: E402
from qa.errors import QaError  # noqa: E402

Problem = Tuple[str, str]


def built_skills(root: Path) -> List[str]:
    try:
        return [entry["name"] for entry in catalog.list_skills(root / "skills")]
    except QaError:
        return []  # no skills/ yet


def check(root: Path) -> Tuple[int, int, List[Problem]]:
    """(skills listed, keys listed, problems)"""
    path = root / "catalog.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0, 0, [("missing", "catalog.json does not exist")]
    except (ValueError, UnicodeDecodeError):
        return 0, 0, [("invalid_json", "catalog.json is not valid JSON")]
    if not isinstance(doc, dict) or set(doc) != {"version", "skills", "responsibilities"} or doc["version"] != 1 \
            or not isinstance(doc["skills"], list) or not isinstance(doc["responsibilities"], list):
        return 0, 0, [("shape", "expected exactly version (1), skills (list) and responsibilities (list)")]
    problems: List[Problem] = []
    listed = doc["skills"]
    if not all(isinstance(name, str) for name in listed):
        return 0, 0, [("shape", "skills must be a list of names")]
    if len(set(listed)) != len(listed):
        problems.append(("duplicate_skill", "a skill is listed twice"))
    built = built_skills(root)
    problems += [("skill_not_built", "%s is listed but has no skills/%s/SKILL.md of this suite" % (n, n)) for n in sorted(set(listed) - set(built))]
    problems += [("skill_not_listed", "%s is built but not listed" % n) for n in sorted(set(built) - set(listed))]
    owners: dict = {}
    for index, item in enumerate(doc["responsibilities"]):
        if not isinstance(item, dict) or set(item) != {"key", "owner"} or not isinstance(item["key"], str) \
                or not item["key"] or not isinstance(item["owner"], str):
            problems.append(("shape", "responsibilities[%d] must be {\"key\": <text>, \"owner\": <skill>}" % index))
            continue
        owners.setdefault(item["key"], []).append(item["owner"])
    for key, names in sorted(owners.items()):
        if len(names) > 1:
            problems.append(("several_owners", "%s is claimed %d times (%s)" % (key, len(names), ", ".join(sorted(names)))))
        elif not names[0]:
            problems.append(("no_owner", "%s has no owner" % key))
        elif names[0] not in listed:
            problems.append(("unknown_owner", "%s is owned by %s, which is not a listed skill" % (key, names[0])))
    return len(listed), len(owners), sorted(problems)


def main(argv: List[str]) -> int:
    sys.stdout.reconfigure(errors="backslashreplace")
    if len(argv) > 1:
        print("usage: check_catalog.py [root]")
        return 2
    root = Path(argv[0]) if argv else Path(".")
    if not root.is_dir():
        print("check_catalog.py: not a directory: %s" % root)
        return 2
    skills, keys, problems = check(root)
    for kind, detail in problems:
        print("catalog.json: %s: %s" % (kind, detail))
    print("%d skill(s) and %d responsibility key(s) checked, %d problem(s)" % (skills, keys, len(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
