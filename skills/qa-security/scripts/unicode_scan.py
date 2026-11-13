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
found may be addressed to whoever reads the report. Lines are numbered on ``\n``, ``\r\n`` and ``\r`` only, as an editor
numbers them. Directories ``.git``, ``.qa``, ``.qa-input``, ``.claude``, ``node_modules``, ``__pycache__`` and ``.venv*`` are
skipped; links are never followed.

How a file is read. The invariant: no file is counted as scanned while a marker phrase in its single-byte text goes unread,
whether or not NUL bytes stand in or around the phrase. A file is counted as scanned only when at least one reading covers its
whole content in a form that shows the markers. Two readings qualify:

* the single-byte reading: the bytes after any byte-order mark (UTF-32's, UTF-16's or UTF-8's), decoded as UTF-8 with an
  undecodable byte becoming U+FFFD. It qualifies when those bytes hold no NUL;
* the wide reading: UTF-32 when the file starts with a UTF-32 mark (checked first, since it starts with UTF-16's); else
  UTF-16 when it starts with a UTF-16 mark, or when it has neither and NUL bytes stand at nine in ten of the odd (or even)
  positions of its first 8 KiB (that byte order). It qualifies when it decodes strictly and holds no U+0000.

Every qualifying reading is scanned. When the single-byte reading holds a NUL and the file is counted (on its wide reading),
two more views of the single-byte text are scanned, neither of which counts: that reading as it is, and that reading with its
NUL bytes removed (``nul-stripped``), which is how a viewer that ignores NUL bytes shows it. When a NUL byte stands between two
characters that are neither white space nor NUL, as between two words, that reading is also scanned with each NUL byte read
as a space, under the same name and counting neither, since removing such a NUL joins the words it separates. So a NUL byte,
stray, inside a phrase or between its words, cannot hide single-byte text behind a wide reading: a qualifying wide reading
is never the only reading of a file whose single-byte reading holds text. A file whose single-byte reading holds a NUL and
whose wide reading does not qualify is skipped. A hit that more than one reading finds at the same line, column and rule is reported once. No reading is dropped: a
mark before single-byte text makes a wide reading of characters that are not in the bytes as written (a mark before ASCII
text can read as a bidirectional control), but a tool that honours the mark shows that reading. So whenever a file is
scanned in more than one reading, every result names the readings that found it, as ``properties.reading`` and at the end
of its message (``reading: utf-8``, ``reading: utf-16-le``, ``reading: nul-stripped``, ...), and a hit that only a wide or
a NUL-stripped reading finds is seen as such. Known limit: UTF-16 text with no mark and no such NUL pattern (only non-Latin
characters) is read as single-byte text only.

Skipped and counted in the summary's ``skipped``, never as scanned: files over 2 MiB (``large``), files with no qualifying
reading that have a UTF-16 or UTF-32 mark or the NUL pattern (``utf16``) or that hold a NUL byte (``binary``), files that
vanished or could not be opened during the walk (``vanished``), and given paths that do not exist (``missing``). Each skip is
also named, with its path written as in a result's location: in the summary's ``skipped_files`` (``{"path", "reason"}``, the
first 50, with ``skipped_files_not_listed`` counting any more), and every one as a ``note`` in the SARIF run's
``invocations[0].toolExecutionNotifications``, located at that path. A path is written relative to the current directory
when it lies under it, with a leading ``./`` or ``/work/`` (a container run's project) removed.

``--out`` is a path under ``.qa/work/security/`` (default ``.qa/work/security/unicode.sarif``), or, inside a container run,
a new file directly in ``/qa-evidence/``. ``--copy-to-work`` copies this file to ``.qa/work/security/unicode_scan.py`` for a
container run's ``--input``. Prints one JSON summary. Exit 0 when the scan ran, 2 on bad arguments or a refused path.
Standard library only; it runs nothing it reads.
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
EVIDENCE_DIR = "/qa-evidence"          # a container run's evidence folder; the only absolute output
LINE_BREAK = re.compile(r"\r\n|\r|\n")

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
    """``(root, parts)`` of a path under ``.qa/work/security/``; Refused otherwise."""
    pure = PurePosixPath(text)
    parts = list(pure.parts)
    if pure.is_absolute() or ".." in parts or "." in parts or parts[:3] != [".qa", "work", "security"] or len(parts) < 4:
        raise Refused("the output must be a path under .qa/work/security/")
    return root, parts


def evidence_file(text: str) -> Path:
    """A new file directly in the evidence folder (``/qa-evidence/<name>``), with no link on the way; Refused otherwise."""
    pure = PurePosixPath(text)
    if str(pure.parent) != EVIDENCE_DIR or pure.name in ("", ".", "..") or text != str(pure):
        raise Refused("an absolute output must be a new file directly in %s/ (a container run's evidence)" % EVIDENCE_DIR)
    try:
        mode = os.lstat(EVIDENCE_DIR).st_mode
    except OSError:
        raise Refused("%s does not exist here: an absolute output is only for a container run" % EVIDENCE_DIR) from None
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise Refused("%s is a link or not a directory" % EVIDENCE_DIR)
    return Path(EVIDENCE_DIR) / pure.name


def write_bytes(text: str, data: bytes, root: Optional[Path] = None) -> str:
    """Write ``data`` to ``text``: under ``.qa/work/security/`` through no link (a fresh temporary file, then a rename that
    replaces the name itself), or as a new file directly in ``/qa-evidence/`` (a container run)."""
    root = (root or Path.cwd()).resolve()
    if os.path.isabs(text):
        target = evidence_file(text)
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

def skip(counts: Dict[str, int], names: Optional[List[Tuple[str, str]]], reason: str, path: Path) -> None:
    """Count a skipped file under ``reason`` and, when ``names`` is given, name it there as ``(path, reason)``."""
    counts[reason] += 1
    if names is not None:
        names.append((uri_of(path), reason))


def files_under(paths: Sequence[str], counts: Dict[str, int], names: Optional[List[Tuple[str, str]]] = None) -> Iterator[Path]:
    for given in paths:
        start = Path(given)
        try:
            mode = os.lstat(str(start)).st_mode
        except OSError:
            skip(counts, names, "missing", start)
            continue
        if stat.S_ISREG(mode):
            yield start
            continue
        if not stat.S_ISDIR(mode):
            continue
        for here, directories, listed_files in os.walk(str(start), followlinks=False):
            directories[:] = sorted(d for d in directories if d not in SKIP_DIRS and not d.startswith(".venv")
                                    and not os.path.islink(os.path.join(here, d)))
            for name in sorted(listed_files):
                path = Path(here, name)
                try:
                    mode = os.lstat(str(path)).st_mode
                except OSError:                   # gone since the directory was listed
                    skip(counts, names, "vanished", path)
                    continue
                if stat.S_ISREG(mode):
                    yield path


# Byte-order marks and the wide encoding each gives, UTF-32's first: they start with UTF-16's. UTF-8's gives none.
BYTE_ORDER_MARKS = ((b"\xff\xfe\x00\x00", "utf-32-le"), (b"\x00\x00\xfe\xff", "utf-32-be"),
                    (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"), (b"\xef\xbb\xbf", None))
SINGLE = "utf-8"
NUL_STRIPPED = "nul-stripped"
NUL_BETWEEN = re.compile(rb"[^\x00\s]\x00+[^\x00\s]")   # NUL bytes between two characters that are neither space nor NUL
SKIP_REASONS = {
    "large": "over 2 MiB",
    "utf16": "a UTF-16 or UTF-32 byte-order mark or the UTF-16 NUL pattern, and no reading that qualifies",
    "binary": "a NUL byte, and no reading that qualifies",
    "vanished": "vanished or could not be opened during the walk",
    "missing": "a given path that does not exist",
}
SUMMARY_FILES = 50                         # skipped files named in the summary; the SARIF names every one


def mark_of(data: bytes) -> Tuple[int, Optional[str]]:
    """``(length, wide encoding)`` of the byte-order mark ``data`` starts with; ``(0, None)`` with none."""
    for mark, encoding in BYTE_ORDER_MARKS:
        if data.startswith(mark):
            return len(mark), encoding
    return 0, None


def utf16_order(head: bytes) -> Optional[str]:
    """``utf-16-le`` (or ``utf-16-be``) when NUL bytes stand at nine in ten of the odd (or the even) positions of ``head`` and at
    fewer than half of the others, as in mostly Latin UTF-16 text with no byte-order mark; else None."""
    if len(head) < 4:
        return None
    odd, even = head[1::2], head[0::2]
    if odd.count(0) >= 0.9 * len(odd) and even.count(0) < 0.5 * len(even):
        return "utf-16-le"
    if even.count(0) >= 0.9 * len(even) and odd.count(0) < 0.5 * len(odd):
        return "utf-16-be"
    return None


def readings(data: bytes) -> Tuple[List[Tuple[str, str]], str]:
    """``(readings, skip)``: the readings of a file to scan, as ``(name, text)`` with the single-byte reading first (the
    module's docstring), or no reading and the ``skipped`` count the file goes to when none qualifies."""
    length, wide = mark_of(data)
    rest = data[length:]
    if wide is None:
        wide = utf16_order(rest[:8192])
    wide_text = None
    if wide is not None:
        try:
            wide_text = rest.decode(wide)                 # strict; a mark was dropped with ``rest``
        except UnicodeDecodeError:
            wide_text = None
        if wide_text is not None and "\x00" in wide_text:
            wide_text = None
    single_counts = b"\x00" not in rest
    if wide_text is None and not single_counts:
        return [], ("utf16" if wide is not None else "binary")
    found = [(SINGLE, rest.decode("utf-8", "replace"))]
    if wide_text is not None:
        found.append((wide, wide_text))
    if not single_counts:                         # counted on its wide reading: the single-byte text as a NUL-blind viewer shows it
        found.append((NUL_STRIPPED, rest.replace(b"\x00", b"").decode("utf-8", "replace")))
        if NUL_BETWEEN.search(rest):              # a NUL between two words joins them once removed: also read it as a space
            found.append((NUL_STRIPPED, rest.replace(b"\x00", b" ").decode("utf-8", "replace")))
    return found, ""


def read_texts(path: Path, counts: Dict[str, int], names: Optional[List[Tuple[str, str]]] = None) -> Optional[List[Tuple[str, str]]]:
    """The readings of ``path`` to scan (``readings``), or None when it is skipped, counted (and named) under its reason."""
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:                               # gone, or replaced by a link, since the walk saw it
        skip(counts, names, "vanished", path)
        return None
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        skip(counts, names, "large", path)
        return None
    found, reason = readings(data)
    if not found:
        skip(counts, names, reason, path)
        return None
    return found


def uri_of(path: Path) -> str:
    """``path`` as a result's location: no leading ``./`` or ``/work/``, and relative to the current directory under it."""
    text = path.as_posix()
    for prefix in ("./", "/work/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    if text.startswith("/"):
        here = Path.cwd()
        if here != Path(here.anchor):
            try:
                text = PurePosixPath(text).relative_to(here.as_posix()).as_posix()
            except ValueError:                    # not under the current directory: kept as it is
                pass
    return text


def scan_text(text: str) -> List[Tuple[str, int, int, str]]:
    """``(rule, line, column, message)`` for every hit in ``text``; columns are 1-based, in characters."""
    hits = []
    for number, line in enumerate(LINE_BREAK.split(text), 1):
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


def merged_hits(texts: Sequence[Tuple[str, str]]) -> List[Tuple[str, int, int, str, str]]:
    """``(rule, line, column, message, reading)`` for every hit of the readings ``texts`` (``(name, text)``), a hit found by more
    than one reading at the same line, column and rule once. ``reading`` names the readings that found it (``utf-8 and
    utf-16-le``) when there is more than one reading, and is empty otherwise."""
    found: Dict[Tuple[str, int, int], Tuple[str, List[str]]] = {}
    order: List[Tuple[str, int, int]] = []
    for name, text in texts:
        for rule, line, column, message in scan_text(text):
            key = (rule, line, column)
            if key not in found:
                found[key] = (message, [])
                order.append(key)
            if name not in found[key][1]:
                found[key][1].append(name)
    labelled = len(texts) > 1
    return [key + (found[key][0], listed(found[key][1]) if labelled else "") for key in order]


def listed(names: Sequence[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    return names[0] if len(names) == 1 else "%s and %s" % (", ".join(names[:-1]), names[-1])


def notification(path: str, reason: str) -> Dict[str, object]:
    """A SARIF ``toolExecutionNotifications`` entry naming a skipped file and why."""
    return {"level": "note", "message": {"text": "not scanned (%s): %s" % (reason, SKIP_REASONS[reason])},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": path}}}], "properties": {"skipped": reason}}


def sarif(results: List[Dict[str, object]], skipped: Sequence[Tuple[str, str]] = ()) -> Dict[str, object]:
    return {"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0",
            "runs": [{"tool": {"driver": {"name": TOOL, "version": VERSION, "rules": RULES}},
                      "invocations": [{"executionSuccessful": True,
                                       "toolExecutionNotifications": [notification(p, r) for p, r in skipped]}],
                      "results": results}]}


def scan(paths: Sequence[str]) -> Tuple[Dict[str, object], Dict[str, int], int, List[Tuple[str, str]]]:
    """``(report, skip counts, files scanned, skipped files as (path, reason))``."""
    counts = {"large": 0, "utf16": 0, "binary": 0, "vanished": 0, "missing": 0}
    names: List[Tuple[str, str]] = []
    results: List[Dict[str, object]] = []
    scanned = 0
    for path in files_under(paths, counts, names):
        texts = read_texts(path, counts, names)
        if texts is None:
            continue
        scanned += 1
        for rule, line, column, message, reading in merged_hits(texts):
            result: Dict[str, object] = {
                "ruleId": rule, "ruleIndex": RULE_INDEX[rule], "level": "warning",
                "message": {"text": message + ("; reading: " + reading if reading else "")},
                "locations": [{"physicalLocation": {"artifactLocation": {"uri": uri_of(path)},
                                                    "region": {"startLine": line, "startColumn": column}}}]}
            if reading:
                result["properties"] = {"reading": reading}
            results.append(result)
    return sarif(results, names), counts, scanned, names


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
        report, counts, scanned, skipped = scan(args or ["."])
        written = write_bytes(out, (json.dumps(report, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    except (Refused, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, Refused) else type(exc).__name__}, sort_keys=True))
        return 2
    results = report["runs"][0]["results"]  # type: ignore[index]
    by_rule: Dict[str, int] = {}
    for result in results:
        by_rule[result["ruleId"]] = by_rule.get(result["ruleId"], 0) + 1
    summary: Dict[str, object] = {"ok": True, "out": written, "files_scanned": scanned, "skipped": counts, "results": len(results),
                                  "by_rule": by_rule,
                                  "skipped_files": [{"path": p, "reason": r} for p, r in skipped[:SUMMARY_FILES]]}
    if len(skipped) > SUMMARY_FILES:
        summary["skipped_files_not_listed"] = len(skipped) - SUMMARY_FILES
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
