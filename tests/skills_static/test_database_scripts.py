"""The bundled scripts and the race template of qa-database.

``schema_model.py``, ``migration_roundtrip.py``, ``query_trace.py`` and ``squawk_to_sarif.py`` are checked on a copy of fixture
F3 (``fixtures/sql-database``) and on small files written at test time, each run as the skill runs it: a subprocess from the
project root. ``assets/race_template.py`` is filled for F3 the way the skill tells an agent to fill it, and run with pytest.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET

import pytest

from helpers import REPO
from qa.ingest import sarif as kernel_sarif

SKILL = REPO / "skills" / "qa-database"
SCRIPTS = SKILL / "scripts"
F3 = REPO / "fixtures" / "sql-database"
OWN = ["schema_model.py", "migration_roundtrip.py", "query_trace.py", "squawk_to_sarif.py"]
STDLIB = {"__future__", "argparse", "contextlib", "importlib", "inspect", "json", "os", "pathlib", "re", "secrets", "sqlite3", "sys",
          "threading", "traceback", "typing", "xml"}                    # the standard-library modules these files use, on Python 3.9


def run(script, *args, cwd):
    done = subprocess.run([sys.executable, "-I", "-B", str(SCRIPTS / script)] + [str(a) for a in args], capture_output=True, text=True,
                          timeout=120, cwd=str(cwd), env={"PATH": os.environ["PATH"], "HOME": str(cwd)})
    return done.returncode, (json.loads(done.stdout) if done.stdout.strip() else None), done.stderr


def snapshot(root):
    found = {}
    for here, dirs, names in os.walk(str(root)):
        dirs[:] = [d for d in dirs if d not in (".qa", "__pycache__")]
        for name in names:
            path = os.path.join(here, name)
            with open(path, "rb") as handle:
                found[os.path.relpath(path, str(root))] = handle.read()
    return found


@pytest.fixture
def f3(tmp_path):
    copy = tmp_path / "f3"
    shutil.copytree(str(F3), str(copy), ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".qa"))
    return copy


def write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")


@pytest.mark.parametrize("name", OWN + ["../assets/race_template.py"])
def test_every_file_starts_with_the_future_import_and_uses_the_standard_library_only(name):
    path = (SCRIPTS / name).resolve()
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    first = [node for node in tree.body if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))][0]
    assert isinstance(first, ast.ImportFrom) and first.module == "__future__" and [a.name for a in first.names] == ["annotations"]
    modules = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    modules |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module and node.level == 0}
    allowed = STDLIB | ({"pytest"} if "race_template" in name else set())
    assert modules <= allowed, sorted(modules - allowed)


# ----------------------------------------------------------------------------------------------- schema_model.py

def leads(out):
    return sorted((lead["rule"], lead["location"]) for lead in out["leads"])


def test_schema_model_of_f3_lists_each_lead_with_its_line_and_writes_only_the_model(f3):
    before = snapshot(f3)
    code, out, err = run("schema_model.py", "--migrations", "migrations", "--code", "shop_db", cwd=f3)
    assert code == 0 and out["ok"] and out["unreadable"] == [] and out["tables"] == 5, (out, err)
    assert leads(out) == [
        ("drop-without-copy", "migrations/0004_split_customer_name.up.sql:3"),
        ("missing-down-step", "migrations/0005_create_audit_log.up.sql:1"),
        ("missing-foreign-key", "migrations/0003_create_orders.up.sql:3"),
        ("missing-uniqueness", "migrations/0001_create_customers.up.sql:3"),
        ("unindexed-foreign-key", "migrations/0003_create_orders.up.sql:13"),
        ("unindexed-foreign-key", "migrations/0005_create_audit_log.up.sql:3")]
    assert all(lead["approximate"] is True for lead in out["leads"])
    model = json.loads((f3 / ".qa" / "work" / "database" / "schema-model.json").read_text(encoding="utf-8"))
    assert model["tables"]["order_items"]["primary_key"] == ["order_id", "line_no"]
    assert model["tables"]["customers"]["columns"]["email"]["not_null"] is True
    assert snapshot(f3) == before


def test_schema_model_of_the_postgres_variant_shows_the_lookup_column_without_an_index(f3):
    code, out, _ = run("schema_model.py", "--migrations", "postgres/migrations", "--out", ".qa/work/database/pg.json", cwd=f3)
    assert code == 0 and out["unreadable"] == []
    assert ("unindexed-foreign-key", "postgres/migrations/0003_create_orders.up.sql:12") in leads(out)
    assert ("missing-foreign-key", "postgres/migrations/0003_create_orders.up.sql:3") in leads(out)


def test_schema_model_reads_table_constraints_indexes_and_alters_and_lists_what_it_cannot_read(tmp_path):
    write(tmp_path, {
        "m/0001_a.up.sql": """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL,
                username TEXT NOT NULL,
                team_id INTEGER,
                CONSTRAINT fk_team FOREIGN KEY (team_id) REFERENCES teams (id)
            );
            CREATE UNIQUE INDEX users_email ON users (email);
            -- a comment; with a semicolon
            CREATE TRIGGER t AFTER INSERT ON users BEGIN SELECT 1; END;
            """,
        "m/0001_a.down.sql": "DROP TABLE users;\n",
        "m/0002_b.up.sql": """
            ALTER TABLE users ADD COLUMN owner_id INTEGER REFERENCES users (id);
            ALTER TABLE users ADD CONSTRAINT u_username UNIQUE (username);
            CREATE INDEX users_owner ON users (owner_id);
            """,
        "m/0002_b.down.sql": "ALTER TABLE users DROP COLUMN owner_id;\n",
        "app/repo.py": 'def add(db):\n    db.execute("INSERT INTO users (email) VALUES (?)", ("a",))\n',
    })
    code, out, _ = run("schema_model.py", "--migrations", "m", "--code", "app", cwd=tmp_path)
    assert code == 0
    found = leads(out)
    assert not [lead for lead in found if lead[0] in ("missing-foreign-key", "missing-uniqueness", "missing-down-step")], found
    assert ("unindexed-foreign-key", "m/0001_a.up.sql:5") in found              # team_id: a foreign key that no index leads with
    assert ("not-null-without-value", "app/repo.py:2") in found                   # username is NOT NULL with no default
    assert [u["location"] for u in out["unreadable"]][:1] == ["m/0001_a.up.sql:10"]   # the trigger: listed, never guessed


def test_schema_model_sees_a_copy_before_a_drop_and_a_drop_without_one(tmp_path):
    write(tmp_path, {
        "m/0001_a.sql": "CREATE TABLE p (id INTEGER PRIMARY KEY, full_name TEXT, first TEXT);\n",
        "m/0002_b.sql": "UPDATE p SET first = full_name;\nALTER TABLE p DROP COLUMN full_name;\n",
        "m/0003_c.sql": "-- the table goes\nDROP TABLE p;\n",
    })
    code, out, _ = run("schema_model.py", "--migrations", "m", cwd=tmp_path)
    assert code == 0
    assert [lead for lead in leads(out) if lead[0] == "drop-without-copy"] == [("drop-without-copy", "m/0003_c.sql:2")]
    assert not [lead for lead in leads(out) if lead[0] == "missing-down-step"]       # no .up.sql/.down.sql convention there


def test_schema_model_refuses_an_output_outside_qa_work_and_a_symlink_on_the_way(f3, tmp_path):
    for out_path in ("schema.json", ".qa/schema.json", "../schema.json"):
        code, out, _ = run("schema_model.py", "--migrations", "migrations", "--out", out_path, cwd=f3)
        assert code == 2 and not out["ok"], out_path
    (f3 / ".qa").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(str(elsewhere), str(f3 / ".qa" / "work"))
    code, out, _ = run("schema_model.py", "--migrations", "migrations", cwd=f3)
    assert code == 2 and "symlink" in out["error"] and list(elsewhere.iterdir()) == []


# ----------------------------------------------------------------------------------------------- migration_roundtrip.py

def test_roundtrip_of_f3_reports_the_loss_the_missing_down_step_and_what_follows(f3):
    before = snapshot(f3)
    code, out, err = run("migration_roundtrip.py", "--migrations", "migrations", "--db", ".qa/work/database/roundtrip.sqlite",
                         "--junit", ".qa/work/database/roundtrip.xml", cwd=f3)
    assert code == 1 and (out["tests"], out["failed"], out["steps"], out["unreadable"]) == (17, 6, 5, []), (out, err)
    assert [(f["phase"], f["step"]) for f in out["failures"]] == [
        ("loss", "up 0004_split_customer_name"), ("down", "0005_create_audit_log"), ("down", "0003_create_orders"),
        ("down", "leaves no table"), ("reup", "0003_create_orders"), ("reup", "0005_create_audit_log")]
    failures = {(f["phase"], f["step"]): f["failure"] for f in out["failures"]}
    assert "customers.name dropped" in failures["loss", "up 0004_split_customer_name"]
    assert "has no down step" in failures["down", "0005_create_audit_log"]
    assert "FOREIGN KEY constraint failed" in failures["down", "0003_create_orders"]
    assert failures["down", "leaves no table"].endswith("audit_log, orders")
    assert "already exists" in failures["reup", "0003_create_orders"]
    suite = ET.parse(str(f3 / ".qa" / "work" / "database" / "roundtrip.xml")).getroot()
    cases = suite.findall(".//testcase")
    assert len(cases) == 17 and sum(1 for c in cases if c.find("failure") is not None) == 6
    assert {c.get("classname") for c in cases} == {"roundtrip.up", "roundtrip.loss", "roundtrip.down", "roundtrip.reup"}
    database = f3 / ".qa" / "work" / "database" / "roundtrip.sqlite"
    assert database.read_bytes()[:16] == b"SQLite format 3\x00"
    assert snapshot(f3) == before


def test_roundtrip_of_clean_migrations_passes_and_replaces_an_old_database(tmp_path):
    write(tmp_path, {
        "m/0001_a.up.sql": "CREATE TABLE a (id INTEGER PRIMARY KEY, label TEXT NOT NULL);\n",
        "m/0001_a.down.sql": "DROP TABLE a;\n",
        "m/0002_b.up.sql": "CREATE TABLE b (id INTEGER PRIMARY KEY, a_id INTEGER NOT NULL REFERENCES a (id));\n",
        "m/0002_b.down.sql": "DROP TABLE b;\n",
        "m/README.md": "notes\n",
    })
    (tmp_path / ".qa" / "work" / "database").mkdir(parents=True)
    (tmp_path / ".qa" / "work" / "database" / "rt.sqlite").write_bytes(b"old")
    code, out, _ = run("migration_roundtrip.py", "--migrations", "m", "--db", ".qa/work/database/rt.sqlite", "--junit", ".qa/rt.xml",
                       cwd=tmp_path)
    assert code == 0 and out["ok"] and out["failed"] == 0 and out["unreadable"] == ["README.md"], out
    assert (tmp_path / ".qa" / "work" / "database" / "rt.sqlite").read_bytes()[:6] == b"SQLite"


def test_roundtrip_refuses_a_database_outside_qa_work_and_a_report_inside_the_project(f3, tmp_path):
    for db in ("shop.sqlite", ".qa/shop.sqlite", str(tmp_path / "shop.sqlite"), ".qa/work"):
        code, out, _ = run("migration_roundtrip.py", "--migrations", "migrations", "--db", db, "--junit", ".qa/r.xml", cwd=f3)
        assert code == 2 and not out["ok"], db
    code, out, _ = run("migration_roundtrip.py", "--migrations", "migrations", "--db", ".qa/work/database/x.sqlite", "--junit", "report.xml", cwd=f3)
    assert code == 2 and "report" in out["error"]
    assert not (tmp_path / "shop.sqlite").exists() and not (f3 / "shop.sqlite").exists() and not (f3 / "report.xml").exists()


def test_roundtrip_never_follows_a_link_under_qa_work(f3, tmp_path):
    outside = tmp_path / "outside.sqlite"
    outside.write_bytes(b"keep")
    work = f3 / ".qa" / "work" / "database"
    work.mkdir(parents=True)
    os.symlink(str(outside), str(work / "rt.sqlite"))
    code, out, _ = run("migration_roundtrip.py", "--migrations", "migrations", "--db", ".qa/work/database/rt.sqlite", "--junit", ".qa/r.xml", cwd=f3)
    assert code == 2 and outside.read_bytes() == b"keep"
    os.unlink(str(work / "rt.sqlite"))
    os.symlink(str(outside), str(work / "rt.sqlite-journal"))
    code, out, _ = run("migration_roundtrip.py", "--migrations", "migrations", "--db", ".qa/work/database/rt.sqlite", "--junit", ".qa/r.xml", cwd=f3)
    assert outside.read_bytes() == b"keep"


def test_roundtrip_refuses_attach_and_vacuum_into(tmp_path):
    target = tmp_path / "other.sqlite"
    write(tmp_path, {
        "m/0001_a.up.sql": "ATTACH DATABASE '%s' AS other;\nCREATE TABLE other.t (x);\n" % target,
        "m/0001_a.down.sql": "SELECT 1;\n",
        "m/0002_b.up.sql": "CREATE TABLE b (x);\nVACUUM INTO '%s';\n" % target,
        "m/0002_b.down.sql": "DROP TABLE b;\n",
    })
    code, out, _ = run("migration_roundtrip.py", "--migrations", "m", "--db", ".qa/work/database/rt.sqlite", "--junit", ".qa/r.xml", cwd=tmp_path)
    assert code == 1 and not target.exists()
    assert {f["step"] for f in out["failures"] if f["phase"] == "up"} == {"0001_a", "0002_b"}


def test_roundtrip_copies_itself_under_qa_work_for_a_container_input(f3):
    code, out, _ = run("migration_roundtrip.py", "--copy-to-work", cwd=f3)
    copied = f3 / ".qa" / "work" / "database" / "migration_roundtrip.py"
    assert code == 0 and out == {"ok": True, "copied": ".qa/work/database/migration_roundtrip.py"}
    assert copied.read_bytes() == (SCRIPTS / "migration_roundtrip.py").read_bytes()


# ----------------------------------------------------------------------------------------------- query_trace.py

DRIVER = """
from pathlib import Path
import sqlite3
from shop_db.store import Store


