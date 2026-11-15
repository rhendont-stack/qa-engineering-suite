"""Converts actionlint's JSON report into SARIF 2.1.0 of category ``pipeline``, so the kernel can ingest it.

Usage: python3 actionlint_to_sarif.py --input .qa/evidence/EXE-.../stdout.log --out .qa/work/release/actionlint.sarif [--strip /work/]

Run from the project root. ``--input`` is what ``actionlint -format '{{json .}}'`` printed: a JSON list of errors, each with
``message``, ``filepath``, ``line``, ``column``, ``end_column``, ``kind`` and ``snippet`` (``null`` or an empty file when it
found nothing). It must be a regular file inside the project, never a link. ``--out`` must lie under ``.qa/work/``; it is
written to a new temporary file by exclusive create and moved into place, never through a link. ``--strip`` is the prefix
removed from each path (the container's mount point, ``/work/`` by default; a leading ``./`` goes too), so the locations
read ``.github/workflows/build.yml``.

Each error becomes one result: ``ruleId`` is actionlint's ``kind`` (``expression``, ``syntax-check``, ``shellcheck`` ...),
level ``error``, with the line and columns as actionlint gives them (1-based). Stdout is one JSON object: ``ok``, ``out``,
``results``, ``rules``. Exit 0, or 2 on a refusal or an input that is not actionlint's JSON. The output is data for
``qa ingest sarif``; nothing in the input is run or followed.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Dict, List

ACTIONLINT_VERSION = "1.7.12"
CATEGORY = "pipeline"
MAX_BYTES = 8 * 1024 * 1024


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
            raise Refused("%s is a link" % current.relative_to(root).as_posix())
    return path


def read_input(path: Path) -> List[Dict[str, Any]]:
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise Refused("the input is over 8 MiB")
    text = data.decode("utf-8", errors="replace").strip()
    try:
        parsed = json.loads(text) if text else []
    except ValueError:
        raise Refused("the input is not JSON: run actionlint with -format '{{json .}}'")
    if parsed is None:
        return []
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise Refused("the input is not actionlint's list of errors")
    return parsed


def write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / (".%s.%s.tmp" % (path.name, secrets.token_hex(8)))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if path.is_symlink():
            raise Refused("%s is a link" % path.name)
        os.replace(str(temp), str(path))
    finally:
        if temp.exists() and not temp.is_symlink():
            temp.unlink()


def number(value: Any, default: int = 1) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else default


def convert(errors: List[Dict[str, Any]], strip: str) -> Dict[str, Any]:
    rules: Dict[str, Dict[str, Any]] = {}
    results = []
    for item in errors:
        rule = str(item.get("kind") or "actionlint")
        message = str(item.get("message") or rule)
        rules.setdefault(rule, {"id": rule, "shortDescription": {"text": "actionlint %s check" % rule},
                                "helpUri": "https://github.com/rhysd/actionlint/blob/v%s/docs/checks.md" % ACTIONLINT_VERSION,
                                "properties": {"category": CATEGORY, "tags": [CATEGORY]}})
        uri = str(item.get("filepath") or "")
        if strip and uri.startswith(strip):
            uri = uri[len(strip):]
        while uri.startswith("./"):
            uri = uri[2:]
        line, column = number(item.get("line")), number(item.get("column"))
        region = {"startLine": line, "startColumn": column, "endColumn": max(column, number(item.get("end_column"), column))}
        result = {"ruleId": rule, "level": "error", "message": {"text": message},
                  "locations": [{"physicalLocation": {"artifactLocation": {"uri": uri}, "region": region}}],
                  "properties": {"category": CATEGORY}}
        if isinstance(item.get("snippet"), str):
            result["locations"][0]["physicalLocation"]["contextRegion"] = {"startLine": line, "snippet": {"text": item["snippet"]}}
        results.append(result)
    return {"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0",
            "runs": [{"tool": {"driver": {"name": "actionlint", "version": ACTIONLINT_VERSION,
                                          "informationUri": "https://github.com/rhysd/actionlint",
                                          "rules": [rules[k] for k in sorted(rules)]}},
                      "results": results, "properties": {"category": CATEGORY}}]}


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
        write_new(out, (json.dumps(sarif, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    except (Refused, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    run = sarif["runs"][0]
    print(json.dumps({"ok": True, "out": out.relative_to(root).as_posix(), "results": len(run["results"]),
                      "rules": [r["id"] for r in run["tool"]["driver"]["rules"]]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
