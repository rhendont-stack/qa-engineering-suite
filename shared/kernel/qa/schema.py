from __future__ import annotations

import re
from typing import Any, Dict, List, Set, Tuple, Union

from . import resource_dir
from .canonical import loads
from .errors import QaError

# SchemaError codes: unsupported_keyword, bad_keyword, bad_pattern, bad_ref,
# ref_path_escape, ref_target_missing, ref_cycle, schema_not_found, schema_malformed.


class SchemaError(QaError):
    """The schema itself is unusable: unsupported keyword, bad ``$ref`` or unreadable file."""


SUPPORTED_KEYWORDS = frozenset([
    "$schema", "$id", "$ref", "$defs", "$comment", "title", "description", "default", "examples",
    "type", "enum", "const",
    "properties", "required", "additionalProperties", "patternProperties", "propertyNames",
    "minProperties", "maxProperties",
    "items", "minItems", "maxItems", "uniqueItems", "contains",
    "minLength", "maxLength", "pattern", "format",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "allOf", "anyOf", "oneOf", "not", "if", "then", "else",
    "dependentRequired",
])

_TYPES = ("null", "boolean", "object", "array", "number", "integer", "string")
_REF_RE = re.compile(r"^(?:(?P<path>[^#]+))?#/\$defs/(?P<name>[A-Za-z0-9_.-]+)$")
_SINGLE = ("additionalProperties", "propertyNames", "items", "contains", "not", "if", "then", "else")
_LISTS = ("allOf", "anyOf", "oneOf")
_MAPS = ("properties", "patternProperties", "$defs")

Schema = Union[Dict[str, Any], bool]

_RAW: Dict[str, Any] = {}
_CHECKED: Set[str] = set()
_PATTERNS: Dict[str, "re.Pattern[str]"] = {}


# --------------------------------------------------------------------------- loading

def _safe_path(path: str) -> None:
    parts = path.split("/")
    if (not path or path.startswith("/") or "\\" in path or ":" in path
            or any(p in ("", ".", "..") for p in parts)):
        raise SchemaError("ref_path_escape", "schema path must stay inside the schemas directory", path=path)


def _raw(name: str) -> Any:
    """Parse ``name`` (relative to the schemas dir) without checking its keywords."""
    if name in _RAW:
        return _RAW[name]
    _safe_path(name)
    root = resource_dir("schemas").resolve()
    file = (root / name).resolve()
    try:
        file.relative_to(root)
    except ValueError:
        raise SchemaError("ref_path_escape", "schema path leaves the schemas directory", path=name)
    if not file.is_file():
        raise SchemaError("schema_not_found", "no such schema file", name=name)
    try:
        doc = loads(file.read_bytes())
    except QaError as exc:
        raise SchemaError("schema_malformed", "schema file is not valid JSON", name=name, cause=exc.code)
    if not isinstance(doc, dict):
        raise SchemaError("schema_malformed", "schema document must be an object", name=name)
    _RAW[name] = doc
    return doc


def load_schema(name: str) -> dict:
    """Load and statically check a schema file; cached."""
    doc = _raw(name)
    if name not in _CHECKED:
        _check(doc, name, doc, "")
        _CHECKED.add(name)
    return doc


# --------------------------------------------------------------------------- patterns

def _translate(pattern: str) -> str:
    """Rewrite an unescaped ``$`` outside a character class to ``\\Z`` (ECMA-262 end of input)."""
    out: List[str] = []
    in_class = False
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            out.append(pattern[i:i + 2])
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
        elif c == "$":
            c = "\\Z"
        out.append(c)
        i += 1
    return "".join(out)


def _compile(pattern: str, where: str) -> "re.Pattern[str]":
    compiled = _PATTERNS.get(pattern)
    if compiled is None:
        try:
            compiled = re.compile(_translate(pattern))
        except re.error as exc:
            raise SchemaError("bad_pattern", str(exc), pattern=pattern, at=where)
        _PATTERNS[pattern] = compiled
    return compiled


# --------------------------------------------------------------------------- $ref and static check

def _resolve(ref: Any, base: str, root: Any, load: Any, where: str) -> Tuple[Schema, str, Any, str]:
    """Return (subschema, new base, new root, def name) for ``ref``."""
    m = _REF_RE.match(ref) if isinstance(ref, str) else None
    if m is None:
        raise SchemaError("bad_ref", "$ref must be '#/$defs/<name>' or '<path>.schema.json#/$defs/<name>'",
                          ref=ref, at=where)
    path, name = m.group("path"), m.group("name")
    if path is not None:
        if not path.endswith(".schema.json"):
            raise SchemaError("bad_ref", "$ref path must end in .schema.json", ref=ref, at=where)
        _safe_path(path)
        base, root = path, load(path)
    elif base != "":
        root = load(base)
    defs = root.get("$defs") if isinstance(root, dict) else None
    if not isinstance(defs, dict) or name not in defs:
        raise SchemaError("ref_target_missing", "definition not found", ref=ref, at=where)
    return defs[name], base, root, name


