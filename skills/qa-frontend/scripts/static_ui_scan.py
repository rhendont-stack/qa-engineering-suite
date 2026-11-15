"""Finds UI problems visible in a site's HTML and CSS source, as SARIF results with WCAG references, each one approximate.

Usage: python3 static_ui_scan.py --root DIR [--out PATH]
       python3 static_ui_scan.py --copy-to-work

It reads every ``*.html``, ``*.htm`` and ``*.css`` file under ``DIR`` (no link is followed; dependency, build and tool
directories are skipped) and applies five rules:

* ``img-without-alt``: an ``<img>`` with no ``alt`` attribute and no other accessible name (``aria-label``,
  ``aria-labelledby``, ``title``) that is not hidden (``role="presentation"``/``"none"``, ``aria-hidden="true"``). WCAG 1.1.1.
  ``alt=""`` marks a decorative image and passes.
* ``click-handler-on-non-interactive``: an inline ``onclick`` on an element that is not natively interactive (a link with
  ``href``, ``button``, ``input``, ``select``, ``textarea``, ``summary``, ``option``, an ``area`` with ``href``, media with
  ``controls``), without ``tabindex`` of 0 or more (WCAG 2.1.1, keyboard) or without a ``role`` (WCAG 4.1.2). Handlers added
  from a script are not visible here.
* ``input-without-label``: an ``input`` (not hidden, submit, reset, button or image), ``select`` or ``textarea`` with no
  ``<label for>``, no wrapping ``<label>``, no ``aria-label``, ``aria-labelledby`` or ``title``; a placeholder is not a
  label. WCAG 1.3.1, 3.3.2, 4.1.2.
* ``email-input-not-typed``: an ``input`` whose ``autocomplete`` holds ``email``, or whose ``name`` or ``id`` names an email,
  with a ``type`` other than ``email``: the browser accepts any text there. CWE-20; related to WCAG 3.3.1, which applies only
  once an input error is detected.
* ``fixed-width-over-375``: a CSS ``width`` or ``min-width`` (in a style sheet, a ``<style>`` block or a ``style`` attribute)
  wider than 375 CSS pixels (``px``, ``pt``, ``pc``, ``in``, ``cm``, ``mm``; ``em`` and ``rem`` at 16 px), outside an
  ``@media`` block that needs a minimum width over 375 px or print. ``calc()``, ``min()``, ``max()`` and ``clamp()`` are not
  read. WCAG 1.4.10 at a 375 px phone viewport.

Every result is approximate: it is read from the source, not from a rendered page, so the cascade, scripts and the browser's
defaults are not applied, and a person or a browser check decides it. Each result carries ``properties.approximate: true`` and
its references (``wcag``, ``cwe``, ``wcag_related``). A file that is a link, is over 1 MiB or is not UTF-8 is listed as
``unreadable`` with its reason, and nothing is reported from it.

Paths are relative to the current directory when the file lies under it (run it from the project root, so they are the
project's own paths), absolute otherwise. ``--out PATH`` writes the SARIF log to a new file under ``.qa/work/frontend/`` or,
inside a container run, ``/qa-evidence/``; without it nothing is written. ``--copy-to-work`` writes a copy of this script to
``.qa/work/frontend/static_ui_scan.py`` for a container run's ``--input``. Files are written by exclusive create through no
link. Prints one JSON summary. Exit 0 when the scan ran (results or not), 2 on bad arguments or a refused path. Standard
library only.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Sequence, Tuple

VERSION = "0.1.0"
DRIVER = "qa-frontend-static-ui-scan"
WORK_DIR = ".qa/work/frontend"
EVIDENCE_DIR = "/qa-evidence"
MAX_BYTES = 1024 * 1024
PHONE_WIDTH = 375
SKIP_DIRS = {".git", ".qa", ".qa-input", ".claude", "node_modules", "__pycache__", "vendor", ".venv", "venv", "dist", "build",
             "coverage", ".next", ".nuxt", "target", "bower_components"}
SUFFIXES = {".html": "html", ".htm": "html", ".css": "css"}
RULES = {
    "img-without-alt": {"text": "An image without a text alternative", "wcag": ["1.1.1"], "cwe": [], "wcag_related": []},
    "click-handler-on-non-interactive": {"text": "A click handler on an element that is not interactive", "wcag": ["2.1.1", "4.1.2"],
                                         "cwe": [], "wcag_related": []},
    "input-without-label": {"text": "A form field without a label", "wcag": ["1.3.1", "3.3.2", "4.1.2"], "cwe": [], "wcag_related": []},
    "email-input-not-typed": {"text": "An email field that is not typed email", "wcag": [], "cwe": ["CWE-20"], "wcag_related": ["3.3.1"]},
    "fixed-width-over-375": {"text": "A fixed width wider than a 375 px phone viewport", "wcag": ["1.4.10"], "cwe": [], "wcag_related": []},
}
APPROXIMATE = " (approximate: read from the source, not from a rendered page)"
UNITS = {"px": 1.0, "pt": 96 / 72, "pc": 16.0, "in": 96.0, "cm": 96 / 2.54, "mm": 96 / 25.4, "em": 16.0, "rem": 16.0}
LENGTH = re.compile(r"(?<![\w.-])(\d+(?:\.\d+)?)(px|pt|pc|in|cm|mm|rem|em)\b", re.I)
COMPUTED = re.compile(r"\b(?:calc|min|max|clamp|var)\s*\(", re.I)
MIN_WIDTH_QUERY = re.compile(r"min-width\s*:\s*(\d+(?:\.\d+)?)(px|em|rem)", re.I)
EMAIL_NAME = re.compile(r"(?i)e[-_]?mail")


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
            os.mkdir(str(here), 0o755)
            mode = os.lstat(str(here)).st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise Refused("%s is a link or not a directory" % "/".join(parts))


def write_new(text: str, data: bytes, root: Optional[Path] = None) -> str:
    """Write ``data`` to ``text``: a new file directly in ``/qa-evidence/`` (a container run), or under ``.qa/work/frontend/``
    through no link (a fresh temporary file by exclusive create, then a rename onto the name)."""
    root = (root or Path.cwd()).resolve()
    if os.path.isabs(text):
        pure = PurePosixPath(text)
        if str(pure.parent) != EVIDENCE_DIR or pure.name in ("", ".", ".."):
            raise Refused("an absolute --out must name a file directly in %s/" % EVIDENCE_DIR)
        mode = os.lstat(EVIDENCE_DIR).st_mode if os.path.lexists(EVIDENCE_DIR) else None
        if mode is None or stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise Refused("%s is missing, a link or not a directory" % EVIDENCE_DIR)
        fd = os.open(text, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return text
    parts = PurePosixPath(text).parts
    if parts[:3] != (".qa", "work", "frontend") or len(parts) < 4 or any(p in ("..", ".") for p in parts):
        raise Refused("--out must name a file under %s/ or %s/" % (WORK_DIR, EVIDENCE_DIR))
    _no_link_on_the_way(root, parts[:-1])
    folder = root.joinpath(*parts[:-1])
    temporary = folder / (".%s.%s.tmp" % (parts[-1], secrets.token_hex(8)))
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        target = folder / parts[-1]
        if os.path.islink(str(target)):
            raise Refused("%s is a link" % text)
        os.replace(str(temporary), str(target))
    except BaseException:
        if os.path.lexists(str(temporary)):
            os.unlink(str(temporary))
        raise
    return "/".join(parts)


# --------------------------------------------------------------------------- files

def uri_of(path: Path) -> str:
    """The path as the walk reached it (a link is named, never resolved to its target), relative to the current directory."""
    absolute = Path(os.path.abspath(str(path)))
    try:
        return absolute.relative_to(Path(os.path.abspath(os.getcwd()))).as_posix()
    except ValueError:
        return absolute.as_posix()


def site_files(root: Path, unreadable: List[Dict[str, str]]) -> List[Tuple[Path, str]]:
    """``(path, kind)`` of every HTML and CSS file under ``root``, sorted; links and oversized files go to ``unreadable``."""
    found: List[Tuple[Path, str]] = []
    for here, directories, names in os.walk(str(root)):
        kept = []
        for name in sorted(directories):
            if name in SKIP_DIRS or name.startswith(".venv"):
                continue
            if os.path.islink(os.path.join(here, name)):
                unreadable.append({"path": uri_of(Path(here) / name), "reason": "a link, not followed"})
                continue
            kept.append(name)
        directories[:] = kept
        for name in sorted(names):
            kind = SUFFIXES.get(os.path.splitext(name)[1].lower())
            if kind is None:
                continue
            path = Path(here) / name
            info = os.lstat(str(path))
            if stat.S_ISLNK(info.st_mode):
                unreadable.append({"path": uri_of(path), "reason": "a link, not followed"})
            elif not stat.S_ISREG(info.st_mode):
                unreadable.append({"path": uri_of(path), "reason": "not a regular file"})
            elif info.st_size > MAX_BYTES:
                unreadable.append({"path": uri_of(path), "reason": "over 1 MiB"})
            else:
                found.append((path, kind))
    return sorted(found)


# --------------------------------------------------------------------------- results

def result(rule: str, uri: str, line: int, column: int, message: str) -> Dict[str, Any]:
    info = RULES[rule]
    return {"ruleId": rule, "level": "warning", "message": {"text": message + APPROXIMATE},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": uri}, "region": {"startLine": line, "startColumn": column}}}],
            "properties": {"approximate": True, "wcag": list(info["wcag"]), "cwe": list(info["cwe"]),
                           "wcag_related": list(info["wcag_related"])}}


# --------------------------------------------------------------------------- CSS

class LineMap:
    def __init__(self, text: str, first_line: int = 1, first_column: int = 1):
        self.starts = [0] + [m.end() for m in re.finditer(r"\n", text)]
        self.first_line, self.first_column = first_line, first_column

    def at(self, index: int) -> Tuple[int, int]:
        row = bisect.bisect_right(self.starts, index) - 1
        column = index - self.starts[row] + 1
        return self.first_line + row, (column + self.first_column - 1 if row == 0 else column)


def _blank_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)


def wide_length(value: str) -> Optional[Tuple[str, float]]:
    """The first length of ``value`` wider than a phone viewport, as ``(text, CSS px)``; None for computed values."""
    if COMPUTED.search(value):
        return None
    match = LENGTH.search(value)
    if not match:
        return None
    pixels = float(match.group(1)) * UNITS[match.group(2).lower()]
    return (match.group(0), pixels) if pixels > PHONE_WIDTH else None


def _media_skips(prelude: str) -> bool:
    text = prelude.lower()
    if text.startswith("@media"):
        if "print" in text and "screen" not in text and "all" not in text:
            return True
        for number, unit in MIN_WIDTH_QUERY.findall(text):
            if float(number) * UNITS[unit.lower()] > PHONE_WIDTH:
                return True
    return False


def css_results(text: str, uri: str, first_line: int = 1, first_column: int = 1) -> List[Dict[str, Any]]:
    """``fixed-width-over-375`` results of a style sheet (or a ``<style>`` block starting at ``first_line``)."""
    clean = _blank_comments(text)
    lines = LineMap(clean, first_line, first_column)
    found: List[Dict[str, Any]] = []
    stack: List[Tuple[str, bool]] = []      # (prelude, skipped by a media query on the way)
    start, i, quote = 0, 0, ""
    while i < len(clean):
        ch = clean[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "{":
            prelude = " ".join(clean[start:i].split())
            skipped = (stack[-1][1] if stack else False) or _media_skips(prelude)
            stack.append((prelude, skipped))
            start = i + 1
        elif ch in ";}":
            if stack and not stack[-1][0].startswith("@"):
                found += _declaration(clean, start, i, stack[-1], uri, lines)
            if ch == "}" and stack:
                stack.pop()
            start = i + 1
        i += 1
    return found


def _declaration(text: str, start: int, end: int, block: Tuple[str, bool], uri: str, lines: LineMap) -> List[Dict[str, Any]]:
    body = text[start:end]
    name, colon, value = body.partition(":")
    if not colon or block[1]:
        return []
    prop = name.strip().lower()
    if prop not in ("width", "min-width"):
        return []
    wide = wide_length(value)
    if wide is None:
        return []
    offset = start + (len(name) - len(name.lstrip()))
    line, column = lines.at(offset)
    return [result("fixed-width-over-375", uri, line, column,
                   "%s: %s %s is %d CSS px, wider than a %d px phone viewport, so the page scrolls sideways there"
                   % (block[0] or "a style attribute", prop, wide[0], round(wide[1]), PHONE_WIDTH))]


# --------------------------------------------------------------------------- HTML

INTERACTIVE = {"button", "select", "textarea", "summary", "option"}
UNLABELLED_TYPES = {"hidden", "submit", "reset", "button", "image"}


def _filled(attrs: Dict[str, Optional[str]], *names: str) -> bool:
    return any(isinstance(attrs.get(n), str) and attrs[n].strip() for n in names)


class Page(HTMLParser):
    def __init__(self, uri: str):
        super().__init__(convert_charrefs=True)
        self.uri = uri
        self.results: List[Dict[str, Any]] = []
        self.labels_for: set = set()
        self.label_depth = 0
        self.fields: List[Tuple[str, Dict[str, Optional[str]], int, int, bool]] = []
        self.style_at: Optional[Tuple[int, int]] = None
        self.style_text: List[str] = []

    def handle_starttag(self, tag: str, attr_list: List[Tuple[str, Optional[str]]]) -> None:
        attrs = {k.lower(): v for k, v in attr_list}
        line, column = self.getpos()
        column += 1
        if tag == "label":
            self.label_depth += 1
            if _filled(attrs, "for"):
                self.labels_for.add(attrs["for"].strip())
        if tag == "style":
            self.style_at, self.style_text = (line, column), []
        if isinstance(attrs.get("style"), str):
            self.results += css_results("x{%s}" % attrs["style"], self.uri, line, column)
        if tag == "img":
            hidden = (attrs.get("role") or "").strip().lower() in ("presentation", "none") or (attrs.get("aria-hidden") or "").strip() == "true"
            if "alt" not in attrs and not hidden and not _filled(attrs, "aria-label", "aria-labelledby", "title"):
                self.results.append(result("img-without-alt", self.uri, line, column,
                                           "<img%s> has no alt attribute and no other text alternative"
                                           % (' src="%s"' % attrs["src"] if attrs.get("src") else "")))
        if "onclick" in attrs and not self._interactive(tag, attrs):
            missing = []
            try:
                reachable = int(str(attrs.get("tabindex")).strip()) >= 0
            except ValueError:
                reachable = False
            if not reachable:
                missing.append("no tabindex, so the keyboard cannot reach it (WCAG 2.1.1)")
            if not _filled(attrs, "role"):
                missing.append("no role, so assistive technology does not announce it as a control (WCAG 4.1.2)")
            if missing:
                found = result("click-handler-on-non-interactive", self.uri, line, column,
                               "<%s onclick> is not interactive: %s" % (tag, "; ".join(missing)))
                found["properties"]["wcag"] = [sc for sc, gap in (("2.1.1", not reachable), ("4.1.2", not _filled(attrs, "role"))) if gap]
                self.results.append(found)
        if tag in ("input", "select", "textarea"):
            self.fields.append((tag, attrs, line, column, self.label_depth > 0))

    def handle_endtag(self, tag: str) -> None:
        if tag == "label" and self.label_depth:
            self.label_depth -= 1
        if tag == "style" and self.style_at is not None:
            self.results += css_results("".join(self.style_text), self.uri, *self.style_at)
            self.style_at = None

    def handle_data(self, data: str) -> None:
        if self.style_at is not None:
            self.style_text.append(data)

    @staticmethod
    def _interactive(tag: str, attrs: Dict[str, Optional[str]]) -> bool:
        if tag in INTERACTIVE:
            return True
        if tag in ("a", "area"):
            return "href" in attrs
        if tag == "input":
            return (attrs.get("type") or "text").strip().lower() != "hidden"
        if tag in ("audio", "video"):
            return "controls" in attrs
        return False

    def finish(self) -> List[Dict[str, Any]]:
        self.close()
        for tag, attrs, line, column, wrapped in self.fields:
            kind = (attrs.get("type") or "text").strip().lower() if tag == "input" else tag
            if tag == "input" and kind in UNLABELLED_TYPES:
                continue
            named = wrapped or (isinstance(attrs.get("id"), str) and attrs["id"].strip() in self.labels_for) \
                or _filled(attrs, "aria-label", "aria-labelledby", "title")
            if not named:
                hint = "; it has only a placeholder, which is not a label" if _filled(attrs, "placeholder") else ""
                self.results.append(result("input-without-label", self.uri, line, column,
                                           "<%s%s> has no label%s" % (tag, ' type="%s"' % kind if tag == "input" else "", hint)))
            if tag == "input" and kind != "email":
                autocomplete = (attrs.get("autocomplete") or "").lower().split()
                if "email" in autocomplete or EMAIL_NAME.search(attrs.get("name") or "") or EMAIL_NAME.search(attrs.get("id") or ""):
                    self.results.append(result("email-input-not-typed", self.uri, line, column,
                                               '<input type="%s"> holds an email address but is not type="email", so any text is accepted'
                                               % kind))
        return sorted(self.results, key=lambda r: (r["locations"][0]["physicalLocation"]["region"]["startLine"], r["ruleId"]))


# --------------------------------------------------------------------------- the scan

def scan(root: Path) -> Dict[str, Any]:
    unreadable: List[Dict[str, str]] = []
    results: List[Dict[str, Any]] = []
    counts = {"html": 0, "css": 0}
    for path, kind in site_files(root, unreadable):
        uri = uri_of(path)
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            unreadable.append({"path": uri, "reason": "not UTF-8"})
            continue
        counts[kind] += 1
        if kind == "css":
            results += css_results(text, uri)
        else:
            page = Page(uri)
            page.feed(text)
            results += page.finish()
    return {"results": results, "unreadable": unreadable, "counts": counts}


def sarif(found: Dict[str, Any]) -> Dict[str, Any]:
    rules = [{"id": rule, "shortDescription": {"text": info["text"]},
              "properties": {"wcag": info["wcag"], "cwe": info["cwe"], "wcag_related": info["wcag_related"], "approximate": True,
                             "tags": ["accessibility" if info["wcag"] else "input-validation"]
                             + ["WCAG %s" % sc for sc in info["wcag"]] + info["cwe"]}}
             for rule, info in RULES.items()]
    notes = [{"level": "warning", "message": {"text": "unreadable: %s: %s" % (u["path"], u["reason"])}} for u in found["unreadable"]]
    return {"version": "2.1.0", "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [{"tool": {"driver": {"name": DRIVER, "version": VERSION, "rules": rules}},
                      "invocations": [{"executionSuccessful": True, "toolExecutionNotifications": notes}],
                      "results": found["results"],
                      "properties": {"unreadable": found["unreadable"], "files": found["counts"]}}]}


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root")
    parser.add_argument("--out")
    parser.add_argument("--copy-to-work", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.copy_to_work:
            if args.root or args.out:
                raise Refused("--copy-to-work takes no other option")
            data = Path(__file__).read_bytes()
            written = write_new(WORK_DIR + "/static_ui_scan.py", data)
            print(json.dumps({"ok": True, "written": written, "sha256": sha256(data),
                              "input": "/work/.qa-input/frontend/static_ui_scan.py"}, sort_keys=True))
            return 0
        if not args.root:
            raise Refused("--root DIR is required")
        root = Path(args.root)
        mode = os.lstat(str(root)).st_mode if os.path.lexists(str(root)) else None
        if mode is None or stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise Refused("--root must be a directory that is not a link")
        found = scan(root)
        data = (json.dumps(sarif(found), indent=1, sort_keys=True) + "\n").encode("utf-8")
        written = write_new(args.out, data) if args.out else None
    except (Refused, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, Refused) else "%s: %s" % (type(exc).__name__, exc)},
                         sort_keys=True))
        return 2
    by_rule: Dict[str, int] = {}
    listed = []
    for item in found["results"]:
        by_rule[item["ruleId"]] = by_rule.get(item["ruleId"], 0) + 1
        region = item["locations"][0]["physicalLocation"]
        listed.append({"rule": item["ruleId"], "at": "%s:%d" % (region["artifactLocation"]["uri"], region["region"]["startLine"]),
                       "wcag": item["properties"]["wcag"], "cwe": item["properties"]["cwe"],
                       "wcag_related": item["properties"]["wcag_related"], "message": item["message"]["text"]})
    print(json.dumps({"ok": True, "approximate": True, "files": found["counts"], "results": len(listed), "by_rule": by_rule,
                      "listed": listed, "unreadable": found["unreadable"], "out": written,
                      "sha256": sha256(data) if written else None}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
