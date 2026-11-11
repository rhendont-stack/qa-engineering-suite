from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import math
import os
import re
from collections import Counter, namedtuple
from typing import Any, Dict, FrozenSet, Iterator, List, Mapping, Optional, Tuple

from . import resource_dir, schema, vocab
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
_CLOSED_WORDS: Optional[FrozenSet[str]] = None

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
        out, count, _found = self._scan(s)
        return out, count

    def _scan(self, s: str, literals: List[Tuple[str, str]] = ()) -> Tuple[str, int, List[Tuple[str, str]]]:
        """``s`` redacted, the number of replacements, and each ``(kind, secret)`` replaced, in order.

        ``literals`` are further ``(kind, value)`` pairs replaced wherever they are still in the clear, after the patterns."""
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
        found: List[Tuple[str, str]] = []
        for value in self._env:
            parts, n = self._replace_literal(parts, value, "env_value", found)
            count += n
        for p in self._patterns:
            parts, n = self._replace_pattern(parts, p, found)
            count += n
        for kind, value in literals:
            parts, n = self._replace_literal(parts, value, kind, found)
            count += n
        return "".join(chunk for chunk, _ in parts), count, found

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

    def _replace_literal(self, parts: _Parts, value: str, kind: str, found: List[Tuple[str, str]]) -> Tuple[_Parts, int]:
        out: _Parts = []
        count = 0
        for chunk, is_marker in parts:
            if is_marker or value not in chunk:
                out.append((chunk, is_marker))
                continue
            pieces = chunk.split(value)
            for i, piece in enumerate(pieces):
                if i:
                    out.append((marker(kind, value, self._key), True))
                    found.append((kind, value))
                    count += 1
                if piece:
                    out.append((piece, False))
        return out, count

    def _replace_pattern(self, parts: _Parts, p: RedactionPattern, found: List[Tuple[str, str]]) -> Tuple[_Parts, int]:
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
                found.append((p.kind, secret))
                count += 1
                last = end
            if last < len(chunk):
                out.append((chunk[last:], False))
        return out, count


    def json_document(self, data: bytes) -> Optional[Tuple[bytes, int]]:
        """A JSON document redacted value by value and serialised again, with the number of replacements; ``None`` when
        ``data`` is not one JSON document, or when the result could not be shown to hold back all the byte-level pass
        holds back (the caller then redacts the bytes as before).

        The byte-level pass reads the serialised text, so a match can run through a closing quote and leave a document
        that no longer parses. Here every key and string is redacted as text, with the same patterns, and every value the
        byte-level pass finds in the serialised text is also replaced wherever it occurs: in a key, in a string, or as the
        text of a number, ``true``, ``false`` or ``null`` (which becomes the marker string). A document with nothing to
        redact is returned byte for byte."""
        try:
            doc = loads(data)
        except QaError:
            return None
        _out, byte_count, byte_found = self._scan(data.decode("utf-8"))
        literals = _literal_forms(byte_found)
        if not _all_located(doc, byte_found, literals):
            return None
        state = {"count": 0}
        redacted = self._tree(doc, literals, state)
        if redacted is _COLLISION:
            return None
        if state["count"] == 0 and byte_count == 0:
            return data, 0
        try:
            text = json.dumps(redacted, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except UnicodeEncodeError:  # a lone surrogate the source escaped stays escaped
            text = json.dumps(redacted, ensure_ascii=True, allow_nan=False).encode("utf-8")
        return text, state["count"]

    def _tree(self, node: Any, literals: List[Tuple[str, str]], state: dict) -> Any:
        if isinstance(node, str):
            out, n, _found = self._scan(node, literals)
            state["count"] += n
            return out
        if isinstance(node, dict):
            result = {}
            for key, value in node.items():
                new_key = self._tree(key, literals, state)
                if new_key in result:  # two keys redacted to one: keep the byte-level route rather than lose a value
                    return _COLLISION
                result[new_key] = self._tree(value, literals, state)
                if result[new_key] is _COLLISION:
                    return _COLLISION
            return result
        if isinstance(node, list):
            items = [self._tree(item, literals, state) for item in node]
            return _COLLISION if any(item is _COLLISION for item in items) else items
        shown = _scalar_text(node)
        for kind, value in literals:
            if shown == value:
                state["count"] += 1
                return marker(kind, value, self._key)
        return node


_COLLISION = object()
_JSON_EDGES = "{}[],:"


def _scalar_text(node: Any) -> str:
    return json.dumps(node, allow_nan=False)


def _literal_forms(found: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Each secret the byte-level pass found, as written in the serialised text, without the structural characters a
    match may take along, and decoded from JSON's string escapes; longest first, so a value that contains another goes whole."""
    forms: Dict[Tuple[str, str], None] = {}
    for kind, secret in found:
        for form in _forms(secret):
            forms[(kind, form)] = None
    return sorted(forms, key=lambda item: (-len(item[1]), item))


def _forms(secret: str) -> List[str]:
    """A match in the serialised text can end on the backslash that escapes a quote: that backslash is the document's,
    not the value's, so a form without it is tried too."""
    out = [secret, secret.strip(_JSON_EDGES)]
    for text in list(out):
        trailing = len(text) - len(text.rstrip("\\"))
        candidates = [text] + ([text[:-1]] if trailing % 2 else [])
        for candidate in candidates:
            try:
                decoded = json.loads('"%s"' % candidate)
            except ValueError:
                continue
            if isinstance(decoded, str):
                out.append(decoded)
    return [form for form in dict.fromkeys(out) if form]


def _all_located(doc: Any, found: List[Tuple[str, str]], literals: List[Tuple[str, str]]) -> bool:
    """Whether every secret the byte-level pass found shows up, in one of its forms, in a key, a string or a scalar's text."""
    strings: List[str] = []
    scalars = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            strings.append(node)
        elif isinstance(node, dict):
            for key, value in node.items():
                strings.append(key)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        else:
            scalars.add(_scalar_text(node))

    walk(doc)
    for kind, secret in found:
        forms = _forms(secret)
        if not any(form in scalars or any(form in text for text in strings) for form in forms):
            return False
    return True


def _strings_in(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings_in(key)
            yield from _strings_in(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _strings_in(item)


def _enums_in(node: object) -> Iterator[str]:
    if isinstance(node, dict):
        if "enum" in node:
            yield from (item for item in node["enum"] if isinstance(item, str))
        if isinstance(node.get("const"), str):
            yield node["const"]
        for item in node.values():
            yield from _enums_in(item)
    elif isinstance(node, list):
        for item in node:
            yield from _enums_in(item)


def _closed_vocabulary() -> FrozenSet[str]:
    """Every word a closed field can hold: what the schemas enumerate, and what ``vocab`` names."""
    global _CLOSED_WORDS
    if _CLOSED_WORDS is None:
        words = {word for name in dir(vocab) if name.isupper() for word in _strings_in(getattr(vocab, name))}
        for path in sorted(resource_dir("schemas").rglob("*.json")):
            words.update(_enums_in(loads(path.read_bytes())))
        _CLOSED_WORDS = frozenset(words)
    return _CLOSED_WORDS


def _env_values(environ: Mapping[str, str], extra_names: List[str]) -> Dict[str, str]:
    """The values to mask. One that is part of a closed-vocabulary word stays out: masking it would make the event
    invalid against its own schema, and it names nothing a reader of the event could not already read in the schema.
    """
    closed = _closed_vocabulary()
    values = {}
    for name, value in environ.items():
        listed = name in extra_names
        by_name = any(fnmatch.fnmatchcase(name.upper(), g) for g in ENV_NAME_GLOBS)
        if (listed or by_name) and len(value) >= MIN_ENV_VALUE_LEN and not any(value in word for word in closed):
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
