from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, NamedTuple, Optional, Tuple

from . import ids, redact, resource_dir, schema
from .canonical import dumps, hash_obj, loads
from .errors import QaError
from .workspace import Workspace

# QaError codes raised here: invalid_session, unknown_event_type, ledger_partial_line,
# ledger_corrupt, ledger_write_failed; schema_invalid comes from schema.validate.

_SESSION_RE = re.compile(r"S-[0-9A-HJKMNP-TV-Z]{26}")
_TYPE_RE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
_LEDGER_FILE_RE = re.compile(r"(S-[0-9A-HJKMNP-TV-Z]{26})\.jsonl")
_ENVELOPE = "envelope.schema.json"
_TAIL_BLOCK = 65536


@dataclass(frozen=True)
class Actor:
    role: str
    skill: Optional[str]
    session: str
    independence: str


class Problem(NamedTuple):
    kind: str  # modified | unchained | missing | invalid
    where: str
    detail: str


def validate_session_id(s: str) -> str:
    if not isinstance(s, str) or not _SESSION_RE.fullmatch(s):
        raise QaError("invalid_session", "session id must look like S-<26 Crockford base32>", session=s)
    return s


def _ledger_dir(ws: Workspace) -> Path:
    return ws.path("ledger")


def _session_file(ws: Workspace, session: str) -> Path:
    return _ledger_dir(ws) / (validate_session_id(session) + ".jsonl")


def _parse_envelope(raw: bytes) -> Optional[dict]:
    """The module's one "valid envelope" predicate: a JSON object that fits envelope.schema.json."""
    try:
        obj = loads(raw)
        schema.validate(obj, _ENVELOPE)
    except QaError:
        return None
    return obj


