"""Lists the settings in an Android or iOS app's configuration files that weaken its security, as SARIF or JSON results with
OWASP MAS references.

Usage: python3 mobile_static.py --root DIR [--out PATH] [--format sarif|json]
       python3 mobile_static.py --copy-to-work

It reads, under ``DIR`` (no link is followed; dependency, build and tool directories are skipped):

* Android: every ``AndroidManifest.xml``; every network security configuration (an ``*.xml`` file directly in a
  ``res/xml*`` directory whose root element is ``network-security-config``); the SDK levels of the nearest ``build.gradle`` or
  ``build.gradle.kts`` above each manifest (``minSdk``, ``minSdkVersion``, ``targetSdk``, ``targetSdkVersion`` written as
  numbers), else the manifest's ``uses-sdk``. The SDK levels are approximate: read as text, with variables, flavours, build
  types and the manifest merger not applied.
* iOS: every ``Info.plist`` (also ``*-Info.plist``) and every ``*.entitlements`` property list, through ``plistlib``; the
  line of each key is read from the same XML.

Rules. Each result names the MASVS control, the MASTG test, the MASWE weakness and the CWE its rule maps to:

* ``cleartext-traffic`` (Android; MASVS-NETWORK-1, MASTG-TEST-0235, MASWE-0026, CWE-319): a network security configuration
  whose ``base-config`` or a ``domain-config`` sets ``cleartextTrafficPermitted="true"``; an ``application`` that sets
  ``usesCleartextTraffic="true"`` and names no ``networkSecurityConfig`` (with one, that flag is ignored); an ``application``
  with neither and a target SDK below 28, where cleartext is permitted by default (approximate).
* ``exported-without-permission`` (Android; MASVS-PLATFORM-1, MASWE-0018, CWE-926; MASTG-TEST-0364 for activities and
  aliases, MASTG-TEST-0365 for services, MASTG-TEST-0366 for receivers, MASTG-TEST-0355 for providers): a component exported
  with no ``android:permission`` (a provider: no ``permission``, ``readPermission`` or ``writePermission``), or with a
  permission this manifest declares at a protection level other than ``signature`` (approximate). A component with an intent
  filter and no ``exported`` attribute counts as exported when the target SDK is below 31 or was not read (approximate). The
  launcher activity (an intent filter with the ``MAIN`` action and the ``LAUNCHER`` category) is exported by design and is
  not listed. A permission declared elsewhere (a platform permission, another app's) is not judged: it goes to ``notes``.
* ``backup-enabled`` (Android; MASVS-STORAGE-2, MASTG-TEST-0262, MASWE-0006, CWE-212): an ``application`` whose
  ``allowBackup`` is ``true`` or absent (the default is ``true``) and that declares neither ``fullBackupContent`` (read up to
  Android 11) nor ``dataExtractionRules`` (Android 12 and later); with only one of them, a result when the SDK levels say the
  other one applies (approximate). Whether declared rules exclude the sensitive files is not read here.
* ``debuggable`` (Android; MASVS-RESILIENCE-4, MASVS-PLATFORM-2, MASTG-TEST-0226, MASWE-0063, CWE-489): an ``application``
  with ``android:debuggable="true"``. A ``debuggable`` setting in ``build.gradle`` is not read.
* ``ats-arbitrary-loads`` (iOS; MASVS-NETWORK-1, MASTG-TEST-0322, MASWE-0026, CWE-319): ``NSAppTransportSecurity`` with
  ``NSAllowsArbitraryLoadsInWebContent`` or ``NSAllowsArbitraryLoadsForMedia`` true, or with ``NSAllowsArbitraryLoads`` true
  when none of those two keys nor ``NSAllowsLocalNetworking`` is present (iOS 10 and later ignore it beside them).
* ``ats-insecure-exception`` (iOS; the same references): ``NSAllowsLocalNetworking`` true, or a domain under
  ``NSExceptionDomains`` with ``NSExceptionAllowsInsecureHTTPLoads`` true.
* ``get-task-allow`` (iOS; MASVS-RESILIENCE-4, MASVS-PLATFORM-2, MASTG-TEST-0261, MASWE-0063, CWE-489): an entitlements file
  with ``get-task-allow`` true.

Every result is a lead read from source files, not from a build: the merged manifest, the signed entitlements and what the
app sends are decided elsewhere. ``properties.approximate`` is true where a result also rests on an SDK level or a protection
level read here.

Hostile input. XML is read with ``xml.parsers.expat`` and handlers that refuse any DOCTYPE except Apple's public plist
identifier without an internal subset (and that one only in a property list), any entity declaration, any reference to an
undeclared entity, any external entity and elements nested deeper than 256 levels. A file that one of them refuses, that is
over 1 MiB, is a link, is not a regular file, resolves outside ``DIR``, is not well-formed or is not text where text is read
is listed ``unreadable`` with its reason, and nothing is reported from it; so is a file whose reading fails in any other way
(the error's type is the reason), and the scan goes on with the other files. Trees are walked without recursion, and an SDK
level is a number only when written in ASCII digits (at most nine).

Paths are relative to the current directory when the file lies under it (run it from the project root, so they are the
project's own paths), absolute otherwise. ``--out PATH`` writes the report to a new file under ``.qa/work/mobile/`` or,
inside a container run, ``/qa-evidence/``; without it nothing is written. ``--format`` picks SARIF 2.1.0 (the default) or
this script's JSON. ``--copy-to-work`` writes a copy of this script to ``.qa/work/mobile/mobile_static.py`` for a container
run's ``--input``. Files are written by exclusive create through no link. Prints one JSON summary. Exit 0 when the scan ran
(results or not), 2 on bad arguments or a refused path. Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import secrets
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Sequence, Tuple
from xml.parsers import expat

VERSION = "0.1.0"
DRIVER = "qa-mobile-static"
WORK_DIR = ".qa/work/mobile"
EVIDENCE_DIR = "/qa-evidence"
MAX_BYTES = 1024 * 1024
MAX_DEPTH = 256
KEY_DEPTH = 4  # the deepest key path a rule reads: NSAppTransportSecurity, NSExceptionDomains, a domain, its key
APPLE_PUBLIC_ID = "-//Apple//DTD PLIST 1.0//EN"
ANDROID_NS = "http://schemas.android.com/apk/res/android"
SKIP_DIRS = {".git", ".qa", ".qa-input", ".claude", "node_modules", "__pycache__", "vendor", ".venv", "venv", "dist", "build",
             ".gradle", ".idea", "Pods", "DerivedData", "coverage", ".dart_tool", "target", "bower_components"}
NETWORK = {"masvs": ["MASVS-NETWORK-1"], "maswe": ["MASWE-0026"], "cwe": ["CWE-319"]}
DEBUG = {"masvs": ["MASVS-RESILIENCE-4", "MASVS-PLATFORM-2"], "maswe": ["MASWE-0063"], "cwe": ["CWE-489"]}
RULES: Dict[str, Dict[str, Any]] = {
    "cleartext-traffic": dict(NETWORK, platform="android", mastg=["MASTG-TEST-0235"],
                              text="The Android configuration permits cleartext traffic"),
    "exported-without-permission": {"platform": "android", "masvs": ["MASVS-PLATFORM-1"], "maswe": ["MASWE-0018"], "cwe": ["CWE-926"],
                                    "mastg": ["MASTG-TEST-0364", "MASTG-TEST-0365", "MASTG-TEST-0366", "MASTG-TEST-0355"],
                                    "text": "An Android component is exported without a permission that restricts its callers"},
    "backup-enabled": {"platform": "android", "masvs": ["MASVS-STORAGE-2"], "maswe": ["MASWE-0006"], "cwe": ["CWE-212"],
                       "mastg": ["MASTG-TEST-0262"], "text": "App data is eligible for backup with no exclusion rules"},
    "debuggable": dict(DEBUG, platform="android", mastg=["MASTG-TEST-0226"], text="The Android application is debuggable"),
    "ats-arbitrary-loads": dict(NETWORK, platform="ios", mastg=["MASTG-TEST-0322"],
                                text="App Transport Security allows arbitrary loads"),
    "ats-insecure-exception": dict(NETWORK, platform="ios", mastg=["MASTG-TEST-0322"],
                                   text="App Transport Security has an exception that allows insecure HTTP loads"),
    "get-task-allow": dict(DEBUG, platform="ios", mastg=["MASTG-TEST-0261"],
                           text="The entitlements allow a debugger to attach (get-task-allow)"),
}
COMPONENT_TEST = {"activity": "MASTG-TEST-0364", "activity-alias": "MASTG-TEST-0364", "service": "MASTG-TEST-0365",
                  "receiver": "MASTG-TEST-0366", "provider": "MASTG-TEST-0355"}
ATS_FINE = ("NSAllowsArbitraryLoadsInWebContent", "NSAllowsArbitraryLoadsForMedia", "NSAllowsLocalNetworking")
SDK_LINE = re.compile(r"\b(minSdk|minSdkVersion|targetSdk|targetSdkVersion)\b\s*(?:=\s*|\(\s*)?([0-9]{1,9})\b")
APPROXIMATE = " (approximate: it rests on a level read from the files, which a build may resolve differently)"


class Refused(Exception):
    pass


class Unreadable(Exception):
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
    """Write ``data`` to ``text``: a new file directly in ``/qa-evidence/`` (a container run), or under ``.qa/work/mobile/``
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
    if parts[:3] != (".qa", "work", "mobile") or len(parts) < 4 or any(p in ("..", ".") for p in parts):
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


