"""Writes the regression-repo project, with its six-commit history, into a directory given at run time.

Usage: python3 generate.py <empty-or-new-directory> [--checkout N]

The project is a small orders service. Its history is made here, from the files under ``templates/``, and never kept in
the repository this file belongs to: each ``templates/cN`` directory holds the files that change in commit ``N``, with a
``.tmpl`` suffix that is dropped when they are written.

    C0  the service, its passing tests and ``openapi.yaml``
    C1  a feature commit that changes an existing function and weakens one of its tests
    C2  a response field renamed in code, tests and ``openapi.yaml``
    C3  a fix, with the test that shows it
    C4  a second fix, for a tier boundary
    C5  a test configuration change that keeps the suite green

The history is deterministic: one fixed author and committer, fixed dates, no configuration read from the machine. The result is
one branch (``main``) with ``HEAD`` on it, no remote, no tag and nothing else. ``--checkout N`` stops after commit ``N``, so that
later commits are not in the directory at all, not even unreachable.

It refuses a directory inside the git work tree this file belongs to, and a directory that already holds files.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
SUFFIX = ".tmpl"
BRANCH = "main"
AUTHOR = "Orders Team"
EMAIL = "orders-team@example.invalid"
COMMITS = (
    ("Add the orders service: pricing, summaries, OpenAPI description", ""),
    ("Add quantity tiers to order pricing", "Orders of 10 units get 5 percent off and orders of 20 units or more get 10 percent.\n"
                                           "Simplify apply_discount while here."),
    ("Rename payable_cents to amount_due_cents in the order summary", "The name matches what the billing team calls it."),
    ("Fix rounding of the discount on odd totals", "A 10 percent discount on 999 cents must leave 900 payable."),
    ("Fix the top tier for exactly 20 units", ""),
    ("Keep CI green while loyalty points land", ""),
)


def stage_files(index: int) -> Dict[str, bytes]:
    """The files commit ``index`` writes or replaces, by path in the project."""
    root = TEMPLATES / ("c%d" % index)
    found: Dict[str, bytes] = {}
    for path in sorted(root.rglob("*" + SUFFIX)):
        found[path.relative_to(root).as_posix()[: -len(SUFFIX)]] = path.read_bytes()
    return found


def write_stage(directory: Path, index: int) -> None:
    for name, data in stage_files(index).items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _inside_work_tree(directory: Path) -> bool:
    roots = [HERE] + [parent for parent in HERE.parents if (parent / ".git").exists()][:1]
    target = directory.resolve()
    return any(target == root or root in target.parents for root in roots)


def _check_directory(directory: Path) -> None:
    if _inside_work_tree(directory):
        raise SystemExit("refusing to write the project inside the repository's work tree")
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        raise SystemExit("the directory must be new or empty")


def generate(directory: Path) -> List[str]:
    """Write the project as it is at the last commit, with no history, and return the relative paths written, sorted."""
    _check_directory(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(len(COMMITS)):
        write_stage(directory, index)
    return sorted(path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file())


def _environment(index: Optional[int]) -> Dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME=AUTHOR, GIT_AUTHOR_EMAIL=EMAIL,
               GIT_COMMITTER_NAME=AUTHOR, GIT_COMMITTER_EMAIL=EMAIL)
    if index is not None:
        stamp = "2026-03-%02dT10:00:00 +0000" % (2 + index)
        env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    return env


def _git(directory: Path, index: Optional[int], *args: str) -> None:
    subprocess.run(["git", "-C", str(directory), "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"] + list(args), check=True,
                   env=_environment(index), capture_output=True, timeout=120)


def generate_history(directory: Path, checkout: Optional[int] = None) -> List[str]:
    """Write the project and its history; return the commit subjects, oldest first.

    ``checkout`` is the index of the last commit made, so ``HEAD`` is there and nothing later exists."""
    last = len(COMMITS) - 1 if checkout is None else checkout
    if type(last) is not int or not 0 <= last < len(COMMITS):
        raise SystemExit("--checkout is a commit index from 0 to %d" % (len(COMMITS) - 1))
    _check_directory(directory)
    directory.mkdir(parents=True, exist_ok=True)
    _git(directory, None, "init", "-q")
    _git(directory, None, "symbolic-ref", "HEAD", "refs/heads/" + BRANCH)
    for index in range(last + 1):
        write_stage(directory, index)
        _git(directory, index, "add", "-A")
        subject, body = COMMITS[index]
        _git(directory, index, "commit", "-q", "-m", subject, *(["-m", body] if body else []))
    return [subject for subject, _body in COMMITS[: last + 1]]


def main(argv: Sequence[str]) -> int:
    args = list(argv[1:])
    checkout: Optional[int] = None
    if len(args) == 3 and args[1] == "--checkout":
        try:
            checkout = int(args[2])
        except ValueError:
            checkout = -1
        args = args[:1]
    if len(args) != 1:
        print("usage: python3 generate.py <empty-or-new-directory> [--checkout N]", file=sys.stderr)
        return 2
    subjects = generate_history(Path(args[0]), checkout)
    print("%d commits in %s" % (len(subjects), Path(args[0]).resolve()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