def _check(node: Any, base: str, root: Any, where: str) -> None:
    """Statically check a whole (sub)schema: keywords, patterns, types and $ref targets."""
    if isinstance(node, bool):
        return
    if not isinstance(node, dict):
        raise SchemaError("bad_keyword", "a schema must be an object or a boolean", at=where)
    for key in node:
        if key not in SUPPORTED_KEYWORDS:
            raise SchemaError("unsupported_keyword", "unsupported keyword %r" % key, keyword=key, at=where)
    if "$ref" in node:
        _resolve(node["$ref"], base, root, _raw, where)
    if "type" in node:
        t = node["type"]
        ts = t if isinstance(t, list) else [t]
        if not ts or any(not isinstance(x, str) or x not in _TYPES for x in ts):
            raise SchemaError("bad_keyword", "invalid 'type'", at=where)
    if "pattern" in node:
        if not isinstance(node["pattern"], str):
            raise SchemaError("bad_keyword", "'pattern' must be a string", at=where)
        _compile(node["pattern"], where)
    for key in _SINGLE:
        if key in node:
            _check(node[key], base, root, "%s/%s" % (where, key))
    for key in _LISTS:
        if key in node:
            if not isinstance(node[key], list) or not node[key]:
                raise SchemaError("bad_keyword", "%r must be a non-empty array" % key, at=where)
            for i, sub in enumerate(node[key]):
                _check(sub, base, root, "%s/%s/%d" % (where, key, i))
    for key in _MAPS:
        if key in node:
            if not isinstance(node[key], dict):
                raise SchemaError("bad_keyword", "%r must be an object" % key, at=where)
            for name, sub in node[key].items():
                if key == "patternProperties":
                    _compile(name, "%s/%s" % (where, key))
                _check(sub, base, root, "%s/%s/%s" % (where, key, name))


# --------------------------------------------------------------------------- validation

def _pointer(path: Tuple[Any, ...]) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in path)


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _is_type(x: Any, t: str) -> bool:
    if t == "null":
        return x is None
    if t == "boolean":
        return isinstance(x, bool)
    if t == "object":
        return isinstance(x, dict)
    if t == "array":
        return isinstance(x, list)
    if t == "string":
        return isinstance(x, str)
    if t == "number":
        return _is_number(x)
    # integer
    return (isinstance(x, int) and not isinstance(x, bool)) or (isinstance(x, float) and x.is_integer())