def kind_of(path: Path) -> Optional[str]:
    name = path.name
    if name == "AndroidManifest.xml":
        return "manifest"
    if name in ("build.gradle", "build.gradle.kts"):
        return "gradle"
    if name == "Info.plist" or name.endswith("-Info.plist"):
        return "plist"
    if name.endswith(".entitlements"):
        return "entitlements"
    if name.endswith(".xml") and path.parent.name.startswith("xml") and path.parent.parent.name == "res":
        return "res-xml"
    return None


def project_files(root: Path, unreadable: List[Dict[str, str]]) -> List[Tuple[Path, str]]:
    """``(path, kind)`` of every configuration file under ``root``, sorted; links, irregular, oversized and outside files go
    to ``unreadable``."""
    real_root = os.path.realpath(str(root))
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
            path = Path(here) / name
            kind = kind_of(path)
            if kind is None:
                continue
            info = os.lstat(str(path))
            real = os.path.realpath(str(path))
            if stat.S_ISLNK(info.st_mode):
                unreadable.append({"path": uri_of(path), "reason": "a link, not followed"})
            elif not stat.S_ISREG(info.st_mode):
                unreadable.append({"path": uri_of(path), "reason": "not a regular file"})
            elif info.st_size > MAX_BYTES:
                unreadable.append({"path": uri_of(path), "reason": "over 1 MiB"})
            elif os.path.commonpath([real_root, real]) != real_root:
                unreadable.append({"path": uri_of(path), "reason": "resolves outside the root"})
            else:
                found.append((path, kind))
    return sorted(found)


