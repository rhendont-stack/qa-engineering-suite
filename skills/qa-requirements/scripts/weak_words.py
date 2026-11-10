"""Finds weak words in requirement documents: vague, open-ended, escape-clause, weak-modal and quantifier terms, in English and Spanish.

Usage: python3 weak_words.py FILE [FILE ...] [--lang en|es|all]

Prints one JSON object on stdout: ``{"ok": true, "files": [...], "lang": ..., "hits": [...], "counts": {...}}``. Each hit is
``{path, line, column, term, matched, langs, kind}``: ``line`` and ``column`` are 1-based, ``column`` counts characters of the
NFC-normalised line (not bytes), ``term`` is the listed term, ``matched`` the text as it is in the file, ``langs`` the lists that hold
the term. Hits are ordered by file (as given), line and column, so the same input always gives the same bytes.

Matching is case-insensitive and ignores accents (``rapido`` and ``RÁPIDO`` both match ``rápido``), whole words only (``fast`` is not
found in ``breakfast``), within one line (a phrase broken across lines is not found). Where two terms overlap, the one that starts
first wins, and of two that start together the longer one (``as fast as possible`` hides ``fast``). A hit is a prompt for review, not a verdict: ``should`` in a quotation, or ``clear`` in a
defined term, may be fine. Read the line before recording it as a quality issue.

The file is only read. Exit 0 on success (with or without hits); exit 2, with ``{"ok": false, "error": {code, message, path?}}``, on
a usage error (``usage``), a file that is missing or not a regular file (``file_unreadable``), or one that is not UTF-8 (``not_utf8``).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import unicodedata
from typing import Dict, Iterable, List, Optional, Tuple

KINDS = ("vague", "approximation", "open-ended", "escape-clause", "weak-modal", "quantifier")

# kind -> terms. A term is lower case, written with its accents; spaces stand for any run of spaces or tabs.
ENGLISH: Dict[str, Tuple[str, ...]] = {
    "vague": (
        "fast", "quick", "quickly", "rapid", "rapidly", "slow", "easy", "easily", "simple", "simply", "user-friendly", "user friendly",
        "friendly", "intuitive", "intuitively", "efficient", "efficiently", "effective", "flexible", "robust", "scalable", "seamless",
        "seamlessly", "adequate", "adequately", "appropriate", "appropriately", "sufficient", "sufficiently", "reasonable",
        "reasonably", "normally", "usually", "typically", "generally", "often", "frequently", "timely", "minimal", "optimal",
        "optimum", "clear", "clearly", "nice", "state-of-the-art", "modern",
    ),
    "approximation": ("approximately", "roughly", "nearly", "almost"),
    "open-ended": ("etc.", "and so on", "and so forth", "among others", "and/or", "including but not limited to", "tbd", "tbc",
                   "to be determined"),
    "escape-clause": (
        "if possible", "where possible", "when possible", "as fast as possible", "as soon as possible", "as far as possible",
        "to the extent possible", "as needed", "as required", "as appropriate", "if necessary", "where necessary", "when necessary",
        "if needed", "where applicable", "as applicable", "if feasible", "where feasible",
    ),
    "weak-modal": ("should", "might", "could", "possibly", "ideally", "preferably"),
    "quantifier": ("several", "many", "few", "numerous", "various"),
}
SPANISH: Dict[str, Tuple[str, ...]] = {
    "vague": (
        "rápido", "rápida", "rápidos", "rápidas", "rápidamente", "lento", "lenta", "fácil", "fáciles", "fácilmente", "sencillo",
        "sencilla", "sencillamente", "amigable", "amigables", "intuitivo", "intuitiva", "intuitivos", "intuitivas", "eficiente",
        "eficientes", "eficientemente", "eficaz", "flexible", "flexibles", "robusto", "robusta", "escalable", "fluido", "fluida",
        "adecuado", "adecuada", "adecuados", "adecuadas", "adecuadamente", "apropiado", "apropiada", "apropiadamente", "suficiente",
        "suficientemente", "razonable", "razonablemente", "normalmente", "generalmente", "habitualmente", "usualmente",
        "típicamente", "frecuentemente", "a menudo", "oportuno", "oportuna", "oportunamente", "óptimo", "óptima", "claro", "clara",
        "claramente", "moderno", "moderna",
    ),
    "approximation": ("aproximadamente", "alrededor de", "cerca de", "casi"),
    "open-ended": ("etc.", "entre otros", "entre otras", "y/o", "y así sucesivamente", "por definir", "a definir"),
    "escape-clause": (
        "si es posible", "cuando sea posible", "siempre que sea posible", "en lo posible", "en la medida de lo posible",
        "lo antes posible", "lo más rápido posible", "de ser posible", "según sea necesario", "según corresponda", "según proceda",
        "si es necesario", "cuando sea necesario", "si procede", "a la brevedad",
    ),
    "weak-modal": ("debería", "deberían", "podría", "podrían", "posiblemente", "idealmente", "preferiblemente", "preferentemente"),
    "quantifier": ("varios", "varias", "muchos", "muchas", "pocos", "pocas", "numerosos", "numerosas", "diversos", "diversas"),
}
LISTS = {"en": ENGLISH, "es": SPANISH}


class ScanError(Exception):
    def __init__(self, code: str, message: str, path: Optional[str] = None) -> None:
        super().__init__(message)
        self.code, self.message, self.path = code, message, path


def fold(text: str) -> str:
    """Lower case without accents. For NFC text the result has exactly one character per input character."""
    return "".join(unicodedata.normalize("NFD", ch)[0].lower()[:1] or ch for ch in text)


def terms() -> List[Tuple[str, List[str], str]]:
    """Every term once, sorted: ``(term, languages that list it, kind)``. A term listed by both languages keeps one kind."""
    merged: Dict[str, Tuple[List[str], str]] = {}
    for lang in sorted(LISTS):
        for kind, words in LISTS[lang].items():
            for term in words:
                langs, known = merged.get(term, ([], kind))
                if known != kind:
                    raise ValueError("%s is listed as %s and as %s" % (term, known, kind))
                merged[term] = (langs + [lang], kind)
    return [(term, merged[term][0], merged[term][1]) for term in sorted(merged)]


def _pattern(term: str) -> str:
    words = fold(term).split(" ")
    body = r"[ \t]+".join(re.escape(w) for w in words)
    if body.endswith(r"\."):  # "etc." also matches "etc" with no full stop
        return r"(?<!\w)" + body[:-2] + r"(?!\w)\.?"
    return r"(?<!\w)" + body + r"(?!\w)"


_COMPILED: List[Tuple[str, List[str], str, "re.Pattern[str]"]] = [(t, l, k, re.compile(_pattern(t))) for t, l, k in terms()]


def scan_text(text: str, lang: str = "all", path: Optional[str] = None) -> List[dict]:
    """The hits in ``text``, ordered by line and column. ``lang`` is ``en``, ``es`` or ``all``."""
    if lang not in ("en", "es", "all"):
        raise ScanError("usage", "lang is en, es or all")
    hits: List[dict] = []
    for number, raw in enumerate(text.split("\n"), 1):
        line = unicodedata.normalize("NFC", raw.rstrip("\r"))
        folded = fold(line)
        found: List[Tuple[int, int, str, List[str], str]] = []
        for term, langs, kind, regex in _COMPILED:
            if lang != "all" and lang not in langs:
                continue
            for match in regex.finditer(folded):
                found.append((match.start(), match.end(), term, langs, kind))
        found.sort(key=lambda f: (f[0], -(f[1] - f[0]), f[2]))
        end = -1
        for start, stop, term, langs, kind in found:
            if start < end:
                continue  # inside a longer term that starts earlier or at the same place
            end = stop
            hit = {"line": number, "column": start + 1, "term": term, "matched": line[start:stop], "langs": list(langs), "kind": kind}
            if path is not None:
                hit = dict({"path": path}, **hit)
            hits.append(hit)
    return hits


def read_text(path: str) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        raise ScanError("file_unreadable", "the file cannot be opened: %s" % exc.strerror, path)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ScanError("file_unreadable", "not a regular file", path)
        with open(fd, "rb", closefd=False) as handle:
            data = handle.read()
    finally:
        os.close(fd)
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ScanError("not_utf8", "the file is not UTF-8 (byte %d)" % exc.start, path)


def scan_files(paths: Iterable[str], lang: str = "all") -> dict:
    files = list(paths)
    hits: List[dict] = []
    for path in files:
        hits.extend(scan_text(read_text(path), lang, path))
    by_file: Dict[str, int] = {}
    by_kind: Dict[str, int] = {}
    for hit in hits:
        by_file[hit["path"]] = by_file.get(hit["path"], 0) + 1
        by_kind[hit["kind"]] = by_kind.get(hit["kind"], 0) + 1
    return {"ok": True, "files": files, "lang": lang, "hits": hits, "counts": {"total": len(hits), "by_file": by_file, "by_kind": by_kind}}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ScanError("usage", message)


def main(argv: List[str]) -> int:
    parser = _Parser(prog="weak_words.py", description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", metavar="FILE")
    parser.add_argument("--lang", choices=("en", "es", "all"), default="all")
    try:
        args = parser.parse_args(argv)
        if not args.files:
            raise ScanError("usage", "name at least one requirements file")
        out = scan_files(args.files, args.lang)
    except ScanError as exc:
        error = {"code": exc.code, "message": exc.message}
        if exc.path is not None:
            error["path"] = exc.path
        print(json.dumps({"ok": False, "error": error}, sort_keys=True, ensure_ascii=False))
        return 2
    print(json.dumps(out, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
