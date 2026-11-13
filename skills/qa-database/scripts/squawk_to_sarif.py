"""Converts squawk's JSON report into SARIF 2.1.0, so the kernel can ingest it (squawk has no SARIF reporter).

Usage: python3 squawk_to_sarif.py --input .qa/evidence/EXE-.../stdout.log --out .qa/work/database/squawk.sarif [--strip /work/]

Run from the project root. ``--input`` is squawk's ``--reporter json`` output: a JSON list of violations, each with ``file``,
``line`` and ``column`` (0-based), ``line_end``, ``column_end``, ``level``, ``message``, ``help`` and ``rule_name``. It must be
a regular file inside the project, never a symlink. ``--out`` must lie under ``.qa/work/``; it is written to a new temporary
file and moved into place, never through a link. ``--strip`` is the prefix removed from each path (the container's mount point,
``/work/`` by default), so the locations read ``postgres/migrations/0004_x.up.sql``.

Lines and columns become 1-based. ``Error`` maps to ``error``, ``Warning`` to ``warning``, anything else to ``note``. Stdout is
one JSON object: ``ok``, ``out``, ``results``, ``rules``. Exit 0, or 2 on a refusal or an input that is not squawk JSON.
The output is data for ``qa ingest sarif``; nothing in the input is run or followed.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Dict, List

SQUAWK_VERSION = "2.72.0"
LEVELS = {"error": "error", "warning": "warning"}


class Refused(Exception):
    pass


def inside(text: str, root: Path, below: Path) -> Path:
    path = Path(os.path.abspath(os.path.join(str(root), text)))
    if path == below or below not in path.parents:
        raise Refused("%s is not under %s" % (text, below.relative_to(root).as_posix() or "."))
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise Refused("%s is a symlink" % current.relative_to(root).as_posix())
    return path


def read_input(path: Path) -> List[Dict[str, Any]]:
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        text = handle.read().decode("utf-8", errors="replace")
    text = text.strip()
    try:
        data = json.loads(text) if text else []
    except ValueError:
        raise Refused("the input is not JSON: run squawk with --reporter json")
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise Refused("the input is not squawk's list of violations")
    return data


def write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / (".%s.%s.tmp" % (path.name, secrets.token_hex(8)))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(str(temp), str(path))
    finally:
        if temp.exists() and not temp.is_symlink():
            temp.unlink()


def number(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default


def convert(violations: List[Dict[str, Any]], strip: str) -> Dict[str, Any]:
    rules: Dict[str, Dict[str, Any]] = {}
    results = []
    for item in violations:
        rule = str(item.get("rule_name") or "squawk")
        message = str(item.get("message") or rule)
        help_text = item.get("help")
        if rule not in rules:
            rules[rule] = {"id": rule, "shortDescription": {"text": message},
                           "helpUri": "https://squawkhq.com/docs/%s" % rule}
            if help_text:
                rules[rule]["help"] = {"text": str(help_text)}
        uri = str(item.get("file") or "")
        if strip and uri.startswith(strip):
            uri = uri[len(strip):]
        line, column = number(item.get("line")) + 1, number(item.get("column")) + 1
        region = {"startLine": line, "startColumn": column,
                  "endLine": max(line, number(item.get("line_end"), line - 1) + 1),
                  "endColumn": number(item.get("column_end"), column - 1) + 1}
        text = message + (" " + str(help_text) if help_text else "")
        results.append({"ruleId": rule, "level": LEVELS.get(str(item.get("level") or "").lower(), "note"), "message": {"text": text},
                        "locations": [{"physicalLocation": {"artifactLocation": {"uri": uri}, "region": region}}]})
    return {"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0",
            "runs": [{"tool": {"driver": {"name": "squawk", "version": SQUAWK_VERSION, "informationUri": "https://squawkhq.com",
                                          "rules": [rules[k] for k in sorted(rules)]}},
                      "results": results}]}


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--strip", default="/work/")
    args = parser.parse_args(argv)
    root = Path(os.getcwd()).resolve()
    try:
        source = inside(args.input, root, root)
        out = inside(args.out, root, root / ".qa" / "work")
        if not source.is_file():
            raise Refused("no file at %s" % args.input)
        sarif = convert(read_input(source), args.strip)
    except (Refused, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    write_new(out, (json.dumps(sarif, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    run = sarif["runs"][0]
    print(json.dumps({"ok": True, "out": out.relative_to(root).as_posix(), "results": len(run["results"]),
                      "rules": [r["id"] for r in run["tool"]["driver"]["rules"]]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
