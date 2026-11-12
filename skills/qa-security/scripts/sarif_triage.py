"""Lists the alerts of a stored SARIF report by rule, CWE and location, with no snippets and every secret masked.

Usage: python3 sarif_triage.py --sarif PATH [--out PATH]

``--sarif`` is a report the kernel stored (``.qa/evidence/<EXE>/reports/<file>``) or one under ``.qa/work/``: a regular file
inside the project's ``.qa/``, reached through no link, at most 32 MiB. For each result it gives:

* ``tool``, ``rule`` and ``level``;
* ``cwe``: every CWE the rule or the result names, read from the rule's ``properties.tags`` and ``properties.cwe``
  (``CWE-89: …``, ``external/cwe/cwe-89``), from a relationship to a taxonomy named CWE (ZAP's form), from the result's
  ``taxa``, and only when none of those names one, from a ``(CWE-n)`` in the rule's descriptions. Empty when the report names
  none (Gitleaks and OSV-Scanner rules carry no CWE);
* ``path`` and ``line`` (``file://``, ``/work/.qa-input/`` and ``/work/`` stripped), or ``endpoint`` (``METHOD URL``) for a
  result about a web request (ZAP's ``webRequest`` or an ``http(s)`` location);
* ``message``: the result's message with every secret-shaped value masked as ``[masked:<8 hex>]``, the start of its sha256,
  so the same value has the same mask everywhere. Masked: what the kernel's redaction patterns match, and any run of 20 or more
  letters, digits and ``_-+/=`` that mixes letters and digits. Snippets, fixes, code flows and request bodies are never read
  into the output.

The full list goes to ``--out`` (default ``.qa/work/security/triage-<report name>.json``), written under ``.qa/work/`` through
no link. Stdout gets one JSON summary: the tool and its version, the count, and counts per rule and per CWE. Exit 0 when the
report was read, 2 on bad arguments, a refused path or a report that is not SARIF. Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Sequence
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
REDACTION = HERE / "_kernel" / "policies" / "redaction.json"
MAX_BYTES = 32 << 20
CWE_TEXT = re.compile(r"(?i)\bcwe[-/ _]?(\d{1,5})\b")
DESCRIBED_CWE = re.compile(r"\(CWE-(\d{1,5})\)")
LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-+/=]{20,}")
WEB = re.compile(r"(?i)^https?://")


def _writer():
    spec = importlib.util.spec_from_file_location("qa_security_unicode_scan", str(HERE / "unicode_scan.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def redaction_patterns() -> List["re.Pattern[str]"]:
    try:
        policy = json.loads(REDACTION.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    found = []
    for pattern in policy.get("patterns", []):
        try:
            found.append(re.compile(pattern["regex"]))
        except (KeyError, re.error, TypeError):
            continue
    return found


PATTERNS = redaction_patterns()


def mask_value(value: str) -> str:
    return "[masked:%s]" % hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def mask(text: str) -> str:
    for pattern in PATTERNS:
        text = pattern.sub(lambda m: mask_value(m.group(0)), text)

    def long_token(match: "re.Match[str]") -> str:
        value = match.group(0)
        if value.startswith("[masked:") or not (re.search(r"[A-Za-z]", value) and re.search(r"\d", value)):
            return value
        if WEB.match(value) or "/" in value.strip("/") and not re.search(r"[+=]", value):
            return value                                  # a path or a URL, not a secret
        return mask_value(value)

    return LONG_TOKEN.sub(long_token, text)


def read_report(root: Path, text: str, refused) -> Dict[str, Any]:
    parts = PurePosixPath(text).parts
    if PurePosixPath(text).is_absolute() or ".." in parts or parts[:1] != (".qa",) or len(parts) < 3:
        raise refused("--sarif must be a path under .qa/")
    here = root
    for part in parts:
        here = here / part
        mode = os.lstat(str(here)).st_mode
        if stat.S_ISLNK(mode):
            raise refused("--sarif passes through a link")
    if not stat.S_ISREG(mode):
        raise refused("--sarif is not a regular file")
    fd = os.open(str(here), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise refused("the report is larger than 32 MiB")
    try:
        report = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        raise refused("the report is not JSON") from None
    if not isinstance(report, dict) or not isinstance(report.get("runs"), list):
        raise refused("the report is not SARIF (no runs)")
    return report


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def cwes_of(rule: Dict[str, Any], result: Dict[str, Any]) -> List[str]:
    found = set()
    for props in (_dict(rule.get("properties")), _dict(result.get("properties"))):
        for item in _list(props.get("tags")) + _list(props.get("cwe")):
            for match in CWE_TEXT.finditer(str(item)):
                found.add(int(match.group(1)))
    for relation in _list(rule.get("relationships")):
        target = _dict(_dict(relation).get("target"))
        if str(_dict(target.get("toolComponent")).get("name", "")).upper() == "CWE" and str(target.get("id", "")).isdigit():
            found.add(int(target["id"]))
    for taxon in _list(result.get("taxa")):
        taxon = _dict(taxon)
        if str(_dict(taxon.get("toolComponent")).get("name", "")).upper() == "CWE" and str(taxon.get("id", "")).isdigit():
            found.add(int(taxon["id"]))
    if not found:
        for field in ("shortDescription", "fullDescription"):
            for match in DESCRIBED_CWE.finditer(str(_dict(rule.get(field)).get("text", ""))):
                found.add(int(match.group(1)))
    return ["CWE-%d" % n for n in sorted(found)]


def clean_path(uri: str) -> str:
    text = uri
    if text.startswith("file://"):
        text = text[len("file://"):]
    for prefix in ("/work/.qa-input/", "/work/"):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def location_of(result: Dict[str, Any]) -> Dict[str, Any]:
    request = _dict(result.get("webRequest"))
    first = _dict(_list(result.get("locations"))[0]) if _list(result.get("locations")) else {}
    physical = _dict(first.get("physicalLocation"))
    uri = str(_dict(physical.get("artifactLocation")).get("uri", ""))
    if request.get("target") or WEB.match(uri):
        method = str(request.get("method") or "GET").upper()
        url = str(request.get("target") or uri)
        if not urlsplit(url).path:
            url = url.split("?", 1)[0].split("#", 1)[0] + "/" + (url[len(url.split("?", 1)[0]):] if "?" in url else "")
        return {"path": None, "line": None, "endpoint": "%s %s" % (method, mask(url))}
    line = _dict(physical.get("region")).get("startLine")
    return {"path": clean_path(uri) or None, "line": line if isinstance(line, int) and not isinstance(line, bool) else None,
            "endpoint": None}


def triage(report: Dict[str, Any]) -> Dict[str, Any]:
    alerts: List[Dict[str, Any]] = []
    tool, version = None, None
    for run in _list(report.get("runs")):
        driver = _dict(_dict(_dict(run).get("tool")).get("driver"))
        tool, version = tool or driver.get("name"), version or driver.get("version") or driver.get("semanticVersion")
        rules = {str(_dict(r).get("id")): _dict(r) for r in _list(driver.get("rules"))}
        listed = _list(driver.get("rules"))
        for result in _list(_dict(run).get("results")):
            result = _dict(result)
            rule_id = str(result.get("ruleId") or _dict(result.get("rule")).get("id") or "")
            rule = rules.get(rule_id)
            if rule is None and isinstance(result.get("ruleIndex"), int) and 0 <= result["ruleIndex"] < len(listed):
                rule = _dict(listed[result["ruleIndex"]])
            rule = rule or {}
            alert = {"tool": driver.get("name"), "rule": rule_id or rule.get("id"), "level": result.get("level") or
                     _dict(rule.get("defaultConfiguration")).get("level"), "cwe": cwes_of(rule, result),
                     "message": mask(str(_dict(result.get("message")).get("text", "")))[:500]}
            alert.update(location_of(result))
            alerts.append(alert)
    by_rule: Dict[str, int] = {}
    by_cwe: Dict[str, int] = {}
    for alert in alerts:
        by_rule[str(alert["rule"])] = by_rule.get(str(alert["rule"]), 0) + 1
        for cwe in alert["cwe"] or ["none"]:
            by_cwe[cwe] = by_cwe.get(cwe, 0) + 1
    return {"tool": tool, "version": version, "results": len(alerts), "by_rule": by_rule, "by_cwe": by_cwe, "alerts": alerts}


def detections(triaged: Dict[str, Any], tool: str) -> List[Dict[str, Any]]:
    """The alerts as answer-key detections (``benchmark/graders/recall.py``): ``{tool, path, line, endpoint, cwe}``, one per CWE
    (or one with ``cwe`` None when the rule names none)."""
    found = []
    for alert in triaged["alerts"]:
        for cwe in alert["cwe"] or [None]:
            found.append({"tool": tool, "path": alert["path"], "line": alert["line"], "endpoint": alert["endpoint"], "cwe": cwe})
    return found


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sarif", required=True)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    writer = _writer()
    root = Path.cwd().resolve()
    try:
        triaged = triage(read_report(root, args.sarif, writer.Refused))
        out = args.out or ".qa/work/security/triage-%s.json" % re.sub(r"[^A-Za-z0-9_.-]", "_", PurePosixPath(args.sarif).stem)
        if os.path.isabs(out):
            raise writer.Refused("--out must be a path under .qa/work/")
        written = writer.write_bytes(out, (json.dumps(triaged, indent=1, sort_keys=True) + "\n").encode("utf-8"), root)
    except (writer.Refused, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, writer.Refused) else type(exc).__name__}, sort_keys=True))
        return 2
    print(json.dumps({"ok": True, "out": written, "tool": triaged["tool"], "version": triaged["version"], "results": triaged["results"],
                      "by_rule": triaged["by_rule"], "by_cwe": triaged["by_cwe"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