def _read_lines(path: Path) -> List[Tuple[int, Optional[dict]]]:
    """(line number, envelope or None) for every line, a partial trailing line included."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    parts = raw.split(b"\n")
    if parts and parts[-1] == b"":
        parts.pop()
    return [(i + 1, _parse_envelope(p)) for i, p in enumerate(parts)]


def _last_line(path: Path) -> Optional[bytes]:
    """The last complete line of ``path``; None when absent or empty; refuses a partial one."""
    try:
        f = open(str(path), "rb")
    except FileNotFoundError:
        return None
    with f:
        pos = f.seek(0, os.SEEK_END)
        data = b""
        while pos > 0:
            step = min(_TAIL_BLOCK, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
            if b"\n" in data[:-1]:
                break
    if not data:
        return None
    if not data.endswith(b"\n"):
        raise QaError("ledger_partial_line", "session file does not end in a newline", file=path.name)
    return data[:-1].rsplit(b"\n", 1)[-1]


def append(ws: Workspace, actor: Actor, type: str, data: dict) -> dict:
    session = validate_session_id(actor.session)
    if not isinstance(type, str) or not _TYPE_RE.fullmatch(type) or not _event_schema_exists(type):
        raise QaError("unknown_event_type", "no schema for this event type", type=type)
    schema.validate(data, "events/%s.schema.json" % type)
    clean, redactions = redact.load_redactor(ws).obj(data)
    path = _session_file(ws, session)
    with ws.lock():
        _ledger_dir(ws).mkdir(parents=True, exist_ok=True)
        last = _last_line(path)
        if last is None:
            seq, prev = 1, None
        else:
            head = _parse_envelope(last)
            if head is None:
                raise QaError("ledger_corrupt", "last ledger line is not a valid envelope", file=path.name)
            seq, prev = head["seq"] + 1, head["hash"]
        envelope = {
            "v": 1,
            "seq": seq,
            "id": ids.new_id("EVT"),
            "ts": ids.format_ts(ids.now_utc()),
            "type": type,
            "actor": {"role": actor.role, "skill": actor.skill, "session": session},
            "data": clean,
            "prev": prev,
        }
        if redactions > 0:
            envelope["redactions"] = redactions
        envelope["hash"] = hash_obj(envelope)
        schema.validate(envelope, _ENVELOPE)
        line = dumps(envelope) + b"\n"
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            if os.write(fd, line) != len(line):
                raise QaError("ledger_write_failed", "short write to the ledger", file=path.name)
            os.fsync(fd)
        finally:
            os.close(fd)
    return envelope


def _event_schema_exists(type: str) -> bool:
    return (resource_dir("schemas") / "events" / (type + ".schema.json")).is_file()


def iter_session(ws: Workspace, session: str) -> Iterator[dict]:
    for _, env in _read_lines(_session_file(ws, session)):
        if env is not None:
            yield env


def _session_files(ws: Workspace) -> Tuple[List[Tuple[str, Path]], List[str]]:
    """(session, path) for each ledger file, plus the names of entries that are not ledger files."""
    files: List[Tuple[str, Path]] = []
    foreign: List[str] = []
    d = _ledger_dir(ws)
    if d.is_dir():
        for p in sorted(d.iterdir()):
            m = _LEDGER_FILE_RE.fullmatch(p.name)
            if m and p.is_file():
                files.append((m.group(1), p))
            else:
                foreign.append(p.name)
    return files, foreign


def iter_all_events(ws: Workspace) -> List[dict]:
    events: List[dict] = []
    for session, path in _session_files(ws)[0]:
        events.extend(e for _, e in _read_lines(path) if e is not None)
    events.sort(key=lambda e: (e["ts"], e["actor"]["session"], e["seq"]))
    return events


def heads(ws: Workspace) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for session, path in _session_files(ws)[0]:
        valid = [e for _, e in _read_lines(path) if e is not None]
        if valid:
            out[session] = {"seq": valid[-1]["seq"], "hash": valid[-1]["hash"]}
    return out


def verify_chains(ws: Workspace, anchors: Optional[Dict[str, dict]] = None, *, foreign: bool = True) -> List[Problem]:
    """Every problem in the session files and, unless ``foreign`` is False, the directory entries that are not ledger files.

    The fold never reads such an entry, so it says nothing about what the ledger authorizes.
    """
    problems: List[Problem] = []
    files, others = _session_files(ws)
    for name in others if foreign else ():
        problems.append(Problem("invalid", name, "not a ledger file"))
    present = set()
    for session, path in files:
        present.add(session)
        prev_hash: Optional[str] = None
        prev_seq = 0
        by_seq: Dict[int, str] = {}
        for lineno, env in _read_lines(path):
            where = "%s:%d" % (path.name, lineno)
            if env is None:
                problems.append(Problem("invalid", where, "line is not a valid event envelope"))
                continue
            if env["prev"] != prev_hash or env["seq"] != prev_seq + 1:
                problems.append(Problem(
                    "unchained", where,
                    "expected seq %d after %s, found seq %d after %s"
                    % (prev_seq + 1, prev_hash, env["seq"], env["prev"])))
            body = {k: v for k, v in env.items() if k != "hash"}
            if hash_obj(body) != env["hash"]:
                problems.append(Problem("modified", where, "stored hash does not match the event content"))
            prev_hash, prev_seq = env["hash"], env["seq"]
            by_seq[env["seq"]] = env["hash"]
        anchor = (anchors or {}).get(session)
        if anchor is not None:
            at = by_seq.get(anchor["seq"])
            if at is None:
                problems.append(Problem("unchained", path.name,
                                        "session ends before anchored seq %d" % anchor["seq"]))
            elif at != anchor["hash"]:
                problems.append(Problem("modified", path.name,
                                        "event at anchored seq %d differs from the anchor" % anchor["seq"]))
    for session in sorted(anchors or {}):
        if session not in present:
            problems.append(Problem("missing", session + ".jsonl", "anchored session file is gone"))
    return problems


def format_human_id(prefix: str, n: int) -> str:
    return "%s-%03d" % (prefix, n)


def next_human_id(ws: Workspace, prefix: str) -> str:
    pattern = re.compile(re.escape(prefix) + r"-([0-9]+)")
    highest = 0
    for env in iter_all_events(ws):
        value = env["data"].get("id")
        m = pattern.fullmatch(value) if isinstance(value, str) else None
        if m:
            highest = max(highest, int(m.group(1)))
    return format_human_id(prefix, highest + 1)
