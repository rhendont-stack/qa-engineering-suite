"""Answer-key recall: of the entries the tools present could find, how many they found.

``recall(key, detections, tools_present)`` reads a key's ``issues``. An entry is detectable when one of its ``detectable_by``
tools is in ``tools_present``. It is detected when a detection from one of those present tools locates it, either by file and
line (the detection's ``path`` is the entry's ``file``, or ends in ``/`` plus it, as a container path does, and its ``line`` lies
within the entry's ``lines``) or by operation (the detection's ``endpoint``, as ``METHOD /path`` or ``METHOD <url>``, names the
entry's ``location``; the query string is ignored and a ``{name}`` segment matches one path segment). A detection from a tool
not present counts for nothing. The result is ``{"detectable", "detected", "recall", "missed"}``, with ``missed`` in key
order and ``recall`` ``None`` when nothing is detectable.

Standard library only; no state, no files.
"""
from __future__ import annotations

import posixpath
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set
from urllib.parse import urlsplit

_METHOD = re.compile(r"[A-Za-z]+")


def _operation(text: Any) -> Optional[str]:
    """``METHOD /path`` from ``METHOD /path?query`` or ``METHOD scheme://host/path``, or None when ``text`` is not one."""
    if not isinstance(text, str):
        return None
    method, _, target = text.strip().partition(" ")
    target = target.strip()
    if not _METHOD.fullmatch(method) or not target or " " in target:
        return None
    path = urlsplit(target).path if "://" in target else target.split("?", 1)[0].split("#", 1)[0]
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
    return posixpath.normpath(path)


def _line_within(line: Any, lines: Any) -> bool:
    if not isinstance(line, int) or isinstance(line, bool):
        return False
    first, last = lines
    return first <= line <= last


def locates(issue: Mapping[str, Any], detection: Mapping[str, Any]) -> bool:
    """Whether ``detection`` points at ``issue``, by file and line or by operation; the tool is not considered here."""
    pattern = _operation_pattern(issue.get("location", ""))
    found = _operation(detection.get("endpoint"))
    if pattern is not None and found is not None and pattern.fullmatch(found):
        return True
    wanted, path = _path(issue.get("file")), _path(detection.get("path"))
    if wanted is None or path is None or not (path == wanted or path.endswith("/" + wanted)):
        return False
    return _line_within(detection.get("line"), issue.get("lines", (0, -1)))


def recall(key: Mapping[str, Any], detections: Iterable[Mapping[str, Any]], tools_present: Set[str]) -> Dict[str, Any]:
    present = set(tools_present)
    detections = list(detections)
    detectable, detected = 0, 0
    missed: List[str] = []
    for issue in key.get("issues", []):
        tools = set(issue.get("detectable_by", ())) & present
        if not tools:
            continue
        detectable += 1
        if any(detection.get("tool") in tools and locates(issue, detection) for detection in detections):
            detected += 1
        else:
            missed.append(issue["id"])
    return {"detectable": detectable, "detected": detected, "recall": detected / detectable if detectable else None, "missed": missed}