def read_regular(path: Path) -> bytes:
    """The bytes of a regular file, opened through no link and read to at most 1 MiB."""
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise Unreadable("not opened: %s" % (exc.strerror or type(exc).__name__))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise Unreadable("not a regular file")
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise Unreadable("over 1 MiB")
    return data


# --------------------------------------------------------------------------- XML through expat, refusing what a config never needs

class Node:
    __slots__ = ("tag", "attrs", "line", "children", "text")

    def __init__(self, tag: str, attrs: Dict[str, str], line: int):
        self.tag, self.attrs, self.line = tag, attrs, line
        self.children: List["Node"] = []
        self.text: List[str] = []

    def android(self, name: str) -> Optional[str]:
        return self.attrs.get("android:" + name)

    def all(self, tag: str) -> List["Node"]:
        return [c for c in self.children if c.tag == tag]


def _name(raw: str) -> str:
    if " " not in raw:
        return raw
    namespace, local = raw.split(" ", 1)
    return "android:" + local if namespace == ANDROID_NS else "{%s}%s" % (namespace, local)


def parse_xml(data: bytes, plist: bool) -> Node:
    """The element tree of ``data``; ``Unreadable`` with the reason for anything a configuration file never needs."""
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    stack: List[Node] = []
    top: List[Node] = []

    def doctype(name: str, system_id: Optional[str], public_id: Optional[str], has_internal_subset: int) -> None:
        if not plist:
            raise Unreadable("a DOCTYPE declaration: only a property list may carry one, Apple's public plist DOCTYPE")
        if public_id != APPLE_PUBLIC_ID:
            raise Unreadable("a DOCTYPE other than Apple's public plist identifier")
        if has_internal_subset:
            raise Unreadable("a DOCTYPE with an internal subset, where entities and attribute defaults are declared")

    def entity(*_args: Any) -> None:
        raise Unreadable("an entity declaration")

    def skipped(*_args: Any) -> None:
        raise Unreadable("a reference to an undeclared entity")

    def external(*_args: Any) -> int:
        raise Unreadable("an external entity reference")

    def start(name: str, attrs: Dict[str, str]) -> None:
        if len(stack) >= MAX_DEPTH:
            raise Unreadable("elements nested deeper than %d levels" % MAX_DEPTH)
        node = Node(_name(name), {_name(k): v for k, v in attrs.items()}, parser.CurrentLineNumber)
        (stack[-1].children if stack else top).append(node)
        stack.append(node)

    def end(_name_: str) -> None:
        stack.pop()

    def text(chunk: str) -> None:
        if stack:
            stack[-1].text.append(chunk)

    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = entity
    parser.UnparsedEntityDeclHandler = entity
    parser.SkippedEntityHandler = skipped
    parser.ExternalEntityRefHandler = external
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise Unreadable("not well-formed XML: %s at line %d" % (expat.ErrorString(exc.code), exc.lineno))
    if len(top) != 1:
        raise Unreadable("no root element")
    return top[0]