def _eq(a: Any, b: Any) -> bool:
    """JSON equality: booleans differ from numbers, 1 equals 1.0."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, list):
        return isinstance(b, list) and len(a) == len(b) and all(_eq(x, y) for x, y in zip(a, b))
    if isinstance(a, dict):
        return (isinstance(b, dict) and set(a) == set(b) and all(_eq(a[k], b[k]) for k in a))
    if isinstance(b, (list, dict)):
        return False
    return a == b


def _multiple_of(x: Any, d: Any) -> bool:
    if isinstance(d, float):
        q = x / d
        try:
            return int(q) == q
        except (OverflowError, ValueError):
            return False
    return not (x % d)


def _validate(inst: Any, sch: Schema, base: str, root: Any, path: Tuple[Any, ...],
              active: Set[Tuple[Any, ...]]) -> List[str]:
    here = _pointer(path)
    if sch is True:
        return []
    if sch is False:
        return ["%s: schema is false, nothing is valid" % here]
    errs: List[str] = []

    def sub(i: Any, s: Schema, p: Tuple[Any, ...]) -> List[str]:
        return _validate(i, s, base, root, p, active)

    if "$ref" in sch:
        target, nbase, nroot, name = _resolve(sch["$ref"], base, root, load_schema, here)
        key = (nbase, name, path)
        if key in active:
            raise SchemaError("ref_cycle", "$ref cycle without consuming input", ref=sch["$ref"], at=here)
        active.add(key)
        try:
            errs.extend(_validate(inst, target, nbase, nroot, path, active))
        finally:
            active.discard(key)

    if "type" in sch:
        t = sch["type"]
        ts = t if isinstance(t, list) else [t]
        if not any(_is_type(inst, x) for x in ts):
            errs.append("%s: expected %s" % (here, " or ".join(ts)))
    if "enum" in sch and not any(_eq(inst, v) for v in sch["enum"]):
        errs.append("%s: not one of the allowed values" % here)
    if "const" in sch and not _eq(inst, sch["const"]):
        errs.append("%s: does not equal the required constant" % here)

    if _is_number(inst):
        if "minimum" in sch and inst < sch["minimum"]:
            errs.append("%s: less than minimum %s" % (here, sch["minimum"]))
        if "maximum" in sch and inst > sch["maximum"]:
            errs.append("%s: greater than maximum %s" % (here, sch["maximum"]))
        if "exclusiveMinimum" in sch and inst <= sch["exclusiveMinimum"]:
            errs.append("%s: not greater than %s" % (here, sch["exclusiveMinimum"]))
        if "exclusiveMaximum" in sch and inst >= sch["exclusiveMaximum"]:
            errs.append("%s: not less than %s" % (here, sch["exclusiveMaximum"]))
        if "multipleOf" in sch and not _multiple_of(inst, sch["multipleOf"]):
            errs.append("%s: not a multiple of %s" % (here, sch["multipleOf"]))

    if isinstance(inst, str):
        if "minLength" in sch and len(inst) < sch["minLength"]:
            errs.append("%s: shorter than %s characters" % (here, sch["minLength"]))
        if "maxLength" in sch and len(inst) > sch["maxLength"]:
            errs.append("%s: longer than %s characters" % (here, sch["maxLength"]))
        if "pattern" in sch and not _compile(sch["pattern"], here).search(inst):
            errs.append("%s: does not match pattern %s" % (here, sch["pattern"]))

    if isinstance(inst, list):
        if "minItems" in sch and len(inst) < sch["minItems"]:
            errs.append("%s: fewer than %s items" % (here, sch["minItems"]))
        if "maxItems" in sch and len(inst) > sch["maxItems"]:
            errs.append("%s: more than %s items" % (here, sch["maxItems"]))
        if sch.get("uniqueItems") is True:
            for j in range(len(inst)):
                if any(_eq(inst[j], inst[k]) for k in range(j)):
                    errs.append("%s: items are not unique" % here)
                    break
        if "items" in sch:
            for j, item in enumerate(inst):
                errs.extend(sub(item, sch["items"], path + (j,)))
        if "contains" in sch and not any(not sub(item, sch["contains"], path + (j,))
                                         for j, item in enumerate(inst)):
            errs.append("%s: no item matches 'contains'" % here)

    if isinstance(inst, dict):
        if "minProperties" in sch and len(inst) < sch["minProperties"]:
            errs.append("%s: fewer than %s properties" % (here, sch["minProperties"]))
        if "maxProperties" in sch and len(inst) > sch["maxProperties"]:
            errs.append("%s: more than %s properties" % (here, sch["maxProperties"]))
        for name in sch.get("required", ()):
            if name not in inst:
                errs.append("%s: missing required property %r" % (here, name))
        for name, deps in sch.get("dependentRequired", {}).items():
            if name in inst:
                for dep in deps:
                    if dep not in inst:
                        errs.append("%s: property %r requires %r" % (here, name, dep))
        props = sch.get("properties", {})
        pats = [(_compile(p, here), s) for p, s in sch.get("patternProperties", {}).items()]
        for key, value in inst.items():
            matched = False
            if key in props:
                matched = True
                errs.extend(sub(value, props[key], path + (key,)))
            for rx, s in pats:
                if rx.search(key):
                    matched = True
                    errs.extend(sub(value, s, path + (key,)))
            if not matched and "additionalProperties" in sch:
                extra = sch["additionalProperties"]
                if extra is False:
                    errs.append("%s: additional property %r is not allowed" % (here, key))
                else:
                    errs.extend(sub(value, extra, path + (key,)))
        if "propertyNames" in sch:
            for key in inst:
                if sub(key, sch["propertyNames"], path):
                    errs.append("%s: property name %r is invalid" % (here, key))

    if "allOf" in sch:
        for s in sch["allOf"]:
            errs.extend(sub(inst, s, path))
    if "anyOf" in sch and not any(not sub(inst, s, path) for s in sch["anyOf"]):
        errs.append("%s: does not match any of 'anyOf'" % here)
    if "oneOf" in sch:
        n = sum(1 for s in sch["oneOf"] if not sub(inst, s, path))
        if n != 1:
            errs.append("%s: matches %d of 'oneOf', expected exactly 1" % (here, n))
    if "not" in sch and not sub(inst, sch["not"], path):
        errs.append("%s: matches a schema it must not match" % here)
    if "if" in sch:
        branch = "then" if not sub(inst, sch["if"], path) else "else"
        if branch in sch:
            errs.extend(sub(inst, sch[branch], path))
    return errs


def errors(instance: Any, schema: Schema, *, base: str = "") -> List[str]:
    """Validate ``instance``; return messages (each starts with a JSON pointer), empty if valid.

    ``base`` names the schema file, relative to the schemas dir, that ``#/$defs/...`` fragments resolve
    against. The empty string means ``schema`` is its own document root.
    """
    root = schema if base == "" else load_schema(base)
    _check(schema, base, root, "")
    return _validate(instance, schema, base, root, (), set())


def validate(instance: Any, name: str) -> None:
    """Validate against a schema file, or ``<file>#/$defs/<name>``; raise ``QaError("schema_invalid")``."""
    if "#" in name:
        errs = errors(instance, {"$ref": name})
    else:
        doc = load_schema(name)
        errs = _validate(instance, doc, name, doc, (), set())
    if errs:
        raise QaError("schema_invalid", "%d schema violation(s); first: %s" % (len(errs), errs[0]), errors=errs)
