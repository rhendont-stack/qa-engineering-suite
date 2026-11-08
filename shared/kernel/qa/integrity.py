"""Checks that the recorded evidence, the ledger and the installed kernel are still what was written (§9.4, §15.7)."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Dict, List, Optional

from . import ledger, schema
from .canonical import loads, sha256_bytes, sha256_file
from .errors import QaError
from .ledger import Problem
from .workspace import Workspace, write_atomic

MANIFEST = "MANIFEST.sha256"
_MANIFEST_LINE = re.compile(r"([0-9a-f]{64})  (.+)")
_MANIFEST_NAME = "manifest.json"
_UNSAFE_PATH = "the path is absolute or leaves its directory, so it was not opened"
_DETAILS = {"missing": "the recorded file is gone", "modified": "the content differs from the recorded hash"}
# kernel_integrity's lists, and the kind of problem each entry becomes.
_KERNEL_KINDS = (("modified", "modified"), ("missing", "missing"), ("extra", "modified"))


def _unsafe(name: object) -> bool:
    """True unless ``name`` is a plain relative path that stays inside its directory."""
    if not isinstance(name, str) or not name or "\0" in name or name[0] in "/\\" or re.match(r"[A-Za-z]:", name):
        return True
    return ".." in re.split(r"[/\\]", name)


def _is_regular(path: Path) -> bool:
    """``lstat``, because opening anything else can hang (a FIFO) or never end (a symlink to ``/dev/zero``)."""
    return stat.S_ISREG(os.lstat(path).st_mode)


def read_regular(path: Path) -> bytes:
    """``path``'s bytes. Anything but a regular file is an ``OSError``: ``read_bytes`` on a device grows without end."""
    if not _is_regular(path):
        raise OSError("not a regular file: %s" % path)
    return path.read_bytes()


def _difference(path: Path, expected: str) -> Optional[str]:
    """``missing`` or ``modified`` when the file at ``path`` is not the recorded one, otherwise None."""
    try:
        return None if _is_regular(path) and sha256_file(path) == expected else "modified"
    except (FileNotFoundError, NotADirectoryError):
        return "missing"
    except OSError:
        return "modified"


def _check_file(directory: Path, execution_id: str, name: object, expected: str) -> List[Problem]:
    where = "%s/%s" % (execution_id, name)
    if _unsafe(name):
        return [Problem("invalid", where, _UNSAFE_PATH)]
    difference = _difference(directory / str(name), expected)
    return [] if difference is None else [Problem(difference, where, _DETAILS[difference])]


def _absent_files(directory: Path, execution_id: str, files: List[str]) -> List[Problem]:
    """With no trustworthy manifest, the event's file list can only be checked for presence."""
    problems = []
    for name in files:
        where = "%s/%s" % (execution_id, name)
        if _unsafe(name):
            problems.append(Problem("invalid", where, _UNSAFE_PATH))
        elif not os.path.lexists(directory / name):
            problems.append(Problem("missing", where, _DETAILS["missing"]))
    return problems


def _check_manifest(directory: Path, execution_id: str, recorded: dict) -> List[Problem]:
    where = "%s/%s" % (execution_id, _MANIFEST_NAME)
    path = directory / _MANIFEST_NAME
    difference = _difference(path, recorded["sha256"])
    if difference is not None:
        return [Problem(difference, where, _DETAILS[difference])] + _absent_files(directory, execution_id, recorded["files"])
    try:
        entries = loads(path.read_bytes())
    except (OSError, QaError):
        entries = None
    if not isinstance(entries, dict):
        return [Problem("invalid", where, "the manifest is not a JSON object")]
    problems = []
    for name, entry in entries.items():
        expected = entry.get("sha256") if isinstance(entry, dict) else None
        if isinstance(expected, str):
            problems.extend(_check_file(directory, execution_id, name, expected))
        else:
            problems.append(Problem("invalid", "%s/%s" % (execution_id, name), "the manifest entry has no sha256"))
    return problems


def verify_evidence(ws: Workspace, st: dict) -> List[Problem]:
    problems: List[Problem] = []
    for execution_id, record in st["executions"].items():
        directory = ws.path("evidence", execution_id)
        if record["evidence_manifest"] is not None:
            problems.extend(_check_manifest(directory, execution_id, record["evidence_manifest"]))
        for item in record["ingested"]:
            problems.extend(_check_file(directory, execution_id, item["file"], item["sha256"]))
    return problems