def plist_key_lines(root: Node) -> Dict[Tuple[Any, ...], int]:
    """The line of each ``<key>`` of an XML property list, by its path of keys (and array indexes), to ``KEY_DEPTH`` deep;
    walked without recursion, in document order level by level, so a repeated key keeps its last line as ``plistlib`` keeps
    its last value."""
    found: Dict[Tuple[Any, ...], int] = {}
    queue: List[Tuple[Node, Tuple[Any, ...]]] = [(root, ())]
    at = 0
    while at < len(queue):
        node, path = queue[at]
        at += 1
        if len(path) >= KEY_DEPTH:
            continue
        if node.tag in ("plist", "array"):
            for index, child in enumerate(node.children):
                queue.append((child, path if node.tag == "plist" else path + (index,)))
        elif node.tag == "dict":
            children = node.children
            for index, child in enumerate(children):
                if child.tag == "key":
                    key = path + ("".join(child.text),)
                    found[key] = child.line
                    if index + 1 < len(children) and children[index + 1].tag != "key":
                        queue.append((children[index + 1], key))
    return found


def read_plist(data: bytes) -> Tuple[Dict[str, Any], Dict[Tuple[Any, ...], int], bool]:
    """``(values, key lines, binary)`` of a property list; ``Unreadable`` when it is refused or not a dictionary list."""
    binary = data.startswith(b"bplist00")
    lines: Dict[Tuple[Any, ...], int] = {}
    if not binary:
        lines = plist_key_lines(parse_xml(data, plist=True))
    try:
        values = plistlib.loads(data)
    except Exception as exc:  # plistlib raises several types for a malformed list
        raise Unreadable("not a property list: %s" % type(exc).__name__)
    if not isinstance(values, dict):
        raise Unreadable("the property list is not a dictionary")
    return values, lines, binary


# --------------------------------------------------------------------------- results

def result(rule: str, uri: str, line: int, message: str, approximate: bool = False, mastg: Optional[List[str]] = None) -> Dict[str, Any]:
    info = RULES[rule]
    return {"rule": rule, "path": uri, "line": line, "message": message + (APPROXIMATE if approximate else ""), "approximate": approximate,
            "platform": info["platform"], "masvs": list(info["masvs"]), "mastg": list(mastg or info["mastg"]),
            "maswe": list(info["maswe"]), "cwe": list(info["cwe"])}


def gradle_levels(text: str) -> Dict[str, Optional[int]]:
    clean = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    clean = "\n".join(re.sub(r"(^|\s)//.*$", "", line) for line in clean.splitlines())
    levels: Dict[str, Optional[int]] = {"min": None, "target": None}
    for name, number in SDK_LINE.findall(clean):
        key = "min" if name.startswith("min") else "target"
        if levels[key] is None:
            levels[key] = int(number)
    return levels


