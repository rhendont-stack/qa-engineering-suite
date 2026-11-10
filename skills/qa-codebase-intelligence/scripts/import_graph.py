"""Builds the import graph of a repository and lists what its code says about entry points, data stores and integrations.

Usage: python3 import_graph.py [--root DIR]

Reads Python (with ``ast``, so the result is exact), JavaScript and TypeScript, Java and Go (with regular expressions, so the
result is approximate and every such module says so). An import is *internal* when it resolves to a file of the repository;
anything else is *external* and is named only by its package.

Prints one JSON object: ``{"modules": [{"path", "language", "imports", "external", "fan_in", "fan_out", "approximate", "description"}],
"signals": [{"kind", "name", "path", "line"}], "entry_hints": [{"kind", "path", "line", "detail"}], "limitations": [...]}``.
``signals`` are imports of well-known database drivers, ORMs, HTTP clients and queue clients, plus environment-variable
names read by the code. Nothing here is a conclusion: a signal is a path and a line to go and read. It only reads; exit 0.
"""
from __future__ import annotations

import argparse
import ast
import bisect
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import _repo  # noqa: E402

# Each alternative is anchored on the keyword just before the quote, with no gap to scan, so the cost is linear in the file size:
# `from 'x'` covers `import ... from` and `export ... from`, the others the bare import, the dynamic import and require.
JS_IMPORT = re.compile(r"""(?:\bfrom\s*|\brequire\s*\(\s*|\bimport\s*\(?\s*)['"]([^'"\n]+)['"]""")
JAVA_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+?)(?:\.\*)?\s*;", re.M)
GO_IMPORT_BLOCK = re.compile(r"^\s*import\s*\((.*?)\)", re.M | re.S)
GO_IMPORT_ONE = re.compile(r'^\s*import\s+(?:\w+\s+)?"([^"]+)"', re.M)
GO_QUOTED = re.compile(r'"([^"]+)"')
ENV_READ = re.compile(r"""os\.environ\s*(?:\[\s*|\.get\s*\(\s*)['"]([A-Z][A-Z0-9_]+)['"]|os\.getenv\s*\(\s*['"]([A-Z][A-Z0-9_]+)['"]"""
                      r"""|process\.env\.([A-Z][A-Z0-9_]+)|System\.getenv\s*\(\s*"([A-Z][A-Z0-9_]+)"|os\.Getenv\s*\(\s*"([A-Z][A-Z0-9_]+)\"""")
MAIN_PY = re.compile(r"""^if\s+__name__\s*==\s*['"]__main__['"]\s*:""", re.M)
MAIN_JAVA = re.compile(r"\bpublic\s+static\s+void\s+main\s*\(\s*String")
MAIN_GO = re.compile(r"^func\s+main\s*\(\s*\)", re.M)
ROUTE = re.compile(r"""@(?:\w+\.)?(?:route|get|post|put|delete|patch)\s*\(\s*['"]([^'"]+)['"]|\b(?:app|router)\.(?:get|post|put|delete|patch|use)\s*\(\s*['"]([^'"]+)['"]"""
                   r"""|@(?:Get|Post|Put|Delete|Patch|Request)Mapping""")

