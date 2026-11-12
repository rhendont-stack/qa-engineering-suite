"""Applies a directory of SQL migrations up, down and up again on a fresh SQLite file, and reports each step as JUnit.

Usage: python3 migration_roundtrip.py --migrations DIR --db PATH --junit OUT [--rows N]
       python3 migration_roundtrip.py --copy-to-work   (writes this script to .qa/work/database/, for a container run's --input)

Run it from the project root, through the kernel (``qa run --activity db-ephemeral``), never by hand.

* ``DIR`` holds ``NNNN_<name>.up.sql`` and, for each step that can be undone, ``NNNN_<name>.down.sql``. Files of any other
  name are listed as ``unreadable`` and never run.
* ``PATH`` is the database the run creates. It must be under ``.qa/work/`` of the current directory, with no symlink on the
  way; an existing file there is replaced. Every other path is refused (exit 2) before anything is opened.
* ``OUT`` is where the JUnit report goes: under ``.qa/`` (the kernel's ``{evidence}`` on the host), or a new file outside the
  current directory (``/qa-evidence/`` in a container); never into the project, and never over an existing file outside it.

What runs, all on one connection with ``PRAGMA foreign_keys = ON``; ``ATTACH``, ``DETACH`` and ``VACUUM INTO`` are refused by an
authorizer, so a migration cannot reach another file:

1. ``up``: every up step, oldest first. Before each step every table gets ``N`` synthetic rows (default 3) if it has fewer.
   After the step, a column or table that held values and is gone, with its values in no other column of the same row (a
   moved value keeps one of its words), is a ``loss`` failure, and so are rows that disappeared.
2. ``down``: every step newest first. A step with no down file is a failure. Synthetic rows are added first; a down step that
   drops data it did not itself create (what its up step added is expected to go) is a ``loss`` failure.
3. A check that the downs left no table behind.
4. ``reup``: every up step again, on what the downs left.

Each step is one test case (``roundtrip.<phase>``, named after the step); an SQLite error is its failure. Stdout is one JSON
object (the database, the report, counts and the failures); exit 0 when every case passed, 1 when any failed, 2 on a refusal.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sqlite3
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

STEP = re.compile(r"^(\d+_[A-Za-z0-9_]+)\.(up|down)\.sql$")
DENIED = (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH)


class Refused(Exception):
    """A path or an input the script will not use; nothing was written."""


# --------------------------------------------------------------------------- paths

def _no_link_between(root: Path, path: Path) -> None:
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise Refused("%s is a symlink" % current.relative_to(root).as_posix())


def work_path(text: str, root: Path) -> Path:
    """``text`` as a path under ``<root>/.qa/work/``; refused when it leaves it or passes a symlink."""
    path = Path(os.path.abspath(os.path.join(str(root), text)))
    work = root / ".qa" / "work"
    if path == work or work not in path.parents:
        raise Refused("the database must be under .qa/work/: %s" % text)
    _no_link_between(root, path)
    return path


def report_path(text: str, root: Path) -> Path:
    """Where the JUnit report may go: under ``.qa/`` of the project or outside the project."""
    path = Path(os.path.abspath(os.path.join(str(root), text)))
    if root in path.parents and (root / ".qa") not in path.parents:
        raise Refused("the report goes under .qa/ or outside the project, never into it: %s" % text)
    if root in path.parents:
        _no_link_between(root, path)
    elif path.is_symlink() or path.exists():
        # Outside the project only a new file is written (the container's /qa-evidence/<name>): an existing file is never replaced.
        raise Refused("the report path outside the project already exists: %s" % text)
    return path


def write_report(path: Path, data: bytes, root: Path) -> None:
    """Under the project's .qa/, through write_new; outside the project, as a new file only (O_EXCL, never through a link)."""
    if root in path.parents:
        write_new(path, data)
        return
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def write_new(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` through a fresh temporary file that is never a link, then move it into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / (".%s.%s.tmp" % (path.name, secrets.token_hex(8)))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if path.is_symlink():
            raise Refused("%s became a symlink" % path.name)
        os.replace(str(temp), str(path))
    finally:
        if temp.exists() and not temp.is_symlink():
            temp.unlink()


def fresh_database(path: Path) -> None:
    """Remove a previous database at ``path`` (and its journal files); never a link or a directory."""
    for suffix in ("", "-journal", "-wal", "-shm"):
        old = Path(str(path) + suffix)
        if old.is_symlink() or old.is_dir():
            raise Refused("%s is not a regular file" % old.name)
        if old.exists():
            old.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- migrations

def read_steps(directory: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    """``(steps oldest first, unreadable names)``; a step is ``{name, up, down}`` with ``down`` None when there is none."""
    steps: Dict[str, Dict[str, Any]] = {}
    unreadable = []
    for path in sorted(directory.iterdir()):
        match = STEP.match(path.name)
        if not match or path.is_symlink() or not path.is_file():
            unreadable.append(path.name)
            continue
        steps.setdefault(match.group(1), {"name": match.group(1), "up": None, "down": None})[match.group(2)] = path
    for name, step in list(steps.items()):
        if step["up"] is None:
            unreadable.append("%s.down.sql (no up step)" % name)
            del steps[name]
    return sorted(steps.values(), key=lambda s: (int(s["name"].split("_", 1)[0]), s["name"])), unreadable


def authorizer(action: int, *_args: Any) -> int:
    return sqlite3.SQLITE_DENY if action in DENIED else sqlite3.SQLITE_OK


def connect(path: Path) -> sqlite3.Connection:
    # sqlite3 cannot open a file without following links. The path was checked for links just before (work_path, and
    # fresh_database removed any old file or link), so the only gap is the moment between that check and this open.
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.set_authorizer(authorizer)
    return connection


# --------------------------------------------------------------------------- schema and data

def tables(connection: sqlite3.Connection) -> List[str]:
    return [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def quote(name: str) -> str:
    return '"%s"' % name.replace('"', '""')


def columns(connection: sqlite3.Connection, table: str) -> List[Dict[str, Any]]:
    return [{"name": r[1], "type": (r[2] or "").upper(), "notnull": bool(r[3]), "default": r[4], "pk": r[5]}
            for r in connection.execute("PRAGMA table_info(%s)" % quote(table))]


def foreign_keys(connection: sqlite3.Connection, table: str) -> List[Tuple[str, str, Optional[str]]]:
    """``(column, parent table, parent column or None for the parent's key)`` per foreign key column."""
    return [(r[3], r[2], r[4]) for r in connection.execute("PRAGMA foreign_key_list(%s)" % quote(table))]


def dependency_order(connection: sqlite3.Connection) -> List[str]:
    names = tables(connection)
    parents = {t: {p for _c, p, _k in foreign_keys(connection, t) if p in names and p != t} for t in names}
    ordered: List[str] = []
    while len(ordered) < len(names):
        ready = [t for t in names if t not in ordered and parents[t] <= set(ordered)]
        ordered += ready or [t for t in names if t not in ordered]      # a cycle: the rest in name order
    return ordered


def rowid_alias(cols: List[Dict[str, Any]]) -> Optional[str]:
    pks = [c for c in cols if c["pk"]]
    return pks[0]["name"] if len(pks) == 1 and pks[0]["type"] == "INTEGER" else None


class Synthetic:
    """Distinct values: each text cell holds two words no other cell holds, so a moved value can be recognised by a word."""

    def __init__(self) -> None:
        self.n = 0

    def value(self, column: Dict[str, Any]) -> Any:
        self.n += 1
        kind = column["type"]
        if "INT" in kind:
            return self.n
        if any(k in kind for k in ("REAL", "FLOA", "DOUB", "NUM", "DEC")):
            return self.n + 0.5
        if "BLOB" in kind:
            return bytes([self.n % 256])
        return "w%dx w%dy" % (self.n, self.n)


def parent_keys(connection: sqlite3.Connection, parent: str, key: Optional[str]) -> List[Any]:
    column = key or rowid_alias(columns(connection, parent)) or "rowid"
    return [r[0] for r in connection.execute("SELECT %s FROM %s ORDER BY 1" % (quote(column) if column != "rowid" else "rowid", quote(parent)))]


def add_rows(connection: sqlite3.Connection, rows: int, synthetic: Synthetic) -> List[str]:
    """Bring every table up to ``rows`` rows; returns notes on tables that refused them."""
    notes = []
    for table in dependency_order(connection):
        have = connection.execute("SELECT COUNT(*) FROM %s" % quote(table)).fetchone()[0]
        cols, fks = columns(connection, table), {c: (p, k) for c, p, k in foreign_keys(connection, table)}
        alias = rowid_alias(cols)
        for index in range(have, rows):
            for lean in (False, True):                       # second try: leave out what has a default or may be null
                values = {}
                for column in cols:
                    if column["name"] == alias:
                        continue
                    if column["name"] in fks:
                        keys = parent_keys(connection, *fks[column["name"]])
                        if keys:
                            values[column["name"]] = keys[index % len(keys)]
                            continue
                    if lean and (column["default"] is not None or not column["notnull"]):
                        continue
                    values[column["name"]] = synthetic.value(column)
                names = list(values)
                sql = ("INSERT INTO %s (%s) VALUES (%s)" % (quote(table), ", ".join(quote(n) for n in names), ", ".join("?" * len(names)))
                       if names else "INSERT INTO %s DEFAULT VALUES" % quote(table))
                try:
                    connection.execute(sql, [values[n] for n in names])
                    break
                except sqlite3.Error as exc:
                    if lean:
                        notes.append("%s: synthetic row refused: %s" % (table, exc))
            else:
                break
    return notes


def snapshot(connection: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """``{table: {columns, rows: {row key: {column: value}}}}``."""
    found = {}
    for table in tables(connection):
        cols = [c["name"] for c in columns(connection, table)]
        try:
            cursor = connection.execute("SELECT rowid, * FROM %s" % quote(table))
            rows = {r[0]: dict(zip(cols, r[1:])) for r in cursor}
        except sqlite3.Error:                                 # a WITHOUT ROWID table: rows by position
            rows = {i: dict(zip(cols, r)) for i, r in enumerate(connection.execute("SELECT * FROM %s" % quote(table)))}
        found[table] = {"columns": cols, "rows": rows}
    return found


def _words(value: Any) -> Set[str]:
    return set(str(value).split()) if value not in (None, "", b"") else set()


def losses(before: Dict[str, Dict[str, Any]], after: Dict[str, Dict[str, Any]], expected: Set[str]) -> List[str]:
    """What held values before and is gone after, unless it is in ``expected`` (``table`` or ``table.column``) or its values
    turn up elsewhere: a dropped column's values in other columns of the same row, a dropped table's in a new table."""
    found = []
    new_tables = [t for t in after if t not in before]
    for table, old in sorted(before.items()):
        if not old["rows"] or table in expected:
            continue
        if table not in after:
            held = set().union(*(_words(v) for row in old["rows"].values() for v in row.values()))
            moved = any(held <= set().union(*(_words(v) for row in after[t]["rows"].values() for v in row.values())) for t in new_tables)
            if not moved:
                found.append("table %s dropped with %d rows" % (table, len(old["rows"])))
            continue
        now = after[table]
        if len(now["rows"]) < len(old["rows"]):
            found.append("table %s lost %d of %d rows" % (table, len(old["rows"]) - len(now["rows"]), len(old["rows"])))
        for column in old["columns"]:
            if column in now["columns"] or "%s.%s" % (table, column) in expected:
                continue
            lost = 0
            held = 0
            for key, row in old["rows"].items():
                words = _words(row.get(column))
                if not words:
                    continue
                held += 1
                others = set().union(*(_words(v) for v in (now["rows"].get(key) or {}).values()))
                if not words & others:
                    lost += 1
            if lost:
                found.append("column %s.%s dropped: %d of %d values found in no other column of their row" % (table, column, lost, held))
    return found


def created(before: Dict[str, Dict[str, Any]], after: Dict[str, Dict[str, Any]]) -> Set[str]:
    out = {t for t in after if t not in before}
    for table in after:
        if table in before:
            out |= {"%s.%s" % (table, c) for c in after[table]["columns"] if c not in before[table]["columns"]}
    return out


# --------------------------------------------------------------------------- the run

def run(steps: List[Dict[str, Any]], connection: sqlite3.Connection, rows: int, display: Dict[Path, str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    cases: List[Dict[str, Any]] = []
    notes: List[str] = []
    synthetic = Synthetic()
    made: Dict[str, Set[str]] = {}

    def case(phase: str, step: str, failure: Optional[str] = None) -> None:
        cases.append({"phase": phase, "step": step, "failure": failure})

    def apply(path: Path) -> Optional[str]:
        try:
            connection.executescript(path.read_text(encoding="utf-8"))
            return None
        except sqlite3.Error as exc:
            return "%s: %s" % (display[path], exc)

    for step in steps:
        notes += add_rows(connection, rows, synthetic)
        before = snapshot(connection)
        error = apply(step["up"])
        after = snapshot(connection)
        made[step["name"]] = created(before, after)
        case("up", step["name"], error)
        lost = losses(before, after, set())
        if lost:
            case("loss", "up " + step["name"], "%s: %s" % (display[step["up"]], "; ".join(lost)))
    for step in reversed(steps):
        if step["down"] is None:
            case("down", step["name"], "%s has no down step: %s.down.sql is missing, so nothing undoes it"
                 % (display[step["up"]], step["name"]))
            continue
        notes += add_rows(connection, rows, synthetic)
        before = snapshot(connection)
        error = apply(step["down"])
        case("down", step["name"], error)
        lost = losses(before, snapshot(connection), made.get(step["name"], set()))
        if lost:
            case("loss", "down " + step["name"], "%s: %s" % (display[step["down"]], "; ".join(lost)))
    left = tables(connection)
    case("down", "leaves no table", "tables left after every down step: %s" % ", ".join(left) if left else None)
    for step in steps:
        case("reup", step["name"], apply(step["up"]))
    return cases, sorted(set(notes))


def junit(cases: List[Dict[str, Any]], notes: List[str]) -> bytes:
    failures = [c for c in cases if c["failure"]]
    suite = ET.Element("testsuite", name="migration-roundtrip", tests=str(len(cases)), failures=str(len(failures)), errors="0", skipped="0")
    for c in cases:
        element = ET.SubElement(suite, "testcase", classname="roundtrip.%s" % c["phase"], name=c["step"])
        if c["failure"]:
            ET.SubElement(element, "failure", message=c["failure"][:1000]).text = c["failure"]
    if notes:
        ET.SubElement(suite, "system-out").text = "\n".join(notes)
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(suite, encoding="utf-8")


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--migrations")
    parser.add_argument("--db")
    parser.add_argument("--junit")
    parser.add_argument("--rows", type=int, default=3)
    parser.add_argument("--copy-to-work", action="store_true")
    args = parser.parse_args(argv)
    root = Path(os.getcwd()).resolve()
    if args.copy_to_work:
        try:
            target = work_path(".qa/work/database/migration_roundtrip.py", root)
        except Refused as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
            return 2
        write_new(target, Path(__file__).read_bytes())
        print(json.dumps({"ok": True, "copied": target.relative_to(root).as_posix()}, sort_keys=True))
        return 0
    if not (args.migrations and args.db and args.junit):
        parser.error("--migrations, --db and --junit are required")
    try:
        if not 1 <= args.rows <= 1000:
            raise Refused("--rows must be between 1 and 1000")
        database = work_path(args.db, root)
        report = report_path(args.junit, root)
        directory = Path(os.path.abspath(os.path.join(str(root), args.migrations)))
        if directory.is_symlink() or not directory.is_dir():
            raise Refused("no migrations directory at %s" % args.migrations)
        steps, unreadable = read_steps(directory)
        if not steps:
            raise Refused("no NNNN_<name>.up.sql file in %s" % args.migrations)
        fresh_database(database)
    except Refused as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    display = {}
    for step in steps:
        for kind in ("up", "down"):
            if step[kind] is not None:
                path = step[kind]
                display[path] = path.relative_to(root).as_posix() if root in path.parents else path.name
    connection = connect(database)
    try:
        cases, notes = run(steps, connection, args.rows, display)
    finally:
        connection.close()
    notes = ["unreadable: %s" % name for name in unreadable] + notes
    write_report(report, junit(cases, notes), root)
    failures = [{"phase": c["phase"], "step": c["step"], "failure": c["failure"]} for c in cases if c["failure"]]
    print(json.dumps({"ok": not failures, "db": database.relative_to(root).as_posix(), "junit": str(report), "steps": len(steps),
                      "tests": len(cases), "failed": len(failures), "failures": failures, "unreadable": unreadable}, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
