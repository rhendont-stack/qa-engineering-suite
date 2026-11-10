"""Vendors the shared kernel into every skill, or checks that the vendored copies are current.

Usage: python3 -I tools/vendor_kernel.py [--check] [--root <repository>]

For each ``skills/<skill>/`` it writes ``scripts/_kernel/`` (``qa/``, ``schemas/``, ``policies/``, ``templates/`` from
``shared/``, plus ``MANIFEST.sha256``) and ``scripts/qa.py`` (the launcher). ``__pycache__`` and ``*.pyc`` are never
copied, and neither is anything in ``shared/`` that is not a regular file. ``--check`` writes nothing: it prints one line
per difference and exits 1 if there is any. With no ``skills/`` directory, or no skill in it, both modes report that
they checked nothing and exit 0.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Dict, List, Optional

KERNEL_DIRECTORIES = (("qa", ("shared", "kernel", "qa")), ("schemas", ("shared", "schemas")),
                      ("policies", ("shared", "policies")), ("templates", ("shared", "templates")))
LAUNCHER = ("shared", "launcher", "qa.py")
MANIFEST = "MANIFEST.sha256"  # the name integrity.kernel_integrity reads


def _regular(path: str) -> bool:
    return stat.S_ISREG(os.lstat(path).st_mode)


def source_tree(root: Path) -> Dict[str, bytes]:
    """``{path relative to scripts/: content}`` that every skill must hold, including the manifest."""
    tree: Dict[str, bytes] = {}
    for target, parts in KERNEL_DIRECTORIES:
        base = root.joinpath(*parts)
        for here, directories, names in os.walk(str(base), followlinks=False):
            directories[:] = sorted(d for d in directories if d != "__pycache__")
            for name in sorted(names):
                path = os.path.join(here, name)
                if name.endswith(".pyc") or name == ".DS_Store" or not _regular(path):
                    continue
                relative = os.path.relpath(path, str(base)).replace(os.sep, "/")
                with open(path, "rb") as handle:
                    tree["_kernel/%s/%s" % (target, relative)] = handle.read()
    inner = {name[len("_kernel/"):]: data for name, data in tree.items()}
    lines = ["%s  %s\n" % (hashlib.sha256(inner[name]).hexdigest(), name) for name in sorted(inner)]
    tree["_kernel/" + MANIFEST] = "".join(lines).encode("utf-8")
    with open(str(root.joinpath(*LAUNCHER)), "rb") as handle:
        tree["qa.py"] = handle.read()
    return tree


def vendored_tree(scripts: Path) -> Dict[str, bytes]:
    """What ``scripts/`` holds of the vendored files: ``qa.py`` and everything under ``_kernel/``."""
    found: Dict[str, bytes] = {}
    launcher = scripts / "qa.py"
    if launcher.exists():  # unsafe() has already refused a link or a non-file here
        found["qa.py"] = launcher.read_bytes()
    kernel = scripts / "_kernel"
    for here, directories, names in os.walk(str(kernel), followlinks=False):
        directories[:] = sorted(d for d in directories if d != "__pycache__")
        for name in sorted(names):
            path = os.path.join(here, name)
            if name.endswith(".pyc"):
                continue
            relative = "_kernel/" + os.path.relpath(path, str(kernel)).replace(os.sep, "/")
            found[relative] = Path(path).read_bytes() if _regular(path) else b"<not a regular file>"
    return found


def skill_directories(root: Path) -> List[Path]:
    skills = root / "skills"
    if not skills.is_dir():
        return []
    return sorted(p for p in skills.iterdir() if p.is_dir() and not p.is_symlink())


def unsafe(skill: Path) -> List[str]:
    """Why ``skill`` cannot be vendored or checked: a link or non-directory where ``scripts``, ``_kernel`` or ``qa.py`` goes.

    Nothing is resolved or followed: a link is refused whatever it points at, so no write or delete can leave the skill.
    """
    found: List[str] = []
    scripts = skill / "scripts"
    for path, want_directory in ((scripts, True), (scripts / "_kernel", True), (scripts / "qa.py", False)):
        try:
            mode = os.lstat(str(path)).st_mode
        except FileNotFoundError:
            if path == scripts:
                return found  # nothing below it can exist
            continue
        if stat.S_ISLNK(mode):
            found.append("symlink: %s is a symlink" % path.relative_to(skill).as_posix())
        elif stat.S_ISDIR(mode) != want_directory or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            found.append("unsafe: %s is not a %s" % (path.relative_to(skill).as_posix(), "directory" if want_directory else "regular file"))
        if path == scripts and found:
            return found
    return found


def differences(expected: Dict[str, bytes], actual: Dict[str, bytes]) -> List[str]:
    found = ["missing: " + name for name in sorted(set(expected) - set(actual))]
    found += ["extra: " + name for name in sorted(set(actual) - set(expected))]
    found += ["differs: " + name for name in sorted(set(expected) & set(actual)) if expected[name] != actual[name]]
    return found


def write(scripts: Path, expected: Dict[str, bytes]) -> None:
    kernel = scripts / "_kernel"
    if kernel.exists():  # unsafe() has already refused a link here
        shutil.rmtree(str(kernel))
    launcher = scripts / "qa.py"
    for name, data in expected.items():
        target = scripts / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    launcher.chmod(0o644)  # it is run as ``python3 qa.py``, never as an executable


def main(argv: List[str]) -> int:
    check = "--check" in argv
    rest = [a for a in argv if a != "--check"]
    root: Optional[Path] = Path(".")
    if rest[:1] == ["--root"] and len(rest) == 2:
        root = Path(rest[1])
    elif rest:
        print("usage: vendor_kernel.py [--check] [--root <repository>]")
        return 2
    if not root.joinpath(*LAUNCHER).is_file():
        print("vendor_kernel.py: not a repository root: %s" % root)
        return 2
    skills = skill_directories(root)
    if not skills:
        print("0 skill(s) found under skills/: nothing to %s" % ("check" if check else "vendor"))
        return 0
    refused = [(skill, reason) for skill in skills for reason in unsafe(skill)]
    if refused and not check:  # all refusals come before the first write or delete
        for skill, reason in refused:
            print("%s: %s" % (skill.relative_to(root).as_posix() + "/scripts", reason))
        print("refused: nothing was written")
        return 1
    expected = source_tree(root)
    problems = 0
    for skill in skills:
        scripts = skill / "scripts"
        if check:
            for reason in unsafe(skill):
                print("%s: %s" % (scripts.relative_to(root).as_posix(), reason))
                problems += 1
            if unsafe(skill):
                continue
            for line in differences(expected, vendored_tree(scripts)):
                print("%s: %s" % (scripts.relative_to(root).as_posix(), line))
                problems += 1
        else:
            write(scripts, expected)
            print("vendored %s" % skill.relative_to(root).as_posix())
    if check:
        print("%d skill(s) checked, %d difference(s)" % (len(skills), problems))
        return 1 if problems else 0
    print("%d skill(s) vendored" % len(skills))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
