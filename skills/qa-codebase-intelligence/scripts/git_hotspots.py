"""Ranks the files of a repository by churn times complexity.

Usage: python3 git_hotspots.py [--root DIR] [--top N] [--max-commits N]

Churn is read from ``git log --numstat`` (commits that touched the file, and lines added plus deleted); complexity is a
count of decision points in the file's current text (``if``, ``for``, ``while``, ``case``, ``catch``, boolean operators,
ternaries) plus one. It is a rough, language-independent metric, not a replacement for a real tool such as lizard; the
output says which one produced the number. ``score`` is ``commits * complexity``. Lock files, binary files and files that no
longer exist are left out.

Prints one JSON object: ``{"commit", "commits_read", "metric", "hotspots": [{"path", "commits", "lines_changed", "loc",
"complexity", "score"}], "limitations": [...]}``. It only reads; exit 0 unless the arguments are wrong (2).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import _repo  # noqa: E402

DECISION = re.compile(r"\b(?:if|elif|else\s+if|for|foreach|while|case|catch|except|when)\b|&&|\|\||\band\b|\bor\b|\?(?=[^?.:])")
CODE_SUFFIXES = tuple(_repo.LANGUAGES)
METRIC = "decision-point count (kernel fallback metric; lizard was not used)"


def complexity(text: str) -> int:
    return 1 + len(DECISION.findall(text))


def parse_numstat(log: str) -> Dict[str, Dict[str, int]]:
    """``{path: {"commits", "lines_changed"}}`` from ``git log --numstat --format=@@%H``. Binary entries (``-``) are skipped."""
    churn: Dict[str, Dict[str, int]] = {}
    seen: set = set()
    commit = None
    for line in log.splitlines():
        if line.startswith("@@"):
            commit, seen = line[2:], set()
            continue
        parts = line.split("\t")
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit() or commit is None:
            continue
        path = parts[2]
        entry = churn.setdefault(path, {"commits": 0, "lines_changed": 0})
        if path not in seen:
            entry["commits"] += 1
            seen.add(path)
        entry["lines_changed"] += int(parts[0]) + int(parts[1])
    return churn


def hotspots(root: Path, top: int = 20, max_commits: int = 2000) -> Dict[str, Any]:
    root = Path(root)
    limitations: List[str] = []
    commit = _repo.head(root)
    result: Dict[str, Any] = {"commit": commit, "commits_read": 0, "metric": METRIC, "hotspots": [], "limitations": limitations}
    if commit is None:
        limitations.append("not a git repository with a commit: no churn data, so no hotspot ranking")
        return result
    log = _repo.git(root, ["log", "--no-merges", "--no-renames", "--no-ext-diff", "--no-textconv", "--no-show-signature", "--numstat", "--format=@@%H", "-n", str(max_commits)])
    if log is None:
        limitations.append("git log failed: no churn data")
        return result
    result["commits_read"] = sum(1 for line in log.splitlines() if line.startswith("@@"))
    if result["commits_read"] < 2:
        limitations.append("the history has %d commit(s): churn cannot tell files apart, so the ranking reflects complexity only"
                           % result["commits_read"])
    if result["commits_read"] >= max_commits:
        limitations.append("history truncated at %d commits" % max_commits)
    ranked = []
    for path, entry in parse_numstat(log).items():
        name = path.rsplit("/", 1)[-1]
        if name in _repo.LOCKFILES or not path.endswith(CODE_SUFFIXES) or _repo.excluded(path):
            continue
        text = _repo.read_text(root, path)
        if text is None:
            continue
        points = complexity(text)
        ranked.append({"path": path, "commits": entry["commits"], "lines_changed": entry["lines_changed"],
                       "loc": sum(1 for line in text.splitlines() if line.strip()), "complexity": points,
                       "score": entry["commits"] * points})
    ranked.sort(key=lambda h: (-h["score"], -h["lines_changed"], h["path"]))
    result["hotspots"] = ranked[:top]
    return result


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--max-commits", type=int, default=2000)
    args = parser.parse_args(argv)
    if args.top < 1 or args.max_commits < 1 or not os.path.isdir(args.root):
        print("error: --top and --max-commits are positive numbers and --root is a directory", file=sys.stderr)
        return 2
    print(json.dumps(hotspots(Path(args.root), args.top, args.max_commits), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