def sdk_for(manifest: Path, root: Path, tree: Node, gradles: Dict[Path, Dict[str, Optional[int]]]) -> Dict[str, Any]:
    """The SDK levels that apply to a manifest: the nearest build file above it, else its ``uses-sdk``."""
    here = manifest.parent
    stop = Path(os.path.abspath(str(root)))
    while True:
        for name in ("build.gradle.kts", "build.gradle"):
            candidate = Path(os.path.abspath(str(here / name)))
            if candidate in gradles:
                levels = gradles[candidate]
                return {"manifest": uri_of(manifest), "min_sdk": levels["min"], "target_sdk": levels["target"],
                        "from": uri_of(candidate), "approximate": True}
        if Path(os.path.abspath(str(here))) == stop or here.parent == here:
            break
        here = here.parent
    uses = tree.all("uses-sdk")

    def level(name: str) -> Optional[int]:
        value = (uses[0].android(name) or "").strip() if uses else ""
        return int(value) if value.isascii() and value.isdecimal() and len(value) <= 9 else None
    return {"manifest": uri_of(manifest), "min_sdk": level("minSdkVersion"), "target_sdk": level("targetSdkVersion"),
            "from": uri_of(manifest) if uses else None, "approximate": True}


def _launcher(component: Node) -> bool:
    for intent in component.all("intent-filter"):
        actions = {a.android("name") for a in intent.all("action")}
        categories = {c.android("name") for c in intent.all("category")}
        if "android.intent.action.MAIN" in actions and "android.intent.category.LAUNCHER" in categories:
            return True
    return False


def _strong(level: Optional[str]) -> bool:
    return bool(level) and bool({p.strip() for p in str(level).split("|")} & {"signature", "signatureOrSystem"})


def manifest_results(uri: str, tree: Node, sdk: Dict[str, Any], nsc_found: Optional[bool], notes: List[str]) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    if tree.tag != "manifest":
        notes.append("%s: the root element is %s, not manifest; not read" % (uri, tree.tag))
        return found
    target, minimum = sdk["target_sdk"], sdk["min_sdk"]
    declared = {p.android("name"): p.android("protectionLevel") for p in tree.all("permission") if p.android("name")}
    for app in tree.all("application"):
        where = "%s:%d" % (uri, app.line)
        if (app.android("debuggable") or "").strip().lower() == "true":
            found.append(result("debuggable", uri, app.line, '<application android:debuggable="true">: the app is debuggable'))
        nsc = app.android("networkSecurityConfig")
        if nsc and nsc_found is False:
            notes.append("%s: networkSecurityConfig %s names a file that was not read" % (where, nsc))
        cleartext = (app.android("usesCleartextTraffic") or "").strip().lower()
        if cleartext == "true" and not nsc:
            found.append(result("cleartext-traffic", uri, app.line,
                                '<application android:usesCleartextTraffic="true"> with no networkSecurityConfig: cleartext traffic is permitted'))
        elif not cleartext and not nsc and target is not None and target < 28:
            found.append(result("cleartext-traffic", uri, app.line,
                                "<application> sets neither usesCleartextTraffic nor networkSecurityConfig, and targetSdk %d is below 28, "
                                "where cleartext traffic is permitted by default" % target, approximate=True))
        backup = (app.android("allowBackup") or "").strip().lower()
        if backup != "false":
            stated = 'android:allowBackup="true"' if backup == "true" else "no android:allowBackup (the default is true)"
            full, extraction = app.android("fullBackupContent"), app.android("dataExtractionRules")
            if not full and not extraction:
                found.append(result("backup-enabled", uri, app.line,
                                    "<application> has %s and declares neither fullBackupContent nor dataExtractionRules: all app data "
                                    "is eligible for backup" % stated))
            elif extraction and not full and (minimum is None or minimum < 31):
                found.append(result("backup-enabled", uri, app.line,
                                    "<application> has %s and only dataExtractionRules, which Android 12 and later read; minSdk %s "
                                    "admits Android 11 or earlier, which reads fullBackupContent" % (stated, minimum if minimum is not None else "not read"),
                                    approximate=True))
            elif full and not extraction and (target is None or target >= 31):
                found.append(result("backup-enabled", uri, app.line,
                                    "<application> has %s and only fullBackupContent; with targetSdk %s, Android 12 and later read "
                                    "dataExtractionRules, which is not declared" % (stated, target if target is not None else "not read"),
                                    approximate=True))
        for component in app.children:
            test = COMPONENT_TEST.get(component.tag)
            if test is None or _launcher(component):
                continue
            name = component.android("name") or "(unnamed)"
            exported = (component.android("exported") or "").strip().lower()
            implicit = False
            if exported == "false":
                continue
            if exported != "true":
                if not component.all("intent-filter") or (target is not None and target >= 31):
                    continue
                implicit = True
            if component.tag == "provider":
                guards = [component.android(k) for k in ("permission", "readPermission", "writePermission") if component.android(k)]
            else:
                guards = [component.android("permission")] if component.android("permission") else []
            how = ("has an intent filter and no android:exported, so it is exported with targetSdk %s"
                   % (target if target is not None else "not read")) if implicit else 'is android:exported="true"'
            if not guards:
                found.append(result("exported-without-permission", uri, component.line,
                                    "<%s %s> %s and declares no permission: any app can reach it" % (component.tag, name, how),
                                    approximate=implicit, mastg=[test]))
                continue
            weak = [g for g in guards if g in declared and not _strong(declared[g])]
            elsewhere = [g for g in guards if g not in declared]
            if weak:
                found.append(result("exported-without-permission", uri, component.line,
                                    "<%s %s> %s and its permission %s is declared here at protection level %s, which any app can be granted"
                                    % (component.tag, name, how, weak[0], declared[weak[0]] or "normal (none set)"),
                                    approximate=True, mastg=[test]))
            elif elsewhere:
                notes.append("%s:%d: <%s %s> is protected by %s, declared outside this manifest; its protection level is not read"
                             % (uri, component.line, component.tag, name, ", ".join(elsewhere)))
    return found


