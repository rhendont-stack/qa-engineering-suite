"""Answer-key recall: of the entries the tools present could find, how many they found.

``recall(key, detections, tools_present)`` reads a key's ``issues``. Each detection is ``{"tool", "path", "line", "endpoint"}``
with an optional ``"cwe"`` (``"CWE-942"``).

- An entry is **detectable** when one of its ``detectable_by`` tools is in ``tools_present``.
- A detection **locates** an entry in one of three ways:
  - by file and line: the detection's ``path`` is the entry's ``file``, or ends in ``/`` plus it (as a container path does), and
    its ``line`` lies within the entry's ``lines``;
  - by file alone: the same path with no ``line``, which locates only an entry marked ``"file_level": true`` (a manifest that
    a tool reports without a region);
  - by operation: the detection's ``endpoint``, as ``METHOD /path`` or ``METHOD <url>``, names the entry's ``location``. The
    query string is ignored, the path is percent-decoded and a ``{name}`` segment matches one path segment.
  Paths are percent-decoded too, and ``\\``, ``./`` and ``file://`` are normalised.
- A detection's **class** must agree: when both the detection's ``cwe`` and the CWE that leads the entry's ``class`` are
  known, they must be equal. A detection without a ``cwe`` counts on its location alone.
- An entry is **detected** when a detection from one of its present tools locates it and its class agrees. A detection from a
  tool that is not present counts for nothing. One detection credits two entries only when both locations and both classes
  agree.

The result is ``{"detectable", "detected", "recall", "missed", "detected_unclassified"}``. ``missed`` lists the detectable
entries not detected, in key order; ``detected_unclassified`` lists, in key order, the detected entries that only detections
without a ``cwe`` credited, so a reader can see which credits rest on location alone. ``recall`` is ``None`` when nothing is
detectable.

Standard library only; no state, no files.
"""
from __future__ import annotations

import posixpath
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set
from urllib.parse import unquote, urlsplit

_METHOD = re.compile(r"[A-Za-z]+")
_CWE = re.compile(r"(?i)(?:CWE-)?(\d+)")


def _operation(text: Any) -> Optional[str]:
    """``METHOD /path`` from ``METHOD /path?query`` or ``METHOD scheme://host/path``, or None when ``text`` is not one."""
    if not isinstance(text, str):
        return None
    method, _, target = text.strip().partition(" ")
    target = target.strip()
    if not _METHOD.fullmatch(method) or not target or " " in target:
        return None
    path = urlsplit(target).path if "://" in target else target.split("?", 1)[0].split("#", 1)[0]
    path = unquote(path)
    if not path.startswith("/"):
        return None
    return "%s %s" % (method.upper(), path)


def _operation_pattern(location: str) -> Optional["re.Pattern[str]"]:
    operation = _operation(location)
    if operation is None:
        return None
    method, path = operation.split(" ", 1)
    segments = [r"[^/]+" if re.fullmatch(r"\{[^/{}]+\}", part) else re.escape(part) for part in path.split("/")]
    return re.compile(re.escape(method) + " " + "/".join(segments))


def _path(text: Any) -> Optional[str]:
    if not isinstance(text, str) or not text.strip():
        return None
    path = text.strip().replace("\\", "/")
    if path.startswith("file://"):
        path = urlsplit(path).path
    return posixpath.normpath(unquote(path))


def _cwe(text: Any) -> Optional[str]:
    """``CWE-<n>`` from ``CWE-942``, ``cwe-942`` or ``942``; None when ``text`` names no CWE."""
    if isinstance(text, int) and not isinstance(text, bool):
        return "CWE-%d" % text
    if not isinstance(text, str):
        return None
    found = _CWE.fullmatch(text.strip())
    return "CWE-%d" % int(found.group(1)) if found else None


def entry_cwe(issue: Mapping[str, Any]) -> Optional[str]:
    """The CWE that leads an entry's ``class`` (``"CWE-942 permissive ..."``), or None."""
    words = str(issue.get("class", "")).split(" ", 1)
    return _cwe(words[0]) if words[0].upper().startswith("CWE-") else None


def _line_within(line: Any, lines: Any) -> bool:
    if not isinstance(line, int) or isinstance(line, bool):
        return False
    first, last = lines
    return first <= line <= last


def locates(issue: Mapping[str, Any], detection: Mapping[str, Any]) -> bool:
    """Whether ``detection`` points at ``issue``, by file and line, by file alone or by operation; tool and class not considered."""
    pattern = _operation_pattern(issue.get("location", ""))
    found = _operation(detection.get("endpoint"))
    if pattern is not None and found is not None and pattern.fullmatch(found):
        return True
    wanted, path = _path(issue.get("file")), _path(detection.get("path"))
    if wanted is None or path is None or not (path == wanted or path.endswith("/" + wanted)):
        return False
    if detection.get("line") is None:
        return issue.get("file_level") is True
    return _line_within(detection.get("line"), issue.get("lines", (0, -1)))


def class_agrees(issue: Mapping[str, Any], detection: Mapping[str, Any]) -> bool:
    """False only when the detection and the entry both name a CWE and the two differ."""
    wanted, given = entry_cwe(issue), detection.get("cwe")
    if given is None or wanted is None:
        return True
    return _cwe(given) == wanted


def recall(key: Mapping[str, Any], detections: Iterable[Mapping[str, Any]], tools_present: Set[str]) -> Dict[str, Any]:
    present = set(tools_present)
    detections = list(detections)
    detectable, detected = 0, 0
    missed: List[str] = []
    unclassified: List[str] = []
    for issue in key.get("issues", []):
        tools = set(issue.get("detectable_by", ())) & present
        if not tools:
            continue
        detectable += 1
        credits = [detection for detection in detections
                   if detection.get("tool") in tools and locates(issue, detection) and class_agrees(issue, detection)]
        if not credits:
            missed.append(issue["id"])
            continue
        detected += 1
        if all(detection.get("cwe") is None for detection in credits):
            unclassified.append(issue["id"])
    return {"detectable": detectable, "detected": detected, "recall": detected / detectable if detectable else None, "missed": missed,
            "detected_unclassified": unclassified}
