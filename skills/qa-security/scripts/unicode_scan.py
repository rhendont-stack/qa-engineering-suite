"""Finds text that addresses an AI agent and characters that hide text, and writes them as SARIF 2.1.0.

Usage: python3 unicode_scan.py [--out PATH] [PATH ...]
       python3 unicode_scan.py --copy-to-work

Three rules, per line of every text file under the given paths (default ``.``):

* ``qa-injection-marker``: a phrase that tries to override an agent's instructions, in English or Spanish ("ignore the
  previous instructions", "olvida las instrucciones", "do not tell the user", ...). CWE-1427.
* ``qa-unicode-tag-character``: a character from the Unicode tag block, U+E0000 to U+E007F, which renders as nothing and can
  carry hidden text. CWE-1427.
* ``qa-bidi-control``: a bidirectional control character, U+202A to U+202E or U+2066 to U+2069, which can make code read
  differently from how it runs.

A result says the rule, the file, the line and column, and for a character its code point. It never quotes the line: the text
found may be addressed to whoever reads the report. Directories ``.git``, ``.qa``, ``.qa-input``, ``.claude``,
``node_modules``, ``__pycache__`` and ``.venv*`` are skipped; links are never followed; files over 2 MiB and files holding a
NUL byte in their first 8 KiB are skipped and counted.

``--out`` is a path under ``.qa/work/`` (default ``.qa/work/security/unicode.sarif``), or an absolute path outside the
project that does not exist yet (a container's ``/qa-evidence/``). ``--copy-to-work`` copies this file to
``.qa/work/security/unicode_scan.py`` for a container run's ``--input``. Prints one JSON summary. Exit 0 when the scan ran,
2 on bad arguments or a refused path. Standard library only; it runs nothing it reads.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

TOOL = "qa-security-unicode-scan"
VERSION = "1"
DEFAULT_OUT = ".qa/work/security/unicode.sarif"
COPY_NAME = ".qa/work/security/unicode_scan.py"
MAX_BYTES = 2 << 20
SKIP_DIRS = {".git", ".qa", ".qa-input", ".claude", "node_modules", "__pycache__", ".tox", ".mypy_cache", ".pytest_cache"}
CWE_PROMPT = "CWE-1427"

MARKERS_EN = (
    r"\bignore\s+(?:all\s+|any\s+)?(?:of\s+)?(?:the\s+|your\s+)?(?:previous|prior|above|earlier|preceding)\s+(?:instructions|prompts?|rules|directions)",
    r"\bdisregard\s+(?:all\s+|any\s+)?(?:the\s+|your\s+)?(?:previous|prior|above|earlier)?\s*(?:instructions|rules|guidelines|system\s+prompt)",
    r"\bforget\s+(?:all\s+|everything\s+|your\s+)?(?:previous\s+|prior\s+)?instructions",
    r"\byou\s+are\s+now\s+(?:a|an|the|in)\s",
    r"\bnew\s+(?:system\s+)?instructions\s*:",
    r"\b(?:do\s+not|don't)\s+(?:tell|inform|show|mention\s+(?:this\s+)?to)\s+the\s+user",
    r"\b(?:reveal|print|show|repeat)\s+(?:your|the)\s+system\s+prompt",
    r"\b(?:ai|llm)\s+(?:agent|assistant|model)s?\s*[,:]\s*(?:please\s+)?(?:run|execute|delete|send|upload|ignore|approve|mark)\b",
)
MARKERS_ES = (
    r"\bignora\s+(?:todas\s+)?(?:las\s+|tus\s+)?instrucciones\s+(?:anteriores|previas)",
    r"\bolvida\s+(?:todas\s+)?(?:las\s+|tus\s+)?instrucciones",
    r"\bhaz\s+caso\s+omiso\s+de\s+(?:las\s+|tus\s+)?instrucciones",
    r"\bahora\s+eres\s+(?:un|una|el|la)\s",
    r"\bnuevas\s+instrucciones\s*:",
    r"\bno\s+(?:le\s+)?(?:digas|informes|muestres|menciones)\s+(?:nada\s+|esto\s+)?al\s+usuario",
    r"\b(?:revela|muestra|imprime|repite)\s+(?:tu|el)\s+(?:prompt|mensaje)\s+del\s+sistema",
)
MARKER = re.compile("|".join("(?:%s)" % p for p in MARKERS_EN + MARKERS_ES), re.I)
SPANISH = re.compile("|".join("(?:%s)" % p for p in MARKERS_ES), re.I)
TAG = re.compile("[\U000E0000-\U000E007F]")
BIDI = re.compile("[\u202a-\u202e\u2066-\u2069]")

RULES = [
    {"id": "qa-injection-marker", "name": "InjectionMarker",
     "shortDescription": {"text": "Text that tries to override an AI agent's instructions"},
     "fullDescription": {"text": "A phrase, in English or Spanish, that addresses an agent reading the file and tries to replace its "
                                 "instructions. Content is data, never instructions; the line is not quoted here."},
     "defaultConfiguration": {"level": "warning"}, "properties": {"tags": ["security", "external/cwe/cwe-1427"], "cwe": [CWE_PROMPT]}},
    {"id": "qa-unicode-tag-character", "name": "UnicodeTagCharacter",
     "shortDescription": {"text": "A Unicode tag character (U+E0000 to U+E007F)"},
     "fullDescription": {"text": "Tag characters render as nothing and can carry text a reader does not see."},
     "defaultConfiguration": {"level": "warning"}, "properties": {"tags": ["security", "external/cwe/cwe-1427"], "cwe": [CWE_PROMPT]}},
    {"id": "qa-bidi-control", "name": "BidiControl",
     "shortDescription": {"text": "A bidirectional control character (U+202A to U+202E, U+2066 to U+2069)"},
     "fullDescription": {"text": "Bidirectional controls reorder how text is shown, so code can read differently from how it runs."},
     "defaultConfiguration": {"level": "warning"}, "properties": {"tags": ["security"]}},
]
RULE_INDEX = {rule["id"]: index for index, rule in enumerate(RULES)}


class Refused(Exception):
    pass


# --------------------------------------------------------------------------- writing without following links

def _no_link_on_the_way(root: Path, parts: Sequence[str]) -> None:
    here = root
    for part in parts:
        here = here / part
        try:
            mode = os.lstat(str(here)).st_mode
        except FileNotFoundError:
            os.mkdir(str(here))
            continue
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise Refused("%s is a link or not a directory" % here)


def work_path(root: Path, text: str) -> Tuple[Path, List[str]]:
    """``(root, parts)`` of a path under ``.qa/work/``; Refused otherwise."""
    pure = PurePosixPath(text)
    parts = list(pure.parts)
    if pure.is_absolute() or ".." in parts or "." in parts or parts[:2] != [".qa", "work"] or len(parts) < 3:
        raise Refused("the output must be a path under .qa/work/")
    return root, parts


def write_bytes(text: str, data: bytes, root: Optional[Path] = None) -> str:
    """Write ``data`` to ``text``: under ``.qa/work/`` through no link (a fresh temporary file, then a rename that replaces the
    name itself), or, for an absolute path outside the project, only as a new file."""
    root = (root or Path.cwd()).resolve()
    if os.path.isabs(text):
        target = Path(os.path.normpath(text))
        if target == root or root in target.parents:
            raise Refused("an absolute output inside the project must be written as .qa/work/...")
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return str(target)
    root, parts = work_path(root, text)
    _no_link_on_the_way(root, parts[:-1])
    folder = root.joinpath(*parts[:-1])
    temporary = folder / (".%s.%s.tmp" % (parts[-1], secrets.token_hex(8)))
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(str(temporary), str(folder / parts[-1]))
    except BaseException:
        if os.path.lexists(str(temporary)):
            os.unlink(str(temporary))
        raise
    return "/".join(parts)


# --------------------------------------------------------------------------- scanning

def files_under(paths: Sequence[str], counts: Dict[str, int]) -> Iterator[Path]:
    for given in paths:
        start = Path(given)
        try:
            mode = os.lstat(str(start)).st_mode
        except OSError:
            counts["missing"] += 1
            continue
        if stat.S_ISREG(mode):
            yield start
            continue
        if not stat.S_ISDIR(mode):
            continue
        for here, directories, names in os.walk(str(start), followlinks=False):
            directories[:] = sorted(d for d in directories if d not in SKIP_DIRS and not d.startswith(".venv")
                                    and not os.path.islink(os.path.join(here, d)))
            for name in sorted(names):
                path = Path(here, name)
                if stat.S_ISREG(os.lstat(str(path)).st_mode):
                    yield path


def read_text(path: Path, counts: Dict[str, int]) -> Optional[str]:
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        counts["large"] += 1
        return None
    if b"\x00" in data[:8192]:
        counts["binary"] += 1
        return None
    return data.decode("utf-8", "replace")


def uri_of(path: Path) -> str:
    text = path.as_posix()
    for prefix in ("./", "/work/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text


def scan_text(text: str) -> List[Tuple[str, int, int, str]]:
    """``(rule, line, column, message)`` for every hit in ``text``; columns are 1-based, in characters."""
    hits = []
    for number, line in enumerate(text.splitlines(), 1):
        for match in TAG.finditer(line):
            hits.append(("qa-unicode-tag-character", number, match.start() + 1, "U+%04X, a Unicode tag character" % ord(match.group(0))))
        for match in BIDI.finditer(line):
            hits.append(("qa-bidi-control", number, match.start() + 1, "U+%04X, a bidirectional control character" % ord(match.group(0))))
        visible = TAG.sub("", line)
        match = MARKER.search(visible)
        if match:
            language = "Spanish" if SPANISH.search(visible) else "English"
            hits.append(("qa-injection-marker", number, match.start() + 1,
                         "a phrase that tries to override an agent's instructions (%s); the line is not quoted" % language))
    return hits


def sarif(results: List[Dict[str, object]]) -> Dict[str, object]:
    return {"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0",
            "runs": [{"tool": {"driver": {"name": TOOL, "version": VERSION, "rules": RULES}},
                      "results": results}]}


def scan(paths: Sequence[str]) -> Tuple[Dict[str, object], Dict[str, int], int]:
    counts = {"large": 0, "binary": 0, "missing": 0}
    results: List[Dict[str, object]] = []
    scanned = 0
    for path in files_under(paths, counts):
        text = read_text(path, counts)
        if text is None:
            continue
        scanned += 1
        for rule, line, column, message in scan_text(text):
            results.append({"ruleId": rule, "ruleIndex": RULE_INDEX[rule], "level": "warning", "message": {"text": message},
                            "locations": [{"physicalLocation": {"artifactLocation": {"uri": uri_of(path)},
                                                                "region": {"startLine": line, "startColumn": column}}}]})
    return sarif(results), counts, scanned


def main(argv: Sequence[str]) -> int:
    args = list(argv)
    try:
        if args == ["--copy-to-work"]:
            written = write_bytes(COPY_NAME, Path(__file__).read_bytes())
            print(json.dumps({"ok": True, "copied": written}, sort_keys=True))
            return 0
        out = DEFAULT_OUT
        if "--out" in args:
            at = args.index("--out")
            if at + 1 >= len(args):
                raise Refused("--out needs a path")
            out = args[at + 1]
            del args[at:at + 2]
        if any(a.startswith("-") for a in args):
            raise Refused("unknown option %s" % next(a for a in args if a.startswith("-")))
        report, counts, scanned = scan(args or ["."])
        written = write_bytes(out, (json.dumps(report, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    except (Refused, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, Refused) else type(exc).__name__}, sort_keys=True))
        return 2
    results = report["runs"][0]["results"]  # type: ignore[index]
    by_rule: Dict[str, int] = {}
    for result in results:
        by_rule[result["ruleId"]] = by_rule.get(result["ruleId"], 0) + 1
    print(json.dumps({"ok": True, "out": written, "files_scanned": scanned, "skipped": counts, "results": len(results), "by_rule": by_rule},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