def descendants(node: Node) -> List[Node]:
    """Every element below ``node``, in document order, walked without recursion."""
    found: List[Node] = []
    stack = list(reversed(node.children))
    while stack:
        child = stack.pop()
        found.append(child)
        stack.extend(reversed(child.children))
    return found


def nsc_results(uri: str, tree: Node, referenced: bool) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    tail = "" if referenced else "; no manifest read here names this file"
    for element in descendants(tree):
        if element.tag not in ("base-config", "domain-config"):
            continue
        if (element.attrs.get("cleartextTrafficPermitted") or "").strip().lower() != "true":
            continue
        if element.tag == "base-config":
            what = '<base-config cleartextTrafficPermitted="true">: cleartext traffic is permitted for every domain'
        else:
            domains = ", ".join("".join(d.text).strip() for d in element.all("domain")) or "no domain listed"
            what = '<domain-config cleartextTrafficPermitted="true"> (%s): cleartext traffic is permitted for these domains' % domains
        found.append(result("cleartext-traffic", uri, element.line, what + tail))
    return found


def ats_results(uri: str, values: Dict[str, Any], lines: Dict[Tuple[Any, ...], int], notes: List[str]) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    ats = values.get("NSAppTransportSecurity")
    if not isinstance(ats, dict):
        return found

    def at(*key: Any) -> int:
        return lines.get(("NSAppTransportSecurity",) + key, lines.get(("NSAppTransportSecurity",), 1))
    present = [k for k in ATS_FINE if k in ats]
    if ats.get("NSAllowsArbitraryLoads") is True:
        if present:
            notes.append("%s:%d: NSAllowsArbitraryLoads is true beside %s, so iOS 10 and later ignore it; those keys are read on their own"
                         % (uri, at("NSAllowsArbitraryLoads"), ", ".join(present)))
        else:
            found.append(result("ats-arbitrary-loads", uri, at("NSAllowsArbitraryLoads"),
                                "NSAppTransportSecurity: NSAllowsArbitraryLoads is true: App Transport Security is off for every connection"))
    for key, what in (("NSAllowsArbitraryLoadsInWebContent", "for web content"), ("NSAllowsArbitraryLoadsForMedia", "for media")):
        if ats.get(key) is True:
            found.append(result("ats-arbitrary-loads", uri, at(key), "NSAppTransportSecurity: %s is true: App Transport Security is off %s"
                                % (key, what)))
    if ats.get("NSAllowsLocalNetworking") is True:
        found.append(result("ats-insecure-exception", uri, at("NSAllowsLocalNetworking"),
                            "NSAppTransportSecurity: NSAllowsLocalNetworking is true: insecure loads are allowed to local hosts"))
    domains = ats.get("NSExceptionDomains")
    if isinstance(domains, dict):
        for domain in sorted(domains):
            entry = domains[domain]
            if isinstance(entry, dict) and entry.get("NSExceptionAllowsInsecureHTTPLoads") is True:
                found.append(result("ats-insecure-exception", uri, at("NSExceptionDomains", domain, "NSExceptionAllowsInsecureHTTPLoads"),
                                    "NSAppTransportSecurity: NSExceptionDomains %s sets NSExceptionAllowsInsecureHTTPLoads true: HTTP "
                                    "is allowed to that domain" % domain))
    return found


