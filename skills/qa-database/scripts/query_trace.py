"""Counts the SQL statements each call of a driver issues, through ``sqlite3.set_trace_callback``, to show N+1 query patterns.

Usage: python3 query_trace.py --driver .qa/work/database/trace_driver.py --junit OUT [--out .qa/work/database/query-trace.json]
       [--repeat 3]

Run from the project root, through ``qa run`` (the driver runs project code). The driver is a Python file you write under
``.qa/work/database/``; each function named ``case_<name>(db, measure)`` is one case. ``db`` is a fresh SQLite path under
``.qa/work/database/`` for that case; build the schema and add the rows the case needs, then make the call you want to measure
inside ``with measure():``. Only the statements issued inside ``measure()`` are counted (a case that takes only ``db`` is
measured whole).

While the driver runs, every ``sqlite3.connect`` gets a trace callback, and a connection to a file outside ``.qa/work/`` is
refused (``:memory:`` is allowed); ``ATTACH`` and ``VACUUM INTO`` are refused on traced connections. Each statement is
normalised (literals become ``?``) and attributed to the innermost project file and line that issued it (``path:line``).
A normalised statement issued ``--repeat`` times or more within one case is reported as ``repeated``: the shape of an N+1
query, a lead to confirm against the code, not a measurement of time.

Outputs: the JSON trace under ``.qa/work/`` (``--out``), and a JUnit report (``--junit``, under ``.qa/`` or a new file outside the project;
in ``qa run`` use ``/qa-evidence/<name>.xml`` with ``--report junit:{evidence}/<name>.xml``) with one test per case that fails
when a statement repeats or the case raises. Stdout is one JSON object with the counts. Exit 0, 1 when a case fails, 2 on a
refusal.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import inspect
import json
import os
import re
import secrets
import sqlite3
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional
from xml.sax.saxutils import escape, quoteattr

DENIED = (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH)
SKIPPED = re.compile(r"^\s*(?:PRAGMA|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE)\b", re.I)


class Refused(Exception):
    pass


def _no_link_between(root: Path, path: Path) -> None:
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise Refused("%s is a symlink" % current.relative_to(root).as_posix())


def work_path(text: str, root: Path) -> Path:
    path = Path(os.path.abspath(os.path.join(str(root), text)))
    work = root / ".qa" / "work"
    if path == work or work not in path.parents:
        raise Refused("%s is not under .qa/work/" % text)
    _no_link_between(root, path)
    return path


def report_path(text: str, root: Path) -> Path:
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


def fresh(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.is_symlink() or candidate.exists():
            if candidate.is_dir() and not candidate.is_symlink():
                raise Refused("%s is a directory" % candidate.name)
            candidate.unlink()


def normalise(sql: str) -> str:
    text = re.sub(r"'(?:[^']|'')*'", "?", sql)
    text = re.sub(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])", "?", text)
    text = re.sub(r"\(\s*\?(?:\s*,\s*\?)+\s*\)", "(?…)", text)
    return re.sub(r"\s+", " ", text).strip().rstrip(";")


class Tracer:
    def __init__(self, root: Path, skip: List[Path]) -> None:
        self.root = root
        self.skip = [str(p) for p in skip]
        self.work = str(root / ".qa" / "work") + os.sep
        self.case: Optional[str] = None
        self.recording = True
        self.calls: Dict[str, List[Dict[str, str]]] = {}
        self.original = sqlite3.connect

    def site(self) -> str:
        frame = sys._getframe(2)
        fallback = ""
        while frame is not None:
            name = os.path.abspath(frame.f_code.co_filename)
            if name.startswith(str(self.root) + os.sep) and name not in self.skip:
                shown = "%s:%d" % (Path(name).relative_to(self.root).as_posix(), frame.f_lineno)
                if not name.startswith(self.work):
                    return shown
                fallback = fallback or shown
            frame = frame.f_back
        return fallback

    def callback(self, sql: str) -> None:
        if self.case is None or not self.recording or SKIPPED.match(sql):
            return
        self.calls.setdefault(self.case, []).append({"sql": normalise(sql), "site": self.site()})

    def allowed(self, database: Any, uri: bool) -> bool:
        text = os.fspath(database) if isinstance(database, (str, bytes, os.PathLike)) else ""
        text = text.decode() if isinstance(text, bytes) else text
        if text == ":memory:" or (uri and "mode=memory" in text):
            return True
        if uri or text.startswith("file:"):
            return False
        path = os.path.abspath(os.path.join(str(self.root), text))
        if not path.startswith(self.work):
            return False
        try:
            _no_link_between(self.root, Path(path))
        except Refused:
            return False
        return True

    @contextlib.contextmanager
    def measure(self) -> Iterator[None]:
        self.recording = True
        try:
            yield
        finally:
            self.recording = False

    def connect(self, database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if not self.allowed(database, bool(kwargs.get("uri"))):
            raise sqlite3.OperationalError("query_trace refuses a database outside .qa/work/: %r" % (database,))
        # sqlite3 cannot open a file without following links; ``allowed`` checked the path for links just before this open.
        connection = self.original(database, *args, **kwargs)
        connection.set_authorizer(lambda action, *_a: sqlite3.SQLITE_DENY if action in DENIED else sqlite3.SQLITE_OK)
        connection.set_trace_callback(self.callback)
        return connection


def load(driver: Path, root: Path) -> Any:
    # importlib opens the driver by path and follows links; work_path checked it for links just before, so the only gap is
    # the moment between that check and this import.
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location("qa_trace_driver_%s" % secrets.token_hex(4), str(driver))
    if spec is None or spec.loader is None:
        raise Refused("cannot load %s" % driver.name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cases_of(module: Any) -> List[Callable[..., Any]]:
    found = [value for name, value in vars(module).items() if name.startswith("case_") and inspect.isfunction(value)]
    return sorted(found, key=lambda f: f.__code__.co_firstlineno)


def summarise(calls: List[Dict[str, str]], repeat: int) -> List[Dict[str, Any]]:
    counts: Dict[str, Dict[str, Any]] = {}
    for call in calls:
        entry = counts.setdefault(call["sql"], {"sql": call["sql"], "count": 0, "sites": []})
        entry["count"] += 1
        if call["site"] and call["site"] not in entry["sites"]:
            entry["sites"].append(call["site"])
    return [e for e in counts.values() if e["count"] >= repeat]


def junit(results: List[Dict[str, Any]]) -> bytes:
    failed = sum(1 for r in results if r["failure"])
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<testsuite name="query_trace" tests="%d" failures="%d" errors="0">' % (len(results), failed)]
    for result in results:
        lines.append('  <testcase classname="query_trace" name=%s>' % quoteattr(result["case"]))
        if result["failure"]:
            lines.append('    <failure message=%s>%s</failure>' % (quoteattr(result["failure"].splitlines()[0][:200]), escape(result["failure"])))
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    return ("\n".join(lines) + "\n").encode("utf-8")


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--driver", required=True)
    parser.add_argument("--junit", required=True)
    parser.add_argument("--out", default=".qa/work/database/query-trace.json")
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args(argv)
    root = Path(os.getcwd()).resolve()
    try:
        if args.repeat < 2:
            raise Refused("--repeat must be at least 2")
        driver = work_path(args.driver, root)
        out = work_path(args.out, root)
        report = report_path(args.junit, root)
        if not driver.is_file() or driver.suffix != ".py":
            raise Refused("no driver file at %s" % args.driver)
    except Refused as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    tracer = Tracer(root, [Path(__file__).resolve()])
    sqlite3.connect = tracer.connect  # type: ignore[assignment]
    results = []
    try:
        module = load(driver, root)
        for case in cases_of(module):
            name = case.__name__[len("case_"):]
            database = driver.parent / ("trace-%s.sqlite" % name)
            fresh(database)
            tracer.case = name
            error = ""
            takes_measure = len(inspect.signature(case).parameters) >= 2
            tracer.recording = not takes_measure
            try:
                case(database, tracer.measure) if takes_measure else case(database)
            except Exception:  # the case's own failure is reported, not raised
                error = traceback.format_exc(limit=6)
            tracer.case = None
            calls = tracer.calls.get(name, [])
            repeated = summarise(calls, args.repeat)
            failure = error or "\n".join("repeated: %d x %s at %s" % (r["count"], r["sql"], ", ".join(r["sites"]) or "?") for r in repeated)
            results.append({"case": name, "db": database.relative_to(root).as_posix(), "statements": len(calls),
                            "repeated": repeated, "error": error, "failure": failure, "calls": calls})
    except Refused as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    finally:
        sqlite3.connect = tracer.original  # type: ignore[assignment]
    write_new(out, (json.dumps({"driver": driver.relative_to(root).as_posix(), "repeat": args.repeat, "cases": results},
                               indent=2, sort_keys=True) + "\n").encode("utf-8"))
    write_report(report, junit(results), root)
    failed = [r["case"] for r in results if r["failure"]]
    print(json.dumps({"ok": not failed and bool(results), "out": out.relative_to(root).as_posix(), "junit": str(report),
                      "cases": [{"case": r["case"], "statements": r["statements"], "repeated": [{k: e[k] for k in ("count", "sql", "sites")}
                                for e in r["repeated"]], "error": r["error"].strip().splitlines()[-1] if r["error"] else ""} for r in results],
                      "failed": failed}, sort_keys=True))
    return 0 if results and not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