# --------------------------------------------------------------------------- kernel manifest

def kernel_files(kernel_dir: Path) -> List[Path]:
    kernel_dir = Path(kernel_dir)
    found = []
    for folder, directories, names in os.walk(str(kernel_dir)):
        directories[:] = [d for d in directories if d != "__pycache__"]
        for name in names:
            path = Path(folder) / name
            if not name.endswith(".pyc") and path != kernel_dir / MANIFEST and _is_regular(path):
                found.append(path)
    return sorted(found, key=lambda p: p.relative_to(kernel_dir).as_posix())


def write_manifest(kernel_dir: Path) -> Path:
    kernel_dir = Path(kernel_dir)
    lines = ["%s  %s\n" % (sha256_file(p)[len("sha256:"):], p.relative_to(kernel_dir).as_posix())
             for p in kernel_files(kernel_dir)]
    target = kernel_dir / MANIFEST
    write_atomic(target, "".join(lines).encode("utf-8"))
    return target


def _parse_manifest(raw: bytes) -> Optional[Dict[str, str]]:
    """{relative path: hex digest}, or None when any line is malformed."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text and not text.endswith("\n"):
        return None
    entries: Dict[str, str] = {}
    for line in text.split("\n")[:-1]:
        match = _MANIFEST_LINE.fullmatch(line)
        if match is None or _unsafe(match.group(2)) or match.group(2) in entries:
            return None
        entries[match.group(2)] = match.group(1)
    return entries


def kernel_integrity(kernel_dir: Optional[Path] = None) -> dict:
    kernel_dir = Path(kernel_dir) if kernel_dir is not None else Path(__file__).resolve().parent.parent
    report: dict = {
        "status": "absent",
        "source_checkout": not (kernel_dir / "schemas").is_dir() and (kernel_dir.parent / "schemas").is_dir(),
        "manifest_sha256": None, "modified": [], "missing": [], "extra": [],
    }
    try:
        raw = read_regular(kernel_dir / MANIFEST)
    except FileNotFoundError:
        return report
    except OSError:
        return dict(report, status="modified", modified=[MANIFEST])
    report["manifest_sha256"] = sha256_bytes(raw)
    expected = _parse_manifest(raw)
    if expected is None:
        return dict(report, status="modified", modified=[MANIFEST])
    actual = {p.relative_to(kernel_dir).as_posix(): p for p in kernel_files(kernel_dir)}
    report["missing"] = sorted(set(expected) - set(actual))
    report["extra"] = sorted(set(actual) - set(expected))
    report["modified"] = sorted(name for name in set(expected) & set(actual)
                                if sha256_file(actual[name]) != "sha256:" + expected[name])
    report["status"] = "modified" if report["missing"] or report["extra"] or report["modified"] else "ok"
    return report


# --------------------------------------------------------------------------- everything together

def _latest_anchors(st: dict) -> Optional[Dict[str, dict]]:
    gates = st["gates"]
    if not gates:
        return None
    return gates[max(gates, key=lambda gate_id: int(gate_id[len("GATE-"):]))].get("heads")


def _event_problems(ws: Workspace) -> List[Problem]:
    """``verify_chains`` already reports lines that do not fit the envelope; this covers what they carry."""
    problems = []
    for session in ledger.heads(ws):
        for event in ledger.iter_session(ws, session):
            where = "%s.jsonl:%d" % (session, event["seq"])
            if event["actor"]["session"] != session:
                problems.append(Problem("invalid", where, "actor.session differs from the session of its file"))
            try:
                schema.validate(event["data"], "events/%s.schema.json" % event["type"])
            except QaError as exc:
                problems.append(Problem("invalid", where, "the event does not fit its type's schema: %s" % exc.code))
    return problems


def _kernel_problems() -> List[Problem]:
    report = kernel_integrity()
    return [Problem(kind, "kernel:" + name, "%s against %s" % (field, MANIFEST))
            for field, kind in _KERNEL_KINDS for name in report[field]]


def problems(ws: Workspace, st: dict) -> List[Problem]:
    """Everything ``qa ledger verify`` reports (§9.4): chains, events, evidence and the kernel."""
    return ledger.verify_chains(ws, _latest_anchors(st)) + _event_problems(ws) + verify_evidence(ws, st) + _kernel_problems()
