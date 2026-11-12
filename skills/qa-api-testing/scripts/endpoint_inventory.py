"""Builds the endpoint inventory of an HTTP API: what the contract documents, what the code routes, and where they drift.

Usage, from the project root:
  python3 <skill-dir>/scripts/endpoint_inventory.py scan --spec PATH [--root DIR] [--out .qa/work/api/inventory.json]
  python3 <skill-dir>/scripts/endpoint_inventory.py mark --inventory .qa/work/api/inventory.json --operation "METHOD /path"
      --status verified|not_run|blocked [--execution EXE-...] [--reason TEXT]
  python3 <skill-dir>/scripts/endpoint_inventory.py assets [--dest .qa/work/api]

``scan`` reads an OpenAPI 3.x document: JSON (and the JSON-compatible subset of YAML) with the standard library, and the
block-style YAML subset that paths, methods, responses and security need with a small reader of its own. A line that reader
cannot read (anchors, aliases, tags, nested flow collections, a key it cannot parse) is listed under ``unreadable`` with
its line number, never guessed. It then scans the code under ``--root`` for routes declared in the usual forms (a
``("METHOD", "/path", handler, ...)`` route table on the standard library, Flask ``@x.route``, Flask or FastAPI
``@x.get`` and the like, Express ``app.get('/path', ...)``), each cited by file and line and marked ``approximate``: a scan
of source text is not a run. For Python handlers it also lists the status-code literals the handler returns
(``reply(200, ...)``, ``abort(404)``, ``return body, 201``), approximate as well.

Drift compares the two: ``undocumented`` (routed, not in the contract), ``unimplemented`` (in the contract, no route
found), ``method_mismatch`` (one path, different methods on each side) and ``status_mismatch`` (a status literal the
handler returns that the operation does not document). Every operation starts ``not_run``: a status comes only from a
recorded check, through ``mark``. ``mark`` sets ``verified`` only with an execution ID, and ``not_run`` or ``blocked``
only with a reason.

``assets`` copies the skill's tool configurations (``spectral.yaml``, ``schemathesis-baseline.toml``, ``smoke.hurl``) into
``.qa/work/api/``, where ``qa run --input`` takes them from; a file already there is kept, never overwritten.

The inventory and the copies are written only under ``.qa/work/``. Standard library only; reads the project, never changes it. Prints one
JSON summary on stdout; exit 0, 1 when the contract cannot be read at all, 2 on bad arguments.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
EXE = re.compile(r"^EXE-[0-9A-Z]{26}$")
STATUSES = ("verified", "not_run", "blocked")
SKIP_DIRS = {".git", ".qa", ".claude", "node_modules", "__pycache__", ".venv", "venv", "env", ".tox", "dist", "build", "site-packages",
             "tests", "test", "__tests__", "spec", "fixtures"}
CODE_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".ts")
NOT_RUN_REASON = "no recorded check covers it yet"
ASSETS = ("spectral.yaml", "schemathesis-baseline.toml", "smoke.hurl")


# --------------------------------------------------------------------------- the block-style YAML subset

class _Line:
    def __init__(self, number: int, indent: int, text: str) -> None:
        self.number, self.indent, self.text = number, indent, text


def _strip_comment(text: str) -> str:
    quote = None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index].rstrip()
    return text.rstrip()


_KEY = re.compile(r"""^("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^\s'"\[\]{}#&*!|>%@`-][^:#]*?|-[^\s:][^:#]*?)\s*:(?:\s+(.*))?$""")
_UNSUPPORTED = re.compile(r"^[&*!]|^\?\s|^%")


class BlockYaml:
    """A reader for the block-style YAML subset: mappings, sequences, plain and quoted scalars, empty or simple flow
    collections and block scalars. What it cannot read goes to ``unreadable`` with its line number."""

    def __init__(self, text: str) -> None:
        self.unreadable: List[Dict[str, Any]] = []
        self.lines: List[_Line] = []
        block_indent: Optional[int] = None
        for number, raw in enumerate(text.splitlines(), start=1):
            if "\t" in raw[:len(raw) - len(raw.lstrip())]:
                self.unreadable.append({"line": number, "text": raw.strip()[:120], "why": "tab indentation"})
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            if block_indent is not None:
                if not raw.strip() or indent > block_indent:
                    continue                            # the body of a block scalar: text, not structure
                block_indent = None
            body = _strip_comment(raw.strip())
            if not body or body in ("---", "...") or body.startswith("%"):
                continue
            self.lines.append(_Line(number, indent, body))
            if re.search(r"(?::|^-)\s*[|>][-+0-9]*$", body):
                block_indent = indent
        self.pos = 0

    def bad(self, line: _Line, why: str) -> None:
        self.unreadable.append({"line": line.number, "text": line.text[:120], "why": why})

    def parse(self) -> Any:
        if not self.lines:
            return None
        node = self.block(self.lines[0].indent)
        while self.pos < len(self.lines):
            self.bad(self.lines[self.pos], "outside the document's structure")
            self.pos += 1
        return node

    def block(self, indent: int) -> Any:
        line = self.lines[self.pos]
        return self.sequence(indent) if self._dash(line.text) else self.mapping(indent)

    @staticmethod
    def _dash(text: str) -> bool:
        return text == "-" or text.startswith("- ")

    def mapping(self, indent: int) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if line.indent < indent or (line.indent == indent and self._dash(line.text)):
                break
            if line.indent > indent:
                self.bad(line, "indented deeper than its mapping")
                self.pos += 1
                continue
            match = _KEY.match(line.text)
            if not match or _UNSUPPORTED.search(line.text):
                self.bad(line, "not a key the reader understands")
                self.pos += 1
                self._skip_deeper(indent)
                continue
            key = self.scalar_key(match.group(1))
            value = match.group(2)
            self.pos += 1
            out[key] = self.value_after(line, indent, value)
        return out

    def sequence(self, indent: int) -> List[Any]:
        out: List[Any] = []
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if line.indent != indent or not self._dash(line.text):
                if line.indent > indent:
                    self.bad(line, "indented deeper than its sequence")
                    self.pos += 1
                    continue
                break
            rest = line.text[1:].lstrip()
            if not rest:
                self.pos += 1
                out.append(self.child(indent))
                continue
            offset = indent + (len(line.text) - len(rest))
            if _KEY.match(rest) and not rest.startswith(("'", '"')) or re.match(r"""^("[^"]*"|'[^']*')\s*:""", rest):
                self.lines[self.pos] = _Line(line.number, offset, rest)   # "- key: value" opens a mapping at the key's column
                out.append(self.mapping(offset))
                continue
            self.pos += 1
            out.append(self.scalar(rest, line))
        return out

    def child(self, indent: int) -> Any:
        if self.pos < len(self.lines) and self.lines[self.pos].indent > indent:
            return self.block(self.lines[self.pos].indent)
        return None

    def value_after(self, line: _Line, indent: int, value: Optional[str]) -> Any:
        if value is None or value == "":
            if self.pos < len(self.lines):
                nxt = self.lines[self.pos]
                if nxt.indent > indent:
                    return self.block(nxt.indent)
                if nxt.indent == indent and self._dash(nxt.text):
                    return self.sequence(indent)
            return None
        if re.match(r"^[|>][-+0-9]*$", value):
            return ""                                   # a block scalar: its text is not structure the inventory needs
        result = self.scalar(value, line)
        self._skip_deeper(indent)                       # a wrapped plain scalar, or the body under a refused anchor or tag
        return result

    def _skip_deeper(self, indent: int) -> None:
        while self.pos < len(self.lines) and self.lines[self.pos].indent > indent:
            self.pos += 1

    @staticmethod
    def scalar_key(text: str) -> str:
        text = text.strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
            return text[1:-1].replace("''", "'") if text[0] == "'" else json.loads(text)
        return text

    def scalar(self, text: str, line: _Line) -> Any:
        text = text.strip()
        if _UNSUPPORTED.search(text):
            self.bad(line, "anchors, aliases and tags are not read")
            return None
        if text in ("[]", "{}"):
            return [] if text == "[]" else {}
        if text.startswith("[") and text.endswith("]"):
            inner = text[1:-1]
            if any(c in inner for c in "[]{}"):
                self.bad(line, "a nested flow collection")
                return None
            return [self.scalar(part, line) for part in inner.split(",") if part.strip()]
        if text.startswith("{"):
            try:
                return json.loads(text)
            except ValueError:
                self.bad(line, "a flow mapping that is not JSON")
                return None
        if len(text) >= 2 and text[0] == text[-1] == '"':
            try:
                return json.loads(text)
            except ValueError:
                return text[1:-1]
        if len(text) >= 2 and text[0] == text[-1] == "'":
            return text[1:-1].replace("''", "'")
        if text in ("true", "false"):
            return text == "true"
        if text in ("null", "~"):
            return None
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        return text


def read_contract(path: Path) -> Tuple[Optional[Dict[str, Any]], str, List[Dict[str, Any]]]:
    """``(document, format, unreadable)``; the document is None when nothing could be read."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if text.lstrip().startswith("{"):
        try:
            document = json.loads(text)
        except ValueError as exc:
            line = getattr(exc, "lineno", 1)
            return None, "json", [{"line": line, "text": "", "why": "not valid JSON: %s" % exc}]
        return (document if isinstance(document, dict) else None), "json", []
    reader = BlockYaml(text)
    document = reader.parse()
    return (document if isinstance(document, dict) else None), "yaml-block", reader.unreadable


def normal(method: str, path: str) -> str:
    """``METHOD /path`` with every path parameter written ``{}``, the form operations are compared in."""
    path = re.sub(r"\{[^}/]*\}|<[^>/]*>|:[A-Za-z_][A-Za-z0-9_]*", "{}", path.strip())
    if len(path) > 1:
        path = path.rstrip("/")
    return "%s %s" % (method.upper(), path)


def _security_names(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    names: List[str] = []
    for requirement in value:
        if isinstance(requirement, dict):
            names.extend(str(k) for k in requirement)
    return sorted(set(names))


def documented_operations(document: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    default_security = _security_names(document.get("security"))
    paths = document.get("paths") if isinstance(document.get("paths"), dict) else {}
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method in METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            responses = operation.get("responses") if isinstance(operation.get("responses"), dict) else {}
            security = _security_names(operation["security"]) if "security" in operation else default_security
            out.append({"operation": "%s %s" % (method.upper(), path), "method": method.upper(), "path": str(path),
                        "operation_id": operation.get("operationId"), "responses": sorted(str(code) for code in responses),
                        "security": security})
    return out


# --------------------------------------------------------------------------- the route scan

_TUPLE = re.compile(r"""^\s*\(\s*["'](GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)["']\s*,\s*["'](/[^"']*)["']\s*,\s*([A-Za-z_][\w.]*)""")
_FLASK_ROUTE = re.compile(r"""^\s*@[\w.]+\.route\(\s*["'](/[^"']*)["'](.*)$""")
_DECORATOR = re.compile(r"""^\s*@[\w.]+\.(get|post|put|delete|patch|head|options)\(\s*["'](/[^"']*)["'](.*)$""")
_EXPRESS = re.compile(r"""\b[\w$]+\.(get|post|put|delete|patch|head|options|all)\(\s*["'`](/[^"'`]*)["'`]""")
_STATUS_CALLEE = re.compile(r"(?i)repl|respon|error|abort|status|exception")


def _code_files(root: Path) -> List[Path]:
    found = []
    for here, dirs, names in os.walk(str(root)):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            if not name.endswith(CODE_SUFFIXES) or re.match(r"^(?:test_.*|.*_test\.py|.*\.(?:test|spec)\.[cm]?[jt]s)$", name):
                continue
            path = Path(here) / name
            if path.is_file() and not path.is_symlink():
                found.append(path)
    return found


def _int_status(node: Any) -> Optional[int]:
    if isinstance(node, ast.Constant) and type(node.value) is int and 100 <= node.value <= 599:
        return node.value
    return None


def _callee(node: ast.Call) -> str:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def handler_statuses(function: Any) -> List[Dict[str, int]]:
    """Status-code literals a Python handler returns: the first argument (or a ``status``/``status_code`` keyword) of a call
    whose name reads like a response or an error, and an integer in a returned tuple. Nested functions are not entered."""
    found: List[Dict[str, int]] = []
    stack = list(function.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, ast.Call) and _STATUS_CALLEE.search(_callee(node)):
            values = [_int_status(node.args[0])] if node.args else []
            values += [_int_status(k.value) for k in node.keywords if k.arg in ("status", "status_code")]
            found += [{"status": v, "line": node.lineno} for v in values if v is not None]
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
            found += [{"status": v, "line": node.lineno} for v in (_int_status(e) for e in node.value.elts[:2]) if v is not None]
        stack.extend(ast.iter_child_nodes(node))
    unique = {(f["status"], f["line"]): f for f in found}
    return [unique[k] for k in sorted(unique, key=lambda k: (k[1], k[0]))]


def _functions(tree: ast.AST) -> Dict[str, Any]:
    return {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _decorated(tree: ast.AST) -> Dict[int, Any]:
    """{line of a decorator: the function it decorates}."""
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                found[decorator.lineno] = node
    return found


def scan_routes(root: Path) -> List[Dict[str, Any]]:
    routes: List[Dict[str, Any]] = []
    for path in _code_files(root):
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        tree = None
        if path.suffix == ".py":
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError):
                tree = None
        functions = _functions(tree) if tree is not None else {}
        decorated = _decorated(tree) if tree is not None else {}
        for number, line in enumerate(text.splitlines(), start=1):
            found: List[Tuple[str, str, Any, Optional[int]]] = []    # (method, path, handler node, decorator default status)
            if path.suffix == ".py":
                match = _TUPLE.match(line)
                if match:
                    found.append((match.group(1), match.group(2), functions.get(match.group(3).split(".")[-1]), None))
                match = _FLASK_ROUTE.match(line)
                if match:
                    methods = re.findall(r"""["'](GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)["']""", match.group(2), re.I) or ["GET"]
                    found += [(m.upper(), match.group(1), decorated.get(number), None) for m in methods]
                match = _DECORATOR.match(line)
                if match:
                    code = re.search(r"status_code\s*=\s*(\d{3})", match.group(3))
                    found.append((match.group(1).upper(), match.group(2), decorated.get(number), int(code.group(1)) if code else None))
            else:
                for match in _EXPRESS.finditer(line):
                    methods = ["GET", "POST", "PUT", "DELETE", "PATCH"] if match.group(1) == "all" else [match.group(1).upper()]
                    found += [(m, match.group(2), None, None) for m in methods]
            for method, route, handler, default in found:
                statuses = handler_statuses(handler) if handler is not None else []
                if default is not None:
                    statuses.insert(0, {"status": default, "line": number})
                routes.append({"operation": "%s %s" % (method, route), "method": method, "path": route, "file": rel, "line": number,
                               "approximate": True, "statuses": statuses})
    return routes


# --------------------------------------------------------------------------- drift and the inventory

def _documents_status(responses: List[str], status: int) -> bool:
    for code in responses:
        if code == str(status) or code.lower() == "default" or (len(code) == 3 and code[1:].upper() == "XX" and code[0] == str(status)[0]):
            return True
    return False


def build(spec_path: Path, spec_rel: str, root: Path) -> Tuple[Dict[str, Any], bool]:
    document, fmt, unreadable = read_contract(spec_path)
    documented = documented_operations(document) if document else []
    routes = scan_routes(root)
    by_doc = {normal(d["method"], d["path"]): d for d in documented}
    by_code: Dict[str, List[Dict[str, Any]]] = {}
    for route in routes:
        by_code.setdefault(normal(route["method"], route["path"]), []).append(route)
    operations: List[Dict[str, Any]] = []
    drift: Dict[str, List[Dict[str, Any]]] = {"undocumented": [], "unimplemented": [], "method_mismatch": [], "status_mismatch": []}
    for key in sorted(set(by_doc) | set(by_code)):
        doc, code = by_doc.get(key), by_code.get(key, [])
        name = doc["operation"] if doc else code[0]["operation"]
        entry: Dict[str, Any] = {"operation": name, "documented": doc is not None, "implemented": bool(code) if routes else None,
                                 "responses": doc["responses"] if doc else [], "security": doc["security"] if doc else None,
                                 "code": [{k: r[k] for k in ("file", "line", "approximate", "statuses")} for r in code],
                                 "status": "not_run", "reason": NOT_RUN_REASON, "executions": []}
        if doc and doc.get("operation_id"):
            entry["operation_id"] = doc["operation_id"]
        if not doc:
            entry["reason"] = "routed in the code, not in the contract; " + NOT_RUN_REASON
            drift["undocumented"] += [{"operation": name, "file": r["file"], "line": r["line"], "approximate": True} for r in code]
        elif routes and not code:
            drift["unimplemented"].append({"operation": name})
        if doc:
            for route in code:
                for found in route["statuses"]:
                    if not _documents_status(doc["responses"], found["status"]):
                        drift["status_mismatch"].append({"operation": name, "returned": found["status"], "file": route["file"],
                                                         "line": found["line"], "documented": doc["responses"], "approximate": True})
        operations.append(entry)
    paths_doc: Dict[str, set] = {}
    paths_code: Dict[str, set] = {}
    for key in by_doc:
        paths_doc.setdefault(key.split(" ", 1)[1], set()).add(key.split(" ", 1)[0])
    for key in by_code:
        paths_code.setdefault(key.split(" ", 1)[1], set()).add(key.split(" ", 1)[0])
    for path in sorted(set(paths_doc) & set(paths_code)):
        if paths_doc[path] != paths_code[path]:
            drift["method_mismatch"].append({"path": path, "documented": sorted(paths_doc[path]), "implemented": sorted(paths_code[path])})
    inventory = {"version": 1, "spec": spec_rel, "spec_format": fmt, "unreadable": unreadable,
                 "routes_found": len(routes), "operations": operations, "drift": drift,
                 "note": "statuses start not_run; code locations and returned statuses are approximate (a scan of source text)"}
    return inventory, document is not None


# --------------------------------------------------------------------------- the command line

def inside_work(path: Path) -> bool:
    work = (Path.cwd() / ".qa" / "work").resolve()
    try:
        resolved = (Path.cwd() / path).resolve()
    except OSError:
        return False
    return resolved != work and str(resolved).startswith(str(work) + os.sep)


def emit(body: Dict[str, Any], code: int) -> int:
    print(json.dumps(body, sort_keys=True))
    return code


def write(path: Path, inventory: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(temporary), str(path))


def cmd_scan(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if not inside_work(out):
        return emit({"ok": False, "error": "the inventory is written only under .qa/work/: %s" % args.out}, 2)
    spec = Path(args.spec)
    if not spec.is_file():
        return emit({"ok": False, "error": "no contract at %s" % args.spec}, 2)
    root = Path(args.root)
    inventory, readable = build(spec, args.spec, root)
    write(out, inventory)
    drift = inventory["drift"]
    return emit({"ok": readable, "inventory": args.out, "documented": sum(1 for o in inventory["operations"] if o["documented"]),
                 "routes_found": inventory["routes_found"], "unreadable": len(inventory["unreadable"]),
                 "drift": {k: [d.get("operation") or d.get("path") for d in v] for k, v in drift.items()}}, 0 if readable else 1)


def cmd_mark(args: argparse.Namespace) -> int:
    path = Path(args.inventory)
    if not inside_work(path) or not path.is_file():
        return emit({"ok": False, "error": "no inventory under .qa/work/ at %s" % args.inventory}, 2)
    if args.status == "verified" and not (args.execution and EXE.match(args.execution)):
        return emit({"ok": False, "error": "verified needs --execution with the EXE- ID of the recorded run that checked it"}, 2)
    if args.status in ("not_run", "blocked") and not (args.reason and args.reason.strip()):
        return emit({"ok": False, "error": "%s needs --reason" % args.status}, 2)
    if args.execution and not EXE.match(args.execution):
        return emit({"ok": False, "error": "not an execution ID: %s" % args.execution}, 2)
    inventory = json.loads(path.read_text(encoding="utf-8"))
    wanted = {normal(*op.strip().split(" ", 1)) for op in args.operation if " " in op.strip()}
    if len(wanted) != len(args.operation):
        return emit({"ok": False, "error": "each --operation is METHOD /path"}, 2)
    changed = []
    for entry in inventory.get("operations", []):
        key = normal(*entry["operation"].split(" ", 1))
        if key in wanted:
            entry["status"] = args.status
            entry["reason"] = args.reason.strip() if args.reason else None
            if args.execution and args.execution not in entry.setdefault("executions", []):
                entry["executions"].append(args.execution)
            if args.status == "verified":
                entry["execution"] = args.execution
            else:
                entry.pop("execution", None)
            changed.append(entry["operation"])
    missing = sorted(wanted - {normal(*o.split(" ", 1)) for o in changed})
    if missing:
        return emit({"ok": False, "error": "not in the inventory: %s" % ", ".join(missing)}, 2)
    write(path, inventory)
    return emit({"ok": True, "inventory": args.inventory, "marked": changed, "status": args.status}, 0)


def cmd_assets(args: argparse.Namespace) -> int:
    dest = Path(args.dest)
    if not inside_work(dest / "x"):
        return emit({"ok": False, "error": "the copies are written only under .qa/work/: %s" % args.dest}, 2)
    source = Path(__file__).resolve().parent.parent / "assets"
    dest.mkdir(parents=True, exist_ok=True)
    copied, kept = [], []
    for name in ASSETS:
        target = dest / name
        if target.exists() or target.is_symlink():
            kept.append((dest / name).as_posix())
            continue
        target.write_bytes((source / name).read_bytes())
        copied.append(target.as_posix())
    return emit({"ok": True, "copied": copied, "kept": kept}, 0)


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", help="read the contract and the code routes; write the inventory with its drift")
    scan.add_argument("--spec", required=True, help="the OpenAPI 3.x document, relative to the project root")
    scan.add_argument("--root", default=".", help="the code to scan for routes (default: the project root)")
    scan.add_argument("--out", default=".qa/work/api/inventory.json")
    mark = sub.add_parser("mark", help="set the status of operations from a recorded check")
    mark.add_argument("--inventory", default=".qa/work/api/inventory.json")
    mark.add_argument("--operation", action="append", required=True, metavar="'METHOD /path'")
    mark.add_argument("--status", required=True, choices=STATUSES)
    mark.add_argument("--execution", metavar="EXE")
    mark.add_argument("--reason")
    assets = sub.add_parser("assets", help="copy the tool configurations into .qa/work/api/ (existing files are kept)")
    assets.add_argument("--dest", default=".qa/work/api")
    args = parser.parse_args(argv)
    return {"scan": cmd_scan, "mark": cmd_mark, "assets": cmd_assets}[args.command](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
