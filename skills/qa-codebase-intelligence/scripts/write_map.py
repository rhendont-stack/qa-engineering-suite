"""Starts, checks and writes the codebase map: ``.qa/map/codebase-map.json`` and ``.qa/map/codebase-map.md``.

Usage: python3 write_map.py status   [--root DIR]
       python3 write_map.py draft    [--root DIR] [--top N]
       python3 write_map.py write    [--root DIR] [--draft FILE|-] [--force] [authoring flags]
       python3 write_map.py validate FILE

``status`` says whether a map exists for the current commit and working tree (``fresh``), so that it can be reused.
``draft`` prints the parts of a map that scripts can establish (stack, modules with the first line of their own description,
hotspots, tests), each already carrying its evidence, plus ``_todo`` and ``_signals`` keys with what the author still has to
decide; keys starting with ``_`` are dropped by ``write``. ``write`` starts from that draft (or from ``--draft``, a JSON file
or ``-`` for stdin), applies the authoring flags, adds the commit, the dirty flag,
the tree fingerprint and the time, checks the result against ``codebase-map.schema.json``, checks that every path it cites
exists and every execution it cites is in the ledger, and only then writes the map. When a fresh map exists it refuses
(exit 3) unless ``--force`` is given, which is for a rebuild the user asked for. Every write appends a line to
``.qa/map/writes.jsonl``. ``validate`` checks any map file against the schema.

The authoring flags need no JSON on the command line (a shell or its safety checks may refuse braces and quotes). Each takes one
quoted string whose fields are separated by ``|``; a path may end in ``:LINES`` (``src/a.py:10-20``). ``--summary TEXT`` with
``--cite PATH`` (repeatable); ``--framework LANGUAGE|NAME|PATH``; ``--purpose PATH|TEXT``; ``--entry KIND|NAME|PATH``;
``--data-store KIND|NAME|PATH[,PATH]``; ``--integration KIND|NAME|PATH[,PATH]``; ``--critical PATH|TAG[,TAG]|REASON``;
``--unknown TOPIC|WHY|SEARCHED[;SEARCHED]``. Each flag may repeat, and each entry carries the paths it names as evidence.

Every command prints one JSON object on stdout. Exit 0 ok, 2 bad input or a failed check, 3 refused because a fresh map exists.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
KERNEL = os.path.join(HERE, "_kernel")
if not os.path.isdir(KERNEL):  # run from the source tree, before the kernel is vendored
    KERNEL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "shared", "kernel")

import _repo  # noqa: E402
import git_hotspots  # noqa: E402
import import_graph  # noqa: E402
import inventory_tests  # noqa: E402
# The sibling scripts put this directory first on the path, and its qa.py (the launcher) would shadow the kernel package: so the
# kernel goes in front only after they are imported.
sys.path.insert(0, KERNEL)
from qa import ids, schema  # noqa: E402

SCHEMA = "codebase-map.schema.json"
MAP_DIR = os.path.join(".qa", "map")
MAP_JSON = "codebase-map.json"
MAP_MD = "codebase-map.md"
WRITES = "writes.jsonl"
MANIFEST_LANGUAGES = {"package.json": ("javascript", "typescript"), "pyproject.toml": ("python",), "requirements.txt": ("python",),
                      "pytest.ini": ("python",), "build.gradle": ("java", "kotlin"), "build.gradle.kts": ("java", "kotlin"),
                      "pom.xml": ("java", "kotlin"), "go.mod": ("go",), "Cargo.toml": ("rust",)}
EXIT_FAILED = 2
EXIT_REFUSED = 3


class MapError(Exception):
    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code, self.extra = code, extra


def emit(body: Dict[str, Any]) -> None:
    print(json.dumps(body, sort_keys=True, separators=(",", ":")))


def map_path(root: Path) -> Path:
    return Path(root) / MAP_DIR / MAP_JSON


def load_map(root: Path) -> Optional[Dict[str, Any]]:
    try:
        doc = json.loads(map_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def status(root: Path) -> Dict[str, Any]:
    """Whether a map for this commit and working tree is already there."""
    now = _repo.tree_state(root)
    doc = load_map(root)
    body: Dict[str, Any] = {"ok": True, "command": "status", "fresh": False, "commit": now["commit"], "dirty": now["dirty"],
                            "map": None, "map_commit": None}
    if doc is None:
        body["reason"] = "no_map"
        return body
    body["map"] = os.path.join(MAP_DIR, MAP_JSON).replace(os.sep, "/")
    body["map_commit"] = doc.get("commit")
    if doc.get("schema_version") != 1 or schema.errors(doc, schema.load_schema(SCHEMA)):
        body["reason"] = "map_invalid"
    elif doc.get("commit") != now["commit"]:
        body["reason"] = "commit_changed"
    elif doc.get("tree") != now["tree"]:
        body["reason"] = "tree_changed"
    else:
        body["fresh"], body["reason"] = True, "same_commit_and_tree"
    return body


def path_allowed(name: str) -> bool:
    """Whether the map schema accepts ``name`` as a path. ``draft`` lists only such paths, so that ``write`` stays possible."""
    spec = schema.load_schema(SCHEMA)["$defs"]["path"]
    return len(name) <= spec["maxLength"] and re.search(spec["pattern"].replace("$", r"\Z"), name) is not None


def _evidence(*paths: str) -> List[Dict[str, str]]:
    return [{"path": p} for p in paths]


def draft(root: Path, top: int = 20) -> Dict[str, Any]:
    """The mechanical part of a map, with evidence, and what is left for the author."""
    names = _repo.files(root)
    refused = [n for n in names if not path_allowed(n)]
    names = [n for n in names if path_allowed(n)]
    graph = import_graph.analyse(root)
    hot = git_hotspots.hotspots(root, top)
    tests = inventory_tests.inventory(root)
    for item in graph["modules"] + hot["hotspots"]:
        if not path_allowed(item["path"]) and item["path"] not in refused:
            refused.append(item["path"])
    for suite in tests["suites"]:
        refused += [f for f in suite["files"] + suite["config"] if not path_allowed(f) and f not in refused]
    graph["modules"] = [dict(m, imports=[i for i in m["imports"] if path_allowed(i)]) for m in graph["modules"] if path_allowed(m["path"])]
    for module in graph["modules"]:
        module["fan_out"] = len(module["imports"])
    hot["hotspots"] = [h for h in hot["hotspots"] if path_allowed(h["path"])]
    for suite in tests["suites"]:
        suite["files"] = [f for f in suite["files"] if path_allowed(f)]
        suite["config"] = [f for f in suite["config"] if path_allowed(f)]
    tests["suites"] = [x for x in tests["suites"] if x["files"]]
    extra: List[str] = []
    if refused:
        extra.append("%d path(s) left out because the map schema does not accept them (for example a backslash or a control "
                     "character in the name): %s" % (len(refused), ", ".join(repr(n) for n in sorted(refused)[:10])))
    extra += sorted(set(_repo.REFUSED))
    bad = _repo.undecodable(root)
    if bad:
        extra.append("%d file name(s) left out because they are not valid UTF-8: %s" % (len(bad), ", ".join(sorted(bad)[:10])))
    by_language: Dict[str, List[str]] = {}
    for name in names:
        language = _repo.language_of(name)
        if language:
            by_language.setdefault(language, []).append(name)
    manifests = [n for n in names if os.path.basename(n) in MANIFEST_LANGUAGES]
    stack = []
    for language, found in sorted(by_language.items()):
        sharing = [m for m in manifests if language in MANIFEST_LANGUAGES[os.path.basename(m)]
                   and (os.path.dirname(m) == "" or any(f.startswith(os.path.dirname(m) + "/") for f in found))]
        stack.append({"language": language, "frameworks": [], "evidence": _evidence(*(sharing[:3] + found[:2]))})
    modules = [{"path": m["path"], "language": m["language"], "purpose": m.get("description") or "No description in the file.", "imports": m["imports"], "fan_in": m["fan_in"],
                "fan_out": m["fan_out"], "approximate": m["approximate"], "evidence": _evidence(m["path"])} for m in graph["modules"]]
    hotspots = [{"path": h["path"], "commits": h["commits"], "lines_changed": h["lines_changed"], "complexity": h["complexity"],
                 "score": h["score"], "evidence": _evidence(h["path"])} for h in hot["hotspots"]]
    suites = [{"framework": s["framework"], "command": s["command"], "files": s["files"], "test_count": s["test_count"],
               "evidence": _evidence(*((s["config"] + s["files"])[:6]))} for s in tests["suites"]]
    return {
        "stack": stack, "entry_points": [], "modules": modules, "data_stores": [], "integrations": [], "critical_components": [],
        "hotspots": hotspots, "tests": suites, "unknowns": [],
        "_todo": ["summary: one factual statement, with evidence", "stack[].frameworks: name only what a manifest or import shows",
                  "modules[].purpose: pre-filled from the file's own first description line; override with --purpose where you read more",
                  "entry_points, data_stores, integrations, critical_components: add only what a signal or a file you read shows",
                  "unknowns: everything asked about or expected that you could not establish, with what you searched",
                  "add what you establish with the authoring flags (see the module docstring of write_map.py): --summary/--cite, --entry, --data-store, --integration, --critical, --framework, --unknown"],
        "_signals": graph["signals"], "_entry_hints": graph["entry_hints"],
        "_limitations": extra + graph["limitations"] + hot["limitations"] + tests["limitations"],
    }


def _paths(node: Any, key: Optional[str] = None) -> List[str]:
    found: List[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "path" and isinstance(v, str):
                found.append(v)
            elif k != "unknowns":
                found += _paths(v, k)
    elif isinstance(node, list):
        for item in node:
            found += [item] if isinstance(item, str) and key in ("files", "imports") else _paths(item, key)
    return found


def _exes(node: Any) -> List[str]:
    found: List[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            found += [v] if k == "exe" and isinstance(v, str) else _exes(v)
    elif isinstance(node, list):
        for item in node:
            found += _exes(item)
    return found


def recorded(root: Path, exe: str) -> bool:
    ledger = Path(root) / ".qa" / "ledger"
    try:
        for entry in ledger.iterdir():
            if entry.suffix == ".jsonl" and exe in entry.read_text(encoding="utf-8", errors="replace"):
                return True
    except OSError:
        pass
    return False


def finish(root: Path, source: Dict[str, Any]) -> Dict[str, Any]:
    """The draft with its metadata added and its ``_`` keys dropped, checked, or a ``MapError``."""
    doc = {k: v for k, v in source.items() if not k.startswith("_")}
    state = _repo.tree_state(root)
    doc.update(schema_version=1, commit=state["commit"], dirty=state["dirty"], tree=state["tree"],
               generated_at=ids.format_ts(ids.now_utc()))
    errors = schema.errors(doc, schema.load_schema(SCHEMA))
    if errors:
        raise MapError("schema_invalid", "the map does not match codebase-map.schema.json", errors=errors[:10])
    existing = set(_repo.files(root))
    missing = sorted({p for p in _paths(doc) if p not in existing})
    if missing:
        raise MapError("path_not_found", "the map cites paths that are not files of the repository", paths=missing[:10])
    unknown = sorted({e for e in _exes(doc) if not recorded(root, e)})
    if unknown:
        raise MapError("execution_not_recorded", "the map cites executions that are not in the ledger", executions=unknown[:10])
    return doc


def _cited(items: List[Dict[str, Any]]) -> str:
    return ", ".join("`%s%s`" % (i["path"], ":" + i["lines"] if i.get("lines") else "") if "path" in i else "`%s`" % i["exe"] for i in items)


def render(doc: Dict[str, Any]) -> str:
    """The Markdown copy of the map: the same statements and the same citations."""
    out = ["# Codebase map", "",
           "Commit `%s`%s, generated %s. Source of truth: `.qa/map/codebase-map.json`." % (
               doc["commit"] or "none (not a git repository)", " (uncommitted changes)" if doc["dirty"] else "", doc["generated_at"]),
           "", doc["summary"]["text"] + " (" + _cited(doc["summary"]["evidence"]) + ")", ""]

    def section(title: str, rows: List[str]) -> None:
        out.append("## " + title)
        out.append("")
        out.extend(rows or ["None found."])
        out.append("")
    section("Stack", ["- %s%s (%s)" % (s["language"], ": " + ", ".join(s["frameworks"]) if s["frameworks"] else "", _cited(s["evidence"])) for s in doc["stack"]])
    section("Entry points", ["- %s `%s`: %s (%s)" % (e["kind"], e["path"], e["name"], _cited(e["evidence"])) for e in doc["entry_points"]])
    section("Modules", ["- `%s`%s: %s; imports %d, imported by %d (%s)" % (m["path"], " (approximate)" if m["approximate"] else "", m["purpose"],
                                                                      m["fan_out"], m["fan_in"], _cited(m["evidence"])) for m in doc["modules"]])
    section("Data stores", ["- %s: %s (%s)" % (d["kind"], d["name"], _cited(d["evidence"])) for d in doc["data_stores"]])
    section("Integrations", ["- %s: %s (%s)" % (i["kind"], i["name"], _cited(i["evidence"])) for i in doc["integrations"]])
    section("Critical components", ["- `%s` [%s]: %s (%s)" % (c["path"], ", ".join(c["tags"]), c["reason"], _cited(c["evidence"])) for c in doc["critical_components"]])
    section("Hotspots", ["- `%s`: %d commits, %d lines changed, complexity %d, score %s (%s)" % (
        h["path"], h["commits"], h["lines_changed"], h["complexity"], h["score"], _cited(h["evidence"])) for h in doc["hotspots"]])
    section("Tests", ["- %s: %d tests in %d files, command %s (%s)" % (t["framework"], t["test_count"], len(t["files"]),
                                                                       "`%s`" % t["command"] if t["command"] else "unknown", _cited(t["evidence"])) for t in doc["tests"]])
    section("Unknowns", ["- %s: %s Searched: %s." % (u["topic"], u["why"], "; ".join(u["searched"]) or "nothing recorded") for u in doc["unknowns"]])
    return "\n".join(out)


def _guard(root: Path) -> Path:
    """The map directory, created if needed, or a ``MapError`` when ``.qa``, ``.qa/map`` or a file the writer would touch is a
    symlink, or the directory resolves outside the project: the repository is data, and a planted link must not redirect a write."""
    real = os.path.realpath(str(root))
    here = Path(root)
    for part in (".qa", MAP_DIR):
        here = Path(root) / part
        if os.path.islink(str(here)):
            raise MapError("unsafe_path", "%s is a symbolic link: refusing to write through it" % part.replace(os.sep, "/"))
        if os.path.lexists(str(here)) and not os.path.isdir(str(here)):
            raise MapError("unsafe_path", "%s exists and is not a directory" % part.replace(os.sep, "/"))
        if not os.path.lexists(str(here)):
            os.mkdir(str(here))
    if os.path.realpath(str(here)) != os.path.join(real, MAP_DIR):
        raise MapError("unsafe_path", "the map directory resolves outside the project")
    for name in (MAP_JSON, MAP_MD, WRITES):
        if os.path.islink(str(here / name)):
            raise MapError("unsafe_path", "%s/%s is a symbolic link: refusing to write through it" % (MAP_DIR.replace(os.sep, "/"), name))
    return here


def _atomic(path: Path, data: str) -> None:
    tmp = path.parent / (".%s.%s.tmp" % (path.name, os.urandom(4).hex()))
    tmp.write_text(data, encoding="utf-8")
    os.replace(str(tmp), str(path))


def check_links(root: Path) -> None:
    """Refuse before anything is read or written when the map directory is a link (``_guard`` without creating anything)."""
    here = Path(root)
    for part in (".qa", MAP_DIR):
        here = Path(root) / part
        if os.path.islink(str(here)):
            raise MapError("unsafe_path", "%s is a symbolic link: refusing to write through it" % part.replace(os.sep, "/"))
    for name in (MAP_JSON, MAP_MD, WRITES):
        if os.path.islink(str(here / name)):
            raise MapError("unsafe_path", "%s/%s is a symbolic link: refusing to write through it" % (MAP_DIR.replace(os.sep, "/"), name))
    if os.path.lexists(str(here)) and os.path.realpath(str(here)) != os.path.join(os.path.realpath(str(root)), MAP_DIR):
        raise MapError("unsafe_path", "the map directory resolves outside the project")


def _cite(text: str) -> Dict[str, str]:
    path, _, tail = text.strip().rpartition(":")
    if path and tail and all(c in "0123456789-" for c in tail):
        return {"path": path, "lines": tail}
    return {"path": text.strip()}


def _fields(spec: str, count: int, flag: str) -> List[str]:
    parts = [p.strip() for p in spec.split("|")]
    if len(parts) != count or not all(parts[:count - 1]) and flag != "--unknown":
        raise MapError("bad_flag", "%s takes %d fields separated by |: got %r" % (flag, count, spec))
    return parts


def _paths_of(text: str, flag: str) -> List[Dict[str, str]]:
    found = [_cite(p) for p in text.split(",") if p.strip()]
    if not found:
        raise MapError("bad_flag", "%s needs at least one path" % flag)
    return found


def apply_flags(doc: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    """The draft with what the authoring flags say added to it."""
    doc = dict(doc)
    if args.summary:
        doc["summary"] = {"text": args.summary, "evidence": [_cite(c) for c in args.cite]}
    for spec in args.framework:
        language, name, path = _fields(spec, 3, "--framework")
        entry = next((s for s in doc.get("stack", []) if s.get("language") == language), None)
        if entry is None:
            raise MapError("bad_flag", "--framework names a language that is not in the stack: %s" % language)
        entry["frameworks"] = sorted(set(entry["frameworks"]) | {name})
        entry["evidence"] = entry["evidence"] + [c for c in [_cite(path)] if c not in entry["evidence"]]
    for spec in args.purpose:
        path, text = _fields(spec, 2, "--purpose")
        module = next((m for m in doc.get("modules", []) if m.get("path") == path), None)
        if module is None:
            raise MapError("bad_flag", "--purpose names a path that is not a module of the draft: %s" % path)
        module["purpose"] = text
    for flag, key, kinds in (("entry", "entry_points", None), ("data_store", "data_stores", None), ("integration", "integrations", None)):
        for spec in getattr(args, flag):
            kind, name, paths = _fields(spec, 3, "--" + flag.replace("_", "-"))
            cites = _paths_of(paths, "--" + flag.replace("_", "-"))
            item: Dict[str, Any] = {"kind": kind, "name": name, "evidence": cites}
            if key == "entry_points":
                item["path"] = cites[0]["path"]
            doc.setdefault(key, []).append(item)
    for spec in args.critical:
        path, tags, reason = _fields(spec, 3, "--critical")
        doc.setdefault("critical_components", []).append(
            {"path": _cite(path)["path"], "tags": [t.strip() for t in tags.split(",") if t.strip()], "reason": reason, "evidence": [_cite(path)]})
    for spec in args.unknown:
        topic, why, searched = _fields(spec, 3, "--unknown")
        doc.setdefault("unknowns", []).append({"topic": topic, "why": why, "searched": [x.strip() for x in searched.split(";") if x.strip()]})
    return doc


def write(root: Path, source: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
    check_links(root)
    current = status(root)
    if current["fresh"] and not force:
        raise MapError("fresh_map_exists", "a map for this commit and working tree already exists: reuse it, do not rebuild it. "
                       "Pass --force only when the user asked for a rebuild.", map=current["map"], commit=current["commit"])
    doc = finish(root, source)
    body = json.dumps(doc, sort_keys=True, indent=2) + "\n"
    markdown = render(doc) + "\n"  # before anything is written: a failure here must leave no half a map
    _guard(root)
    _atomic(map_path(root), body)
    _atomic(map_path(root).with_name(MAP_MD), markdown)
    line = json.dumps({"commit": doc["commit"], "tree": doc["tree"], "generated_at": doc["generated_at"], "forced": bool(force),
                       "sha256": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()}, sort_keys=True)
    fd = os.open(str(map_path(root).with_name(WRITES)), os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o666)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return {"ok": True, "command": "write", "map": (MAP_DIR + "/" + MAP_JSON).replace(os.sep, "/"),
            "markdown": (MAP_DIR + "/" + MAP_MD).replace(os.sep, "/"), "commit": doc["commit"], "dirty": doc["dirty"],
            "modules": len(doc["modules"]), "unknowns": len(doc["unknowns"]), "forced": bool(force)}


def read_draft(source: str) -> Dict[str, Any]:
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
        doc = json.loads(text)
    except (OSError, ValueError) as exc:
        raise MapError("bad_draft", "the draft is not readable JSON: %s" % exc)
    if not isinstance(doc, dict):
        raise MapError("bad_draft", "the draft must be a JSON object")
    return doc


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "draft", "write"):
        one = sub.add_parser(name)
        one.add_argument("--root", default=".")
        if name == "draft":
            one.add_argument("--top", type=int, default=20)
        if name == "write":
            one.add_argument("--draft", metavar="FILE|-", help="start from this JSON instead of the generated draft")
            one.add_argument("--force", action="store_true")
            one.add_argument("--top", type=int, default=20)
            one.add_argument("--summary", metavar="TEXT")
            one.add_argument("--cite", action="append", default=[], metavar="PATH[:LINES]", help="evidence for --summary")
            for flag in ("framework", "purpose", "entry", "data-store", "integration", "critical", "unknown"):
                one.add_argument("--" + flag, action="append", default=[], metavar="A|B|C")
    check = sub.add_parser("validate")
    check.add_argument("file")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            doc = read_draft(args.file)
            errors = schema.errors(doc, schema.load_schema(SCHEMA))
            emit({"ok": not errors, "command": "validate", "valid": not errors, "errors": errors[:20]})
            return 0 if not errors else EXIT_FAILED
        root = Path(args.root)
        if not root.is_dir():
            raise MapError("bad_root", "--root must be a directory")
        if args.command == "status":
            emit(status(root))
        elif args.command == "draft":
            emit(draft(root, args.top))
        else:
            base = read_draft(args.draft) if args.draft else draft(root, args.top)
            emit(write(root, apply_flags(base, args), args.force))
        return 0
    except MapError as exc:
        emit(dict(exc.extra, ok=False, command=args.command, error=exc.code, message=str(exc)))
        return EXIT_REFUSED if exc.code == "fresh_map_exists" else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