def entitlement_results(uri: str, values: Dict[str, Any], lines: Dict[Tuple[Any, ...], int]) -> List[Dict[str, Any]]:
    if values.get("get-task-allow") is True:
        return [result("get-task-allow", uri, lines.get(("get-task-allow",), 1),
                       "get-task-allow is true: a debugger can attach to the app signed with these entitlements")]
    return []


# --------------------------------------------------------------------------- the scan

FAILED = (Unreadable, RecursionError, ValueError)  # a file's own failure: it is listed, and the scan goes on


def failure(exc: BaseException) -> str:
    return str(exc) if isinstance(exc, Unreadable) else "not read: %s" % type(exc).__name__


def scan(root: Path) -> Dict[str, Any]:
    unreadable: List[Dict[str, str]] = []
    notes: List[str] = []
    counts = {"manifest": 0, "network-security-config": 0, "gradle": 0, "plist": 0, "entitlements": 0}
    data: Dict[Path, bytes] = {}
    files = []
    for path, kind in project_files(root, unreadable):
        try:
            data[path] = read_regular(path)
        except Unreadable as exc:
            unreadable.append({"path": uri_of(path), "reason": str(exc)})
            continue
        files.append((path, kind))
    gradles: Dict[Path, Dict[str, Optional[int]]] = {}
    for path, kind in files:
        if kind == "gradle":
            try:
                gradles[Path(os.path.abspath(str(path)))] = gradle_levels(data[path].decode("utf-8"))
                counts["gradle"] += 1
            except UnicodeDecodeError:
                unreadable.append({"path": uri_of(path), "reason": "not UTF-8"})
            except FAILED as exc:
                unreadable.append({"path": uri_of(path), "reason": failure(exc)})
    nsc_trees: Dict[Path, Node] = {}
    for path, kind in files:
        if kind == "res-xml":
            try:
                tree = parse_xml(data[path], plist=False)
            except FAILED as exc:
                unreadable.append({"path": uri_of(path), "reason": failure(exc)})
                continue
            if tree.tag == "network-security-config":
                nsc_trees[Path(os.path.abspath(str(path)))] = tree
    results: List[Dict[str, Any]] = []
    sdk: List[Dict[str, Any]] = []
    referenced: set = set()
    for path, kind in files:
        if kind != "manifest":
            continue
        try:
            tree = parse_xml(data[path], plist=False)
            levels = sdk_for(path, root, tree, gradles)
            nsc_found: Optional[bool] = None
            names = set()
            for app in tree.all("application"):
                name = app.android("networkSecurityConfig") or ""
                if name.startswith("@xml/"):
                    target = Path(os.path.abspath(str(path.parent / "res" / "xml" / (name[len("@xml/"):] + ".xml"))))
                    nsc_found = target in nsc_trees
                    if nsc_found:
                        names.add(target)
            own_notes: List[str] = []
            found = manifest_results(uri_of(path), tree, levels, nsc_found, own_notes)
        except FAILED as exc:
            unreadable.append({"path": uri_of(path), "reason": failure(exc)})
            continue
        counts["manifest"] += 1
        sdk.append(levels)
        referenced |= names
        notes += own_notes
        results += found
    for target, tree in sorted(nsc_trees.items()):
        try:
            found = nsc_results(uri_of(target), tree, target in referenced)
        except FAILED as exc:
            unreadable.append({"path": uri_of(target), "reason": failure(exc)})
            continue
        counts["network-security-config"] += 1
        results += found
    for path, kind in files:
        if kind not in ("plist", "entitlements"):
            continue
        try:
            values, lines, binary = read_plist(data[path])
            own_notes = []
            found = ats_results(uri_of(path), values, lines, own_notes) if kind == "plist" else entitlement_results(uri_of(path), values, lines)
        except FAILED as exc:
            unreadable.append({"path": uri_of(path), "reason": failure(exc)})
            continue
        counts[kind] += 1
        if binary:
            notes.append("%s: a binary property list has no lines; its results cite line 1" % uri_of(path))
        notes += own_notes
        results += found
    results.sort(key=lambda r: (r["path"], r["line"], r["rule"]))
    return {"results": results, "unreadable": unreadable, "counts": counts, "sdk": sdk, "notes": notes}


