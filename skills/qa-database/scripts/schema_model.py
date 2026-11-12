"""Reads a directory of SQL migrations into a schema model and lists the integrity leads a static reading can show.

Usage: python3 schema_model.py --migrations DIR [--code PATH ...] [--out .qa/work/database/schema-model.json]

Run from the project root. It reads files and writes only the model, under ``.qa/work/``; it opens no database and runs nothing.

The up files (``*.up.sql``, else every ``*.sql``) are read oldest first, in the subset of SQL this reader understands:
``CREATE TABLE`` (column types, ``NOT NULL``, ``PRIMARY KEY``, ``UNIQUE``, ``REFERENCES``, ``DEFAULT``, identity columns, and the
table constraints ``PRIMARY KEY``, ``UNIQUE`` and ``FOREIGN KEY``), ``ALTER TABLE`` (``ADD``/``DROP``/``RENAME`` a column,
``RENAME TO``, ``ADD`` a ``UNIQUE`` or ``FOREIGN KEY`` constraint), ``CREATE [UNIQUE] INDEX``, ``DROP TABLE``, ``DROP INDEX``,
and ``INSERT``, ``UPDATE`` and ``DELETE`` (noted, not modelled). Every other statement is listed as ``unreadable`` with its
``path:line``: read it yourself, never guess.

Leads, each with ``rule``, ``location`` (``path:line``), ``detail`` and ``approximate: true``; a lead is not a result:

* ``missing-foreign-key``: a column named ``*_id`` (other than the table's own key) that references nothing.
* ``missing-uniqueness``: a natural identifier (``email``, ``username``, ``login``, ``handle``, ``slug``, ``sku``, ``code``,
  ``external_id``, ``phone``) that no primary key, ``UNIQUE`` constraint or unique index covers on its own.
* ``drop-without-copy``: an up step that drops a column (or a table) holding data, with no ``UPDATE`` or ``INSERT … SELECT``
  in the same file that reads it first.
* ``missing-down-step``: an up step with no down file beside it, when the directory uses ``.up.sql``/``.down.sql``.
* ``unindexed-foreign-key``: a referencing column that no index, primary key or unique constraint leads with; a lookup or
  a join on it scans the table. A PostgreSQL plan (``references/tools.md``) is the measurement.
* ``not-null-without-value``: an ``INSERT INTO t (…)`` in the code (``--code``, Python files) that leaves out a ``NOT NULL``
  column with no default.

Stdout is one JSON object: the model's path, counts, the leads and the unreadable statements. Exit 0, or 2 on a refusal.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

IDENTITY_NAMES = re.compile(r"^(?:email|e_mail|email_address|username|user_name|login|handle|slug|sku|code|external_id|phone|phone_number)$", re.I)
NAME = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*)'
QUALIFIED = r"%s(?:\s*\.\s*%s)?" % (NAME, NAME)


class Refused(Exception):
    pass


def bare(name: str) -> str:
    name = name.strip()
    if "." in name and not name.startswith(('"', "`", "[")):
        name = name.split(".")[-1]
    return name.strip('"`[]').lower()


# --------------------------------------------------------------------------- paths and writing

def work_path(text: str, root: Path) -> Path:
    path = Path(os.path.abspath(os.path.join(str(root), text)))
    work = root / ".qa" / "work"
    if path == work or work not in path.parents:
        raise Refused("the model is written under .qa/work/ only: %s" % text)
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise Refused("%s is a symlink" % current.relative_to(root).as_posix())
    return path


def write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / (".%s.%s.tmp" % (path.name, secrets.token_hex(8)))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(str(temp), str(path))
    finally:
        if temp.exists() and not temp.is_symlink():
            temp.unlink()


def rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix() if root in path.parents else path.as_posix()


# --------------------------------------------------------------------------- statements with line numbers

def blank_comments(text: str) -> str:
    """``text`` with comments replaced by spaces (newlines kept) and string contents kept, so offsets keep their lines."""
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch in ("'", '"'):
            j = i + 1
            while j < n and not (text[j] == ch and (j + 1 >= n or text[j + 1] != ch)):
                j += 2 if text[j] == ch else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("--", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r"[^\n]", " ", text[i:j]))
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def statements(text: str) -> List[Tuple[int, str]]:
    """``(first line, statement text)`` per statement, split at ``;`` outside quotes and parentheses."""
    clean = blank_comments(text)
    found, start, depth, quote = [], 0, 0, None
    for i, ch in enumerate(clean):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == ";" and depth == 0:
            found.append((start, clean[start:i]))
            start = i + 1
    found.append((start, clean[start:]))
    out = []
    for offset, body in found:
        lead = len(body) - len(body.lstrip())
        if body.strip():
            out.append((clean.count("\n", 0, offset + lead) + 1, body.strip()))
    return out


def split_top(text: str) -> List[Tuple[int, str]]:
    """``(offset, part)`` of ``text`` split at commas outside parentheses and quotes."""
    parts, start, depth, quote = [], 0, 0, None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append((start, text[start:i]))
            start = i + 1
    parts.append((start, text[start:]))
    return [(o + len(p) - len(p.lstrip()), p.strip()) for o, p in parts if p.strip()]


def names_in(group: str) -> List[str]:
    return [bare(re.split(r"\s+", p.strip())[0]) for _o, p in split_top(group) if p.strip()]


# --------------------------------------------------------------------------- the model

class Model:
    def __init__(self) -> None:
        self.tables: Dict[str, Dict[str, Any]] = {}
        self.indexes: Dict[str, Dict[str, Any]] = {}
        self.unreadable: List[Dict[str, str]] = []
        self.data_statements: List[Dict[str, str]] = []
        self.dropped: List[Dict[str, Any]] = []

    def column(self, table: str, definition: str, where: str) -> Optional[Dict[str, Any]]:
        match = re.match(r"(%s)\s*(.*)$" % NAME, definition, re.S)
        if not match:
            return None
        rest = match.group(2)
        kind = re.match(r"([A-Za-z][\w ]*?(?:\([^)]*\))?)(?=\s+(?:NOT|NULL|PRIMARY|UNIQUE|REFERENCES|DEFAULT|CHECK|CONSTRAINT|GENERATED|COLLATE)\b|\s*$)",
                        rest, re.I | re.S)
        upper = rest.upper()
        ref = re.search(r"\bREFERENCES\s+(%s)\s*(?:\(\s*(%s)\s*\))?" % (QUALIFIED, NAME), rest, re.I)
        return {"name": bare(match.group(1)), "type": (kind.group(1).strip().upper() if kind else ""),
                "not_null": bool(re.search(r"\bNOT\s+NULL\b", upper)) or bool(re.search(r"\bPRIMARY\s+KEY\b", upper)),
                "primary_key": bool(re.search(r"\bPRIMARY\s+KEY\b", upper)), "unique": bool(re.search(r"\bUNIQUE\b", upper)),
                "default": bool(re.search(r"\bDEFAULT\b", upper)) or bool(re.search(r"\bGENERATED\b|\bIDENTITY\b|\bSERIAL\b|AUTOINCREMENT", upper))
                or bool(re.search(r"\bINTEGER\s+PRIMARY\s+KEY\b", (kind.group(1) if kind else "") + " " + upper, re.I)),
                "references": {"table": bare(ref.group(1)), "column": bare(ref.group(2)) if ref.group(2) else None} if ref else None,
                "location": where}

    def table_constraint(self, table: Dict[str, Any], part: str, where: str) -> bool:
        body = re.sub(r"^CONSTRAINT\s+%s\s+" % NAME, "", part, flags=re.I)
        pk = re.match(r"PRIMARY\s+KEY\s*\((.*)\)", body, re.I | re.S)
        unique = re.match(r"UNIQUE\s*\((.*)\)", body, re.I | re.S)
        fk = re.match(r"FOREIGN\s+KEY\s*\((.*?)\)\s*REFERENCES\s+(%s)\s*(?:\((.*?)\))?" % QUALIFIED, body, re.I | re.S)
        if pk:
            table["primary_key"] = names_in(pk.group(1))
        elif unique:
            table["uniques"].append(names_in(unique.group(1)))
        elif fk:
            cols, parent = names_in(fk.group(1)), bare(fk.group(2))
            targets = names_in(fk.group(3)) if fk.group(3) else [None] * len(cols)
            for col, target in zip(cols, targets):
                if col in table["columns"]:
                    table["columns"][col]["references"] = {"table": parent, "column": target}
        elif not re.match(r"CHECK\b|EXCLUDE\b", body, re.I):
            return False
        return True

    def create_table(self, stmt: str, line_of, where: str) -> bool:
        match = re.match(r"CREATE\s+(?:TEMP(?:ORARY)?\s+|UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(%s)\s*\((.*)\)[^)]*$" % QUALIFIED,
                         stmt, re.I | re.S)
        if not match:
            return False
        name = bare(match.group(1))
        table = {"name": name, "location": where, "columns": {}, "primary_key": [], "uniques": [], "order": []}
        body_offset = match.start(2)
        for offset, part in split_top(match.group(2)):
            at = line_of(body_offset + offset)
            if re.match(r"(?:CONSTRAINT\s+%s\s+)?(?:PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|CHECK|EXCLUDE)\b" % NAME, part, re.I):
                if not self.table_constraint(table, part, at):
                    self.unreadable.append({"location": at, "text": part[:120]})
                continue
            column = self.column(name, part, at)
            if column is None:
                self.unreadable.append({"location": at, "text": part[:120]})
                continue
            table["columns"][column["name"]] = column
            table["order"].append(column["name"])
            if column["primary_key"]:
                table["primary_key"] = [column["name"]]
        self.tables[name] = table
        return True

    def alter_table(self, stmt: str, where: str, file_text: str) -> bool:
        match = re.match(r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?(%s)\s+(.*)$" % QUALIFIED, stmt, re.I | re.S)
        if not match:
            return False
        name, action = bare(match.group(1)), match.group(2).strip()
        table = self.tables.get(name)
        if table is None:
            return False
        add_constraint = re.match(r"ADD\s+((?:CONSTRAINT\s+%s\s+)?(?:PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|CHECK)\b.*)$" % NAME, action, re.I | re.S)
        add = re.match(r"ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(.*)$", action, re.I | re.S)
        drop = re.match(r"DROP\s+(?:COLUMN\s+)?(?:IF\s+EXISTS\s+)?(%s)" % NAME, action, re.I)
        rename_col = re.match(r"RENAME\s+(?:COLUMN\s+)?(%s)\s+TO\s+(%s)" % (NAME, NAME), action, re.I)
        rename = re.match(r"RENAME\s+TO\s+(%s)" % QUALIFIED, action, re.I)
        if add_constraint:
            return self.table_constraint(table, add_constraint.group(1), where)
        if add and not drop:
            column = self.column(name, add.group(1), where)
            if column is None:
                return False
            table["columns"][column["name"]] = column
            table["order"].append(column["name"])
            return True
        if drop and not re.match(r"DROP\s+CONSTRAINT\b", action, re.I):
            col = bare(drop.group(1))
            if col in table["columns"]:
                del table["columns"][col]
                table["order"].remove(col)
            self.dropped.append({"table": name, "column": col, "location": where, "file_text": file_text})
            return True
        if rename_col:
            old, new = bare(rename_col.group(1)), bare(rename_col.group(2))
            if old in table["columns"]:
                column = table["columns"].pop(old)
                column["name"] = new
                table["columns"][new] = column
                table["order"] = [new if c == old else c for c in table["order"]]
            return True
        if rename:
            new = bare(rename.group(1))
            self.tables[new] = dict(self.tables.pop(name), name=new)
            return True
        return False

    def create_index(self, stmt: str, where: str) -> bool:
        match = re.match(r"CREATE\s+(UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(%s)?\s*ON\s+(?:ONLY\s+)?(%s)\s*(?:USING\s+\w+\s*)?\((.*?)\)"
                         % (QUALIFIED, QUALIFIED), stmt, re.I | re.S)
        if not match:
            return False
        name = bare(match.group(2) or "index_at_%s" % where)
        self.indexes[name] = {"table": bare(match.group(3)), "columns": names_in(match.group(4)), "unique": bool(match.group(1)), "location": where}
        return True

    def drop(self, stmt: str, where: str, file_text: str) -> bool:
        table = re.match(r"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(%s)" % QUALIFIED, stmt, re.I)
        index = re.match(r"DROP\s+INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+EXISTS\s+)?(%s)" % QUALIFIED, stmt, re.I)
        if table:
            name = bare(table.group(1))
            self.tables.pop(name, None)
            self.indexes = {k: v for k, v in self.indexes.items() if v["table"] != name}
            self.dropped.append({"table": name, "column": None, "location": where, "file_text": file_text})
            return True
        if index:
            self.indexes.pop(bare(index.group(1)), None)
            return True
        return False


def read(model: Model, path: Path, root: Path) -> None:
    text = path.read_text(encoding="utf-8")
    shown = rel(path, root)
    for line, stmt in statements(text):
        first_word = stmt.split(None, 1)[0].upper() if stmt.split() else ""
        lines = stmt.split("\n")
        starts = [0]
        for piece in lines[:-1]:
            starts.append(starts[-1] + len(piece) + 1)

        def line_of(offset: int, _line: int = line, _starts: List[int] = starts) -> str:
            return "%s:%d" % (shown, _line + sum(1 for s in _starts[1:] if s <= offset))

        where = "%s:%d" % (shown, line)
        done = False
        if first_word == "CREATE":
            done = model.create_table(stmt, line_of, where) or model.create_index(stmt, where)
        elif first_word == "ALTER":
            done = model.alter_table(stmt, where, text)
        elif first_word == "DROP":
            done = model.drop(stmt, where, text)
        elif first_word in ("INSERT", "UPDATE", "DELETE", "BEGIN", "COMMIT", "SET", "PRAGMA"):
            model.data_statements.append({"location": where, "text": stmt[:120]})
            done = True
        if not done:
            model.unreadable.append({"location": where, "text": stmt[:120]})


# --------------------------------------------------------------------------- leads

def covered(table: Dict[str, Any], indexes: Dict[str, Dict[str, Any]], column: str, alone: bool) -> bool:
    """Whether a key, unique constraint or index covers ``column``: on its own (``alone``), or as its leading column."""
    groups = [table["primary_key"]] + table["uniques"] + [i["columns"] for i in indexes.values() if i["table"] == table["name"] and (i["unique"] or not alone)]
    col = table["columns"].get(column) or {}
    if col.get("unique") or col.get("primary_key"):
        return True
    return any(g and (g == [column] if alone else g[0] == column) for g in groups)


def leads(model: Model, steps: List[Path], has_downs: bool, code: List[Path], root: Path) -> List[Dict[str, Any]]:
    found = []

    def lead(rule: str, location: str, detail: str, **extra: Any) -> None:
        found.append(dict({"rule": rule, "location": location, "detail": detail, "approximate": True}, **extra))

    for name, table in sorted(model.tables.items()):
        for col_name in table["order"]:
            col = table["columns"][col_name]
            if col_name.endswith("_id") and not col["references"] and table["primary_key"] != [col_name]:
                lead("missing-foreign-key", col["location"], "%s.%s references nothing: rows can point at a missing parent" % (name, col_name),
                     table=name, column=col_name)
            if IDENTITY_NAMES.match(col_name) and not covered(table, model.indexes, col_name, alone=True):
                lead("missing-uniqueness", col["location"], "%s.%s is not unique: two rows can hold the same value" % (name, col_name),
                     table=name, column=col_name)
            if col["references"] and not covered(table, model.indexes, col_name, alone=False):
                lead("unindexed-foreign-key", col["location"], "%s.%s references %s with no index leading on it: a lookup or a join scans %s"
                     % (name, col_name, col["references"]["table"], name), table=name, column=col_name)
    for dropped in model.dropped:
        target = dropped["column"] or dropped["table"]
        copies = re.search(r"\b(?:UPDATE|INSERT\s+INTO)\b[^;]*\b%s\b" % re.escape(target), blank_comments(dropped["file_text"]), re.I | re.S)
        if not copies:
            what = "column %s.%s" % (dropped["table"], dropped["column"]) if dropped["column"] else "table %s" % dropped["table"]
            lead("drop-without-copy", dropped["location"], "the step drops %s and no statement in the file reads it first: its data is lost"
                 % what, table=dropped["table"], column=dropped["column"])
    if has_downs:
        for path in steps:
            down = path.with_name(path.name[:-len(".up.sql")] + ".down.sql")
            if not down.exists():
                lead("missing-down-step", "%s:1" % rel(path, root), "%s has no down step: a rollback cannot undo it" % path.name, file=rel(path, root))
    for path in code:
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(%s)\s*\(([^)]*)\)" % NAME, text, re.I):
            table = model.tables.get(bare(match.group(1)))
            if table is None:
                continue
            given = set(names_in(match.group(2)))
            missing = [c for c in table["order"] if table["columns"][c]["not_null"] and not table["columns"][c]["default"] and c not in given]
            if missing:
                line = text.count("\n", 0, match.start()) + 1
                lead("not-null-without-value", "%s:%d" % (rel(path, root), line), "the insert into %s leaves out NOT NULL %s, which has no default"
                     % (table["name"], ", ".join(missing)), table=table["name"])
    return found


def code_files(paths: List[str], root: Path) -> List[Path]:
    found = []
    for text in paths:
        path = Path(os.path.abspath(os.path.join(str(root), text)))
        if path.is_symlink():
            continue
        if path.is_file() and path.suffix == ".py":
            found.append(path)
        elif path.is_dir():
            found += sorted(p for p in path.rglob("*.py") if not p.is_symlink() and "__pycache__" not in p.parts and ".qa" not in p.parts)
    return found


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--migrations", required=True)
    parser.add_argument("--code", action="append", default=[])
    parser.add_argument("--out", default=".qa/work/database/schema-model.json")
    args = parser.parse_args(argv)
    root = Path(os.getcwd()).resolve()
    try:
        out = work_path(args.out, root)
        directory = Path(os.path.abspath(os.path.join(str(root), args.migrations)))
        if directory.is_symlink() or not directory.is_dir():
            raise Refused("no migrations directory at %s" % args.migrations)
    except Refused as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    ups = sorted(p for p in directory.glob("*.up.sql") if p.is_file() and not p.is_symlink())
    has_downs = bool(ups)
    steps = ups or sorted(p for p in directory.glob("*.sql") if p.is_file() and not p.is_symlink())
    model = Model()
    for path in steps:
        read(model, path, root)
    found = leads(model, steps if has_downs else [], has_downs, code_files(args.code, root), root)
    document = {"migrations": rel(directory, root), "files": [rel(p, root) for p in steps],
                "tables": {n: {k: v for k, v in t.items() if k != "order"} for n, t in sorted(model.tables.items())},
                "indexes": model.indexes, "data_statements": model.data_statements, "leads": found, "unreadable": model.unreadable}
    write_new(out, (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    print(json.dumps({"ok": True, "model": rel(out, root), "files": len(steps), "tables": len(model.tables), "leads": found,
                      "unreadable": model.unreadable}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