def case_orders_for_customer(db, measure):
    store = Store(db)
    store.migrate(Path("migrations"))
    customer = store.add_customer("t@example.test", "T", "U")
    product = store.add_product("s-1", "P", 100, 10)
    for _ in range(5):
        store.place_order(customer, [(product, 1)])
    with measure():
        store.orders_for_customer(customer)
    store.close()


def case_one_order(db, measure):
    store = Store(db)
    store.migrate(Path("migrations"))
    with measure():
        store.add_customer("t@example.test", "T", "U")
    store.close()


def case_in_memory(db):
    sqlite3.connect(":memory:").execute("SELECT 1").fetchall()


def case_outside(db):
    Store("shop.sqlite")
"""


def test_query_trace_of_f3_shows_one_items_query_per_order_at_its_line(f3):
    write(f3, {".qa/work/database/trace_driver.py": DRIVER})
    before = snapshot(f3)
    code, out, err = run("query_trace.py", "--driver", ".qa/work/database/trace_driver.py", "--junit", ".qa/work/database/trace.xml", cwd=f3)
    assert code == 1 and out["failed"] == ["orders_for_customer", "outside"], (out, err)
    cases = {case["case"]: case for case in out["cases"]}
    assert cases["orders_for_customer"]["statements"] == 6
    assert [(r["count"], r["sites"]) for r in cases["orders_for_customer"]["repeated"]] == [(5, ["shop_db/store.py:76"])]
    assert cases["one_order"]["repeated"] == [] and cases["one_order"]["error"] == ""
    assert cases["in_memory"]["error"] == ""
    assert "refuses a database outside .qa/work/" in cases["outside"]["error"]
    assert not (f3 / "shop.sqlite").exists() and snapshot(f3) == before
    suite = ET.parse(str(f3 / ".qa" / "work" / "database" / "trace.xml")).getroot()
    assert (suite.get("tests"), suite.get("failures")) == ("4", "2")
    trace = json.loads((f3 / ".qa" / "work" / "database" / "query-trace.json").read_text(encoding="utf-8"))
    assert trace["repeat"] == 3 and len(trace["cases"]) == 4


def test_query_trace_refuses_a_driver_outside_qa_work_and_attach_inside_a_case(f3, tmp_path):
    write(f3, {"driver.py": DRIVER})
    code, out, _ = run("query_trace.py", "--driver", "driver.py", "--junit", ".qa/t.xml", cwd=f3)
    assert code == 2 and not out["ok"]
    target = tmp_path / "other.sqlite"
    write(f3, {".qa/work/database/attach.py": """
        import sqlite3


        def case_attach(db):
            sqlite3.connect(str(db)).execute("ATTACH DATABASE '%s' AS o")
        """ % target})
    code, out, _ = run("query_trace.py", "--driver", ".qa/work/database/attach.py", "--junit", ".qa/t.xml", cwd=f3)
    assert code == 1 and "not authorized" in out["cases"][0]["error"] and not target.exists()


# ----------------------------------------------------------------------------------------------- squawk_to_sarif.py

SQUAWK = [{"file": "/work/postgres/migrations/0004_split_customer_name.up.sql", "line": 2, "column": 0, "line_end": 2, "column_end": 33,
           "level": "Warning", "message": "Dropping a column may break existing clients.", "note": None,
           "help": "Update your application code to no longer read or write the column.", "rule_name": "ban-drop-column"},
          {"file": "/work/postgres/migrations/0003_create_orders.up.sql", "line": 0, "column": 4, "line_end": 5, "column_end": 1,
           "level": "Error", "message": "Prefer bigint.", "note": None, "help": None, "rule_name": "prefer-bigint-over-int"}]


def test_squawk_to_sarif_makes_lines_one_based_strips_the_mount_and_the_kernel_reads_it(f3):
    write(f3, {".qa/evidence/EXE-X/stdout.log": json.dumps(SQUAWK)})
    code, out, err = run("squawk_to_sarif.py", "--input", ".qa/evidence/EXE-X/stdout.log", "--out", ".qa/work/database/squawk.sarif", cwd=f3)
    assert code == 0 and out == {"ok": True, "out": ".qa/work/database/squawk.sarif", "results": 2,
                                 "rules": ["ban-drop-column", "prefer-bigint-over-int"]}, err
    data = (f3 / ".qa" / "work" / "database" / "squawk.sarif").read_bytes()
    doc = json.loads(data)
    first = doc["runs"][0]["results"][0]
    assert first["level"] == "warning" and doc["runs"][0]["results"][1]["level"] == "error"
    location = first["locations"][0]["physicalLocation"]
    assert location["artifactLocation"]["uri"] == "postgres/migrations/0004_split_customer_name.up.sql"
    assert location["region"] == {"startLine": 3, "startColumn": 1, "endLine": 3, "endColumn": 34}
    summary = kernel_sarif.parse(data)
    assert [(a.tool, a.rule_id, a.path, a.start_line) for a in summary.alerts] == [
        ("squawk", "ban-drop-column", "postgres/migrations/0004_split_customer_name.up.sql", 3),
        ("squawk", "prefer-bigint-over-int", "postgres/migrations/0003_create_orders.up.sql", 1)]


def test_squawk_to_sarif_of_a_clean_run_has_no_results(f3):
    write(f3, {".qa/evidence/EXE-X/stdout.log": "[]\n"})
    code, out, _ = run("squawk_to_sarif.py", "--input", ".qa/evidence/EXE-X/stdout.log", "--out", ".qa/work/database/s.sarif", cwd=f3)
    assert code == 0 and out["results"] == 0
    assert list(kernel_sarif.parse((f3 / ".qa" / "work" / "database" / "s.sarif").read_bytes()).alerts) == []


def test_squawk_to_sarif_refuses_other_input_an_output_outside_qa_work_and_links(f3, tmp_path):
    write(f3, {".qa/evidence/EXE-X/stdout.log": json.dumps(SQUAWK), ".qa/evidence/EXE-Y/stdout.log": "squawk: 1 violation\n"})
    code, out, _ = run("squawk_to_sarif.py", "--input", ".qa/evidence/EXE-X/stdout.log", "--out", "squawk.sarif", cwd=f3)
    assert code == 2 and not (f3 / "squawk.sarif").exists()
    code, out, _ = run("squawk_to_sarif.py", "--input", ".qa/evidence/EXE-Y/stdout.log", "--out", ".qa/work/database/s.sarif", cwd=f3)
    assert code == 2 and "not JSON" in out["error"]
    secret = tmp_path / "secret.json"
    secret.write_text("[]")
    os.symlink(str(secret), str(f3 / ".qa" / "evidence" / "link.log"))
    code, out, _ = run("squawk_to_sarif.py", "--input", ".qa/evidence/link.log", "--out", ".qa/work/database/s.sarif", cwd=f3)
    assert code == 2 and "symlink" in out["error"]
    code, out, _ = run("squawk_to_sarif.py", "--input", str(secret), "--out", ".qa/work/database/s.sarif", cwd=f3)
    assert code == 2


# ----------------------------------------------------------------------------------------------- the race template

TEMPLATE = SKILL / "assets" / "race_template.py"
FILLS = {
    "open_store": "    from shop_db.store import Store\n    return Store(DB)\n",
    "prepare": ("    store.migrate(MIGRATIONS)\n    return {'product': store.add_product('race-1', 'Last unit', 1, 100),\n"
                "            'customers': [store.add_customer('a@example.test', 'A', 'A'), store.add_customer('b@example.test', 'B', 'B')]}\n"),
    "operation": "    store.place_order(ids['customers'][caller], [(ids['product'], 1)])\n",
    "invariant": ("    stock = connection.execute('SELECT stock FROM products WHERE id = ?', (ids['product'],)).fetchone()[0]\n"
                  "    return successes <= 1 and stock >= 0, 'stock now %d' % stock\n"),
}
CONDITIONAL = '''
    def decrement_stock(self, product_id: int, quantity: int) -> None:
        changed = self.connection.execute("UPDATE products SET stock = stock - ? WHERE id = ? AND stock >= ?",
                                          (quantity, product_id, quantity)).rowcount
        self.connection.commit()
        if changed == 0:
            raise OutOfStock(product_id)
'''


def filled():
    text = TEMPLATE.read_text(encoding="utf-8").replace('READ_METHOD = "read_the_value"', 'READ_METHOD = "stock_of"')
    for name, code in FILLS.items():
        pattern = re.compile(r'(def %s\([^)]*\):\n    """.*?"""\n)    raise NotImplementedError\("FILL %s"\)\n' % (name, name), re.S)
        assert pattern.search(text), name
        text = pattern.sub(lambda m, code=code: m.group(1) + code, text)
    assert "NotImplementedError" not in text
    return text


def race(root):
    write(root, {".qa/work/database/test_race_stock.py": filled()})
    done = subprocess.run([sys.executable, "-B", "-m", "pytest", "-p", "no:cacheprovider", "-q", ".qa/work/database/test_race_stock.py",
                           "--junitxml=" + str(root / ".qa" / "race.xml")], capture_output=True, text=True, timeout=120, cwd=str(root))
    return done.returncode, done.stdout + done.stderr


def test_the_template_unfilled_marks_every_part_to_fill_and_fails_loudly(tmp_path):
    text = TEMPLATE.read_text(encoding="utf-8")
    assert re.findall(r'raise NotImplementedError\("FILL (\w+)"\)', text) == ["open_store", "prepare", "operation", "invariant"]
    assert 'DB = Path(__file__).resolve().parent / "race.sqlite"' in text and "lost update" in text
    assert "shop_db" not in text.split('"""', 2)[2].split("def open_store")[0]


def test_the_filled_template_shows_the_lost_update_of_f3_on_a_file_under_qa_work(f3):
    before = snapshot(f3)
    for _ in range(3):                                    # the interleaving is forced, so every run gives the same answer
        code, output = race(f3)
        assert code == 1 and "lost update: 2 of 2 callers succeeded; stock now 0" in output, output
    junit = (f3 / ".qa" / "race.xml").read_text(encoding="utf-8")
    assert "lost update" in junit
    assert (f3 / ".qa" / "work" / "database" / "race.sqlite").read_bytes()[:6] == b"SQLite"
    assert snapshot(f3) == before


def test_the_filled_template_passes_when_the_decrement_is_conditional(f3):
    store = f3 / "shop_db" / "store.py"
    text = store.read_text(encoding="utf-8")
    start, end = text.index("    def decrement_stock"), text.index("    def _write_stock")
    store.write_text(text[:start] + CONDITIONAL.lstrip("\n") + "\n" + text[end:], encoding="utf-8")
    code, output = race(f3)
    assert code == 0, output
    connection = sqlite3.connect(str(f3 / ".qa" / "work" / "database" / "race.sqlite"))
    assert connection.execute("SELECT stock FROM products").fetchall() == [(0,)]
    connection.close()