def sarif(found: Dict[str, Any]) -> Dict[str, Any]:
    rules = [{"id": rule, "shortDescription": {"text": info["text"]},
              "properties": {"platform": info["platform"], "masvs": info["masvs"], "mastg": info["mastg"], "maswe": info["maswe"],
                             "cwe": info["cwe"], "tags": ["security", "mobile", info["platform"]] + info["masvs"] + info["cwe"]}}
             for rule, info in RULES.items()]
    results = [{"ruleId": r["rule"], "level": "warning", "message": {"text": r["message"]},
                "locations": [{"physicalLocation": {"artifactLocation": {"uri": r["path"]}, "region": {"startLine": r["line"]}}}],
                "properties": {k: r[k] for k in ("approximate", "platform", "masvs", "mastg", "maswe", "cwe")}}
               for r in found["results"]]
    notes = [{"level": "warning", "message": {"text": "unreadable: %s: %s" % (u["path"], u["reason"])}} for u in found["unreadable"]]
    notes += [{"level": "note", "message": {"text": n}} for n in found["notes"]]
    return {"version": "2.1.0", "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [{"tool": {"driver": {"name": DRIVER, "version": VERSION, "rules": rules}},
                      "invocations": [{"executionSuccessful": True, "toolExecutionNotifications": notes}],
                      "results": results,
                      "properties": {"unreadable": found["unreadable"], "files": found["counts"], "sdk": found["sdk"],
                                     "notes": found["notes"]}}]}


def as_json(found: Dict[str, Any]) -> Dict[str, Any]:
    return {"tool": DRIVER, "version": VERSION, "results": listed(found), "unreadable": found["unreadable"], "files": found["counts"],
            "sdk": found["sdk"], "notes": found["notes"]}


def listed(found: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"rule": r["rule"], "at": "%s:%d" % (r["path"], r["line"]), "platform": r["platform"], "masvs": r["masvs"], "mastg": r["mastg"],
             "maswe": r["maswe"], "cwe": r["cwe"], "approximate": r["approximate"], "message": r["message"]} for r in found["results"]]


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root")
    parser.add_argument("--out")
    parser.add_argument("--format", choices=("sarif", "json"), default="sarif")
    parser.add_argument("--copy-to-work", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.copy_to_work:
            if args.root or args.out:
                raise Refused("--copy-to-work takes no other option")
            data = Path(__file__).read_bytes()
            written = write_new(WORK_DIR + "/mobile_static.py", data)
            print(json.dumps({"ok": True, "written": written, "sha256": sha256(data),
                              "input": "/work/.qa-input/mobile/mobile_static.py"}, sort_keys=True))
            return 0
        if not args.root:
            raise Refused("--root DIR is required")
        root = Path(args.root)
        mode = os.lstat(str(root)).st_mode if os.path.lexists(str(root)) else None
        if mode is None or stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise Refused("--root must be a directory that is not a link")
        found = scan(root)
        report = sarif(found) if args.format == "sarif" else as_json(found)
        data = (json.dumps(report, indent=1, sort_keys=True) + "\n").encode("utf-8")
        written = write_new(args.out, data) if args.out else None
    except (Refused, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, Refused) else "%s: %s" % (type(exc).__name__, exc)},
                         sort_keys=True))
        return 2
    by_rule: Dict[str, int] = {}
    for item in found["results"]:
        by_rule[item["rule"]] = by_rule.get(item["rule"], 0) + 1
    print(json.dumps({"ok": True, "format": args.format, "files": found["counts"], "results": len(found["results"]), "by_rule": by_rule,
                      "listed": listed(found), "unreadable": found["unreadable"], "sdk": found["sdk"], "notes": found["notes"],
                      "out": written, "sha256": sha256(data) if written else None}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