# package prefix -> (signal kind, name)
KNOWN = [
    ("sqlite3", "data-store", "sqlite"), ("psycopg", "data-store", "postgresql"), ("psycopg2", "data-store", "postgresql"), ("pg", "data-store", "postgresql"),
    ("asyncpg", "data-store", "postgresql"), ("pymysql", "data-store", "mysql"), ("mysql", "data-store", "mysql"),
    ("mysql2", "data-store", "mysql"), ("sqlalchemy", "data-store", "sql-orm"), ("django.db", "data-store", "django-orm"),
    ("peewee", "data-store", "sql-orm"), ("pymongo", "data-store", "mongodb"), ("mongoose", "data-store", "mongodb"),
    ("mongodb", "data-store", "mongodb"), ("redis", "data-store", "redis"), ("ioredis", "data-store", "redis"),
    ("@prisma/client", "data-store", "prisma"), ("typeorm", "data-store", "sql-orm"), ("sequelize", "data-store", "sql-orm"),
    ("knex", "data-store", "sql-query-builder"), ("better-sqlite3", "data-store", "sqlite"), ("java.sql", "data-store", "jdbc"),
    ("javax.persistence", "data-store", "jpa"), ("jakarta.persistence", "data-store", "jpa"), ("org.hibernate", "data-store", "hibernate"),
    ("org.springframework.data", "data-store", "spring-data"), ("database/sql", "data-store", "go-sql"), ("gorm.io", "data-store", "gorm"),
    ("requests", "http-client", "requests"), ("httpx", "http-client", "httpx"), ("urllib.request", "http-client", "urllib"),
    ("aiohttp", "http-client", "aiohttp"), ("axios", "http-client", "axios"), ("node-fetch", "http-client", "node-fetch"),
    ("got", "http-client", "got"), ("okhttp3", "http-client", "okhttp"), ("java.net.http", "http-client", "java-http"),
    ("net/http", "http-client", "go-http"),
    ("pika", "queue", "rabbitmq"), ("amqplib", "queue", "rabbitmq"), ("kafka", "queue", "kafka"), ("confluent_kafka", "queue", "kafka"),
    ("kafkajs", "queue", "kafka"), ("celery", "queue", "celery"), ("bullmq", "queue", "bullmq"), ("boto3", "sdk", "aws"),
    ("@aws-sdk", "sdk", "aws"), ("stripe", "sdk", "stripe"), ("google.cloud", "sdk", "google-cloud"),
]
JS_EXT = ("", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", "/index.js", "/index.ts", "/index.mjs")
SUPPORTED = ("python", "javascript", "typescript", "java", "go")


COMMENT_LINE = re.compile(r"^\s*(?://+|/\*+|\*(?!/)|#)\s*(.*?)\s*(?:\*/)?\s*$")
NOT_A_DESCRIPTION = re.compile(r"copyright|licen[sc]e|^[-=*#/ ]*$|^@\w+|^!/|coding[:=]|eslint|prettier", re.I)


def first_description(text: str, language: str) -> Optional[str]:
    """The first line of a module's own description: a Python docstring, else the first comment line near the top of a
    JavaScript, TypeScript, Java or Go file. None when it has none (a copyright header is not a description)."""
    line = None
    if language == "python":
        try:
            doc = ast.get_docstring(ast.parse(text))
        except (SyntaxError, ValueError):
            doc = None
        line = doc.strip().splitlines()[0] if doc and doc.strip() else None
    else:
        for raw in text.splitlines()[:60]:
            match = COMMENT_LINE.match(raw)
            if match and match.group(1) and not NOT_A_DESCRIPTION.search(match.group(1)):
                line = match.group(1)
                break
    return line[:300] if line else None


def classify(external: str) -> Optional[Tuple[str, str]]:
    for prefix, kind, name in KNOWN:
        if external == prefix or external.startswith(prefix + ".") or external.startswith(prefix + "/"):
            return kind, name
    return None


def _resolve_python(known: Set[str], importer: str, module: str, level: int, names: List[str]) -> List[str]:
    """Repository files an import statement can mean: ``module`` itself, or ``module.<name>`` for ``from module import name``."""
    parts = [p for p in module.split(".") if p]
    here = importer.split("/")[:-1]
    if level:
        up = len(here) - (level - 1)
        bases = ["/".join(here[:up]) + "/" if up > 0 else ""]
    else:
        bases = [""] + (["%s/" % here[0]] if here else [])  # the root, and the first directory (a src layout)
    found: List[str] = []
    for base in bases:
        stem = base + "/".join(parts)
        for candidate in ([stem + ".py", stem + "/__init__.py"] if parts else [base + "__init__.py"]):
            if candidate in known:
                found.append(candidate)
        for name in names:
            sub = (stem + "/" if parts else base) + name
            for candidate in (sub + ".py", sub + "/__init__.py"):
                if candidate in known:
                    found.append(candidate)
        if found:
            break
    return sorted(set(found) - {importer})


def python_module(root: Path, rel: str, text: str, known: Set[str]) -> Dict[str, Any]:
    internal: Set[str] = set()
    external: Set[str] = set()
    lines: List[Tuple[int, str]] = []
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                hit = _resolve_python(known, rel, alias.name, 0, [])
                (internal.update(hit) if hit else external.add(alias.name))
                lines.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            hit = _resolve_python(known, rel, module, node.level, [a.name for a in node.names])
            if hit:
                internal.update(hit)
            elif not node.level:
                external.add(module)
            lines.append((node.lineno, module))
    return {"internal": sorted(internal), "external": sorted(external), "lines": lines, "approximate": False}


class Lines:
    """Line numbers of offsets in a text: one pass for the newline offsets, then a bisect per lookup, so a file with many
    matches costs no more than a linear scan."""

    def __init__(self, text: str) -> None:
        self.breaks = [i for i, c in enumerate(text) if c == "\n"]

    def at(self, offset: int) -> int:
        return bisect.bisect_left(self.breaks, offset) + 1


def _js_resolve(known: Set[str], importer: str, spec: str) -> Optional[str]:
    base = os.path.normpath(os.path.join(os.path.dirname(importer), spec)).replace(os.sep, "/")
    for ext in JS_EXT:
        if base + ext in known:
            return base + ext
    return None


def _package(spec: str) -> str:
    parts = spec.split("/")
    return "/".join(parts[:2]) if spec.startswith("@") else parts[0]


def regex_module(rel: str, text: str, known: Set[str], language: str) -> Dict[str, Any]:
    internal: Set[str] = set()
    external: Set[str] = set()
    lines: List[Tuple[int, str]] = []
    where = Lines(text)
    if language in ("javascript", "typescript"):
        found = [(m.start(), m.group(1)) for m in JS_IMPORT.finditer(text)]
        for at, spec in found:
            line = where.at(at)
            if spec.startswith("."):
                hit = _js_resolve(known, rel, spec)
                if hit and hit != rel:
                    internal.add(hit)
            elif not spec.startswith("node:"):
                external.add(_package(spec))
                lines.append((line, _package(spec)))
    elif language == "java":
        by_class = {}
        resolved: Dict[str, Optional[str]] = {}
        for path in known:
            if path.endswith(".java"):
                by_class.setdefault(path[:-5], path)
        for m in JAVA_IMPORT.finditer(text):
            name, line = m.group(1), where.at(m.start(1))
            if name not in resolved:
                slash = name.replace(".", "/")
                resolved[name] = next((p for stem, p in by_class.items() if stem.endswith("/" + slash) or stem == slash), None)
            hit = resolved[name]
            if hit and hit != rel:
                internal.add(hit)
            elif not hit:
                external.add(name)
                lines.append((line, name))
    elif language == "go":
        block = [(m.start(1), q) for m in GO_IMPORT_BLOCK.finditer(text) for q in GO_QUOTED.findall(m.group(1))]
        one = [(m.start(1), m.group(1)) for m in GO_IMPORT_ONE.finditer(text)]
        for at, spec in [(a, s) for a, s in block + one]:
            external.add(spec)
            lines.append((where.at(at), spec))
    return {"internal": sorted(internal), "external": sorted(external), "lines": lines, "approximate": True}


def entry_hints(rel: str, text: str, language: str) -> List[Dict[str, Any]]:
    hints = []

    def add(kind: str, pattern: "re.Pattern[str]", detail: str) -> None:
        match = pattern.search(text)
        if match:
            hints.append({"kind": kind, "path": rel, "line": text.count("\n", 0, match.start()) + 1, "detail": detail})
    if language == "python":
        add("main", MAIN_PY, "if __name__ == '__main__' block")
    elif language == "java":
        add("main", MAIN_JAVA, "public static void main")
    elif language == "go":
        add("main", MAIN_GO, "func main()")
    add("route", ROUTE, "route or handler declaration")
    return hints


def package_json_hints(root: Path, names: List[str]) -> List[Dict[str, Any]]:
    hints = []
    for rel in names:
        if rel.rsplit("/", 1)[-1] != "package.json":
            continue
        text = _repo.read_text(root, rel)
        try:
            doc = json.loads(text or "")
        except ValueError:
            continue
        if not isinstance(doc, dict):
            continue
        for key in ("main", "bin"):
            if doc.get(key):
                hints.append({"kind": "cli" if key == "bin" else "main", "path": rel, "line": 1, "detail": "package.json %s" % key})
        scripts = doc.get("scripts")
        if isinstance(scripts, dict) and "start" in scripts:
            hints.append({"kind": "main", "path": rel, "line": 1, "detail": "package.json scripts.start"})
    return hints


def analyse(root: Path) -> Dict[str, Any]:
    root = Path(root)
    names = _repo.files(root)
    known = set(names)
    limitations: List[str] = []
    modules: List[Dict[str, Any]] = []
    signals: List[Dict[str, Any]] = []
    hints = package_json_hints(root, names)
    for rel in names:
        language = _repo.language_of(rel)
        if language not in SUPPORTED:
            continue
        text = _repo.read_text(root, rel)
        if text is None:
            limitations.append("%s: not read (binary or larger than %d bytes)" % (rel, _repo.MAX_FILE_BYTES))
            continue
        try:
            parsed = python_module(root, rel, text, known) if language == "python" else regex_module(rel, text, known, language)
        except (SyntaxError, ValueError) as exc:
            limitations.append("%s: not parsed (%s)" % (rel, type(exc).__name__))
            continue
        modules.append({"path": rel, "language": language, "imports": parsed["internal"], "external": parsed["external"],
                        "fan_in": 0, "fan_out": len(parsed["internal"]), "approximate": parsed["approximate"],
                        "description": first_description(text, language)})
        for line, name in parsed["lines"]:
            hit = classify(name)
            if hit:
                signals.append({"kind": hit[0], "name": hit[1], "path": rel, "line": line})
        where = Lines(text)
        for m in ENV_READ.finditer(text):
            signals.append({"kind": "env-var", "name": next(g for g in m.groups() if g), "path": rel, "line": where.at(m.start())})
        hints += entry_hints(rel, text, language)
    by_path = {m["path"]: m for m in modules}
    for module in modules:
        for target in module["imports"]:
            if target in by_path:
                by_path[target]["fan_in"] += 1
    if any(m["approximate"] for m in modules):
        limitations.append("JavaScript, TypeScript, Java and Go imports are read with regular expressions: those modules are approximate")
    signals.sort(key=lambda s: (s["path"], s["line"], s["kind"], s["name"]))
    return {"modules": modules, "signals": signals, "entry_hints": hints, "limitations": limitations}


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.root):
        print("error: --root must be a directory", file=sys.stderr)
        return 2
    print(json.dumps(analyse(Path(args.root)), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
