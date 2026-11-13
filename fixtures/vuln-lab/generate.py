"""Writes the catalogue lab into a directory given at run time.

Usage: python3 generate.py <empty-or-new-directory>

It copies ``lab/``, ``README.md`` and ``requirements.txt``, then writes ``lab/settings.py``, whose signing key is a credential
made at this moment: the format of a Stripe test-mode key, with a body that spells two words saying it is not real and a random
tail. It is no key of any account and works nowhere. This file keeps only fragments of it, so nothing in the repository, and nothing a scanner
or an agent reads there, holds one. The directory it fills is where the assembled key exists.

It refuses a directory inside the git work tree this file belongs to, and a directory that already holds files.
"""
from __future__ import annotations

import json
import secrets
import shutil
import string
import sys
from pathlib import Path
from typing import List, Sequence

HERE = Path(__file__).resolve().parent
COPIED = ("README.md", "requirements.txt")
TAIL_LENGTH = 16


def said(*chunks: str) -> str:
    return "".join(chunks)


def credential() -> str:
    """A key in the shape of a Stripe test-mode secret key, made now; its tail never repeats a character more than twice."""
    alphabet = string.ascii_letters + string.digits
    while True:
        tail = "".join(secrets.choice(alphabet) for _ in range(TAIL_LENGTH))
        if max(tail.count(ch) for ch in tail) <= 2:
            break
    return said("s", "k", "_", "te", "st", "_", "FA", "KE", "EXAM", "PLE", tail)


def settings_text(key: str) -> str:
    return said(
        '"""Settings of the catalogue lab."""\n\n',
        "DEBUG = True\n",
        'SECRET_KEY = "', key, '"\n',
        'SESSION_COOKIE = "lab_session"\n',
    )


def _inside_work_tree(directory: Path) -> bool:
    """Whether ``directory`` is in this file's own directory or in the git work tree around it."""
    roots = [HERE] + [parent for parent in HERE.parents if (parent / ".git").exists()][:1]
    target = directory.resolve()
    return any(target == root or root in target.parents for root in roots)


def generate(directory: Path) -> List[str]:
    """Write the lab under ``directory`` and return the relative paths written, sorted."""
    if _inside_work_tree(directory):
        raise SystemExit("refusing to write the lab inside the repository's work tree")
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        raise SystemExit("the directory must be new or empty")
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copytree(str(HERE / "lab"), str(directory / "lab"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in COPIED:
        shutil.copyfile(str(HERE / name), str(directory / name))
    (directory / "lab" / "settings.py").write_bytes(settings_text(credential()).encode("utf-8"))
    return sorted(path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file())


def main(argv: Sequence[str]) -> int:
    if len(argv) != 2:
        print("usage: python3 generate.py <empty-or-new-directory>", file=sys.stderr)
        return 2
    print(json.dumps({"directory": str(Path(argv[1]).resolve()), "files": generate(Path(argv[1]))}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
