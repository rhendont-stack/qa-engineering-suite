from __future__ import annotations

import fnmatch
import hashlib
import hmac
import math
import os
import re
from collections import Counter, namedtuple
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import resource_dir, schema
from .canonical import loads
from .errors import QaError
from .workspace import Workspace, read_config, read_limited

# QaError codes raised here: redaction_policy_invalid, redaction_key_invalid, plus
# invalid_json / schema_invalid from reading and validating the policy and config.

RedactionPattern = namedtuple("RedactionPattern", "kind regex group min_len min_entropy")

ENV_NAME_GLOBS = ("*_TOKEN", "*_SECRET", "*_KEY", "*PASSWORD*")
MIN_ENV_VALUE_LEN = 8
KEY_BYTES = 32
_MAX_KEY_READ = 4096  # past the key's size, so a wrong key still reports its length

_MARKER_RE = re.compile(r"\[REDACTED:[a-z_]+:[0-9a-f]{8}\]")
_PATTERNS: Optional[List[RedactionPattern]] = None
_COMPILED: Dict[str, "re.Pattern[str]"] = {}

# A text is a list of (chunk, is_marker). Markers are never scanned again.
_Parts = List[Tuple[str, bool]]


def shannon_entropy(s: str) -> float:
    """Bits per character."""
    if not s:
        return 0.0
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def marker(kind: str, secret: str, key: bytes) -> str:
    digest = hmac.new(key, secret.encode("utf-8", "surrogateescape"), hashlib.sha256).hexdigest()
    return "[REDACTED:%s:%s]" % (kind, digest[:8])


def load_patterns() -> List[RedactionPattern]:
    """Patterns of ``redaction.json`` in file order; validated and compiled once."""
    global _PATTERNS
    if _PATTERNS is not None:
        return _PATTERNS
    doc = loads((resource_dir("policies") / "redaction.json").read_bytes())
    schema.validate(doc, "redaction-policy.schema.json")
    patterns = []
    compiled = {}
    for item in doc["patterns"]:
        p = RedactionPattern(item["kind"], item["regex"], item["group"],
                             item["min_len"], item["min_entropy"])
        try:
            rx = re.compile(p.regex)
        except re.error as exc:
            raise QaError("redaction_policy_invalid", "pattern does not compile",
                          kind=p.kind, error=str(exc))
        if p.group > rx.groups:
            raise QaError("redaction_policy_invalid", "pattern group out of range",
                          kind=p.kind, group=p.group, groups=rx.groups)
        compiled[p.regex] = rx
        patterns.append(p)
    _COMPILED.update(compiled)
    _PATTERNS = patterns
    return patterns


def _compile(regex: str) -> "re.Pattern[str]":
    rx = _COMPILED.get(regex)
    if rx is None:
        rx = _COMPILED[regex] = re.compile(regex)
    return rx


class Redactor:
    def __init__(self, key: bytes, patterns: List[RedactionPattern], env_values: Dict[str, str]) -> None:
        self._key = key
        self._patterns = list(patterns)
        # Longest first, so a value that contains another is replaced whole.
        self._env = sorted({v for v in env_values.values() if v}, key=lambda v: (-len(v), v))

    def mask(self, kind: str, secret: str) -> str:
        """The marker for a value the caller knows is secret, whatever it looks like."""
        return marker(kind, secret, self._key)

    def text(self, s: str) -> Tuple[str, int]:
        parts: _Parts = []
        last = 0
        for m in _MARKER_RE.finditer(s):
            if m.start() > last:
                parts.append((s[last:m.start()], False))
            parts.append((m.group(0), True))
            last = m.end()
        if last < len(s):
            parts.append((s[last:], False))
        count = 0
        for value in self._env:
            parts, n = self._replace_literal(parts, value)
            count += n
        for p in self._patterns:
            parts, n = self._replace_pattern(parts, p)
            count += n
        return "".join(chunk for chunk, _ in parts), count

    def bytes_(self, b: bytes) -> Tuple[bytes, int]:
        out, n = self.text(b.decode("utf-8", "surrogateescape"))
        return out.encode("utf-8", "surrogateescape"), n

    def obj(self, o: Any) -> Tuple[Any, int]:
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            total = 0
            result = {}
            for k, v in o.items():
                result[k], n = self.obj(v)
                total += n
            return result, total
        if isinstance(o, (list, tuple)):
            items = [self.obj(v) for v in o]
            return type(o)(i for i, _ in items), sum(n for _, n in items)
        return o, 0

    def _replace_literal(self, parts: _Parts, value: str) -> Tuple[_Parts, int]:
        out: _Parts = []
        count = 0
        for chunk, is_marker in parts:
            if is_marker or value not in chunk:
                out.append((chunk, is_marker))
                continue
            pieces = chunk.split(value)
            for i, piece in enumerate(pieces):
                if i:
                    out.append((marker("env_value", value, self._key), True))
                    count += 1
                if piece:
                    out.append((piece, False))
        return out, count

    def _replace_pattern(self, parts: _Parts, p: RedactionPattern) -> Tuple[_Parts, int]:
        rx = _compile(p.regex)
        out: _Parts = []
        count = 0
        for chunk, is_marker in parts:
            if is_marker:
                out.append((chunk, True))
                continue
            last = 0
            for m in rx.finditer(chunk):
                secret = m.group(p.group)
                if secret is None or len(secret) < p.min_len:
                    continue
                if p.min_entropy is not None and shannon_entropy(secret) < p.min_entropy:
                    continue
                start, end = m.span(p.group)
                if start > last:
                    out.append((chunk[last:start], False))
                out.append((marker(p.kind, secret, self._key), True))
                count += 1
                last = end
            if last < len(chunk):
                out.append((chunk[last:], False))
        return out, count


def _env_values(environ: Mapping[str, str], extra_names: List[str]) -> Dict[str, str]:
    values = {}
    for name, value in environ.items():
        listed = name in extra_names
        by_name = any(fnmatch.fnmatchcase(name.upper(), g) for g in ENV_NAME_GLOBS)
        if (listed or by_name) and len(value) >= MIN_ENV_VALUE_LEN:
            values[name] = value
    return values


def load_redactor(ws: Workspace, environ: Optional[Mapping[str, str]] = None) -> Redactor:
    try:
        key = read_limited(ws.path("redaction.key"), _MAX_KEY_READ)
    except OSError as exc:
        raise QaError("redaction_key_invalid", "cannot read .qa/redaction.key", error=str(exc))
    if len(key) != KEY_BYTES:
        raise QaError("redaction_key_invalid", "redaction key must be exactly 32 bytes",
                      length=len(key))
    cfg = loads(read_config(ws))
    schema.validate(cfg, "config.schema.json")
    extra = cfg["redaction"]["extra_env_names"]
    env = os.environ if environ is None else environ
    return Redactor(key, load_patterns(), _env_values(env, extra))
