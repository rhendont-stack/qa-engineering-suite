"""F3 ``sql-database``: a small shop database as migrations on SQLite with a PostgreSQL variant, and an answer key.

``shop_db.store`` is the access layer on the standard library's ``sqlite3``. ``migrations/`` is the SQLite schema and
``postgres/migrations/`` its PostgreSQL port. The fixture's own files never say what is wrong with them.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import FIXTURES, LAUNCHER, REPO, answer_key
from qa import container
from test_container_live import Live, assert_no_service_left, live  # noqa: F401 (live is a fixture)

F3 = FIXTURES / "sql-database"
MIGRATIONS = F3 / "migrations"
PG_MIGRATIONS = F3 / "postgres" / "migrations"
PYTEST_RUN = ("run", "--report", "junit:{evidence}/junit.xml", "--", sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o",
              "junit_family=xunit2", "--junitxml={evidence}/junit.xml")
# What a smoke test must never contain: the input or the step that would show one of the weaknesses the key lists.
EXPOSING = ("duplicate", "orphan", "999", "oversell", "interleav", "concurrent", "EXPLAIN", "DROP", ".down", "trace", "audit")
TABLES = {"customers", "products", "orders", "order_items", "audit_log"}
SCHEMA_CASES = ["sqlite", "postgres"]
USER_APPROVAL = ("Hacer lo necesario para ejecutar el requerimiento, no me preguntes, ejecute de forma autonoma. / Los apruebo yo")
POSTGRES_IMAGE = "postgres@sha256:e5d2edd28b1f8d40a0a6c0c9cdc545b19b56a0d5d88f94bc11a97d970d4700d0"  # postgres 17.11-alpine, uid 70


# The seeds a test in this file shows in process, by that test; F3-07 shows only in a PostgreSQL plan (the docker test).
DEMONSTRATED_IN_PROCESS = {
    "F3-01": "test_the_schema_allows_the_rows_the_key_describes", "F3-02": "test_the_schema_allows_the_rows_the_key_describes",
    "F3-03": "test_the_migration_that_drops_a_column_moves_nothing", "F3-04": "test_migrations_apply_from_scratch_on_sqlite",
    "F3-05": "test_the_list_of_orders_asks_the_database_once_per_order",
    "F3-06": "test_two_connections_that_read_before_either_writes_lose_an_update",
}


def forget_the_package():
    for name in [name for name in sys.modules if name == "shop_db" or name.startswith("shop_db.")]:
        del sys.modules[name]


@pytest.fixture(autouse=True)
def drop_the_package_after_each_test():
    yield
    forget_the_package()


def load_store(monkeypatch, directory=F3):
    forget_the_package()
    monkeypatch.syspath_prepend(str(directory))
    return importlib.import_module("shop_db.store")


def load_migrate(monkeypatch):
    load_store(monkeypatch)
    return importlib.import_module("shop_db.migrate")


def migrated_store(monkeypatch, path, upto=None):
    module = load_store(monkeypatch)
    store = module.Store(path)
    migrate = importlib.import_module("shop_db.migrate")
    if upto is None:
        migrate.apply(store.connection, MIGRATIONS, "up")
    else:
        for step in migrate.migration_files(MIGRATIONS, "up")[:upto]:
            store.connection.executescript(step.read_text(encoding="utf-8"))
    return module, store


def tables_of(connection):
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}


def columns_of(connection, table):
    return [row[1] for row in connection.execute("PRAGMA table_info(%s)" % table)]


def test_f3_smoke_suite_passes_and_hides_every_seed(qa_on_fixture):
    key = answer_key("sql-database")["suites"]["python"]
    project = qa_on_fixture("sql-database")
    code, ran = project.cli(*PYTEST_RUN)
    assert (code, ran["status"]) == (0, key["run_status"]) and ran["status"] == "passed"
    assert project.results(ran["execution"]) == key["tests"] and len(key["tests"]) == 3
    tests = "".join(path.read_text(encoding="utf-8") for path in (F3 / "tests").glob("test_*.py"))
    assert not [word for word in EXPOSING if word in tests]
    for path in F3.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            assert not re.search(r"(?i)seed|defect|\bbug\b|answer|off-by-one|vulnerab|exploit|payload", path.read_text(encoding="utf-8")), path


def test_f3_answer_key_locations_exist(repo_root):
    key = answer_key("sql-database")
    anchors = json.loads((repo_root / "shared" / "policies" / "severity-rubric.json").read_text(encoding="utf-8"))["anchors"]
    categories = set(json.loads((repo_root / "shared" / "schemas" / "defs.schema.json").read_text(encoding="utf-8"))["$defs"]["category"]["enum"])
    issues = key["issues"]
    assert key["fixture"] == "sql-database" and [issue["id"] for issue in issues] == ["F3-%02d" % n for n in range(1, len(issues) + 1)]
    assert len(issues) == 7
    for issue in issues:
        assert set(issue) == {"id", "location", "file", "lines", "match", "category", "severity_anchor", "class", "detectable_by"}, issue["id"]
        assert issue["location"] == issue["file"], issue["id"]
        path = F3 / issue["file"]
        assert path.is_file(), issue["id"]
        first, last = issue["lines"]
        lines = path.read_text(encoding="utf-8").splitlines()
        assert 1 <= first <= last <= len(lines), issue["id"]
        assert issue["match"] in "\n".join(lines[first - 1:last]), issue["id"]
        assert issue["severity_anchor"] in anchors and issue["category"] in categories, issue["id"]
        assert issue["class"] and issue["detectable_by"] and all(isinstance(tool, str) and tool for tool in issue["detectable_by"]), issue["id"]
        assert "static-review" in issue["detectable_by"], issue["id"]
        assert ("in-process-check" in issue["detectable_by"]) is (issue["id"] in DEMONSTRATED_IN_PROCESS), issue["id"]
    assert len({issue["class"] for issue in issues}) == len(issues)
    assert [issue["id"] for issue in issues if "squawk" in issue["detectable_by"]] == ["F3-03"]       # all that squawk 2.72.0 flagged
    assert [issue["id"] for issue in issues if "postgres" in issue["detectable_by"]] == ["F3-07"]
    assert all(callable(globals().get(name)) for name in DEMONSTRATED_IN_PROCESS.values())
    extras = key["known_extras"]                    # real weaknesses outside the seven, by class, so a finding of one is not a false positive
    assert len(extras) == 1 and set(extras[0]) == {"file", "class", "note"} and (F3 / extras[0]["file"]).is_file()
    assert [issue["file"] for issue in issues if issue["file"].startswith("postgres/")] == ["postgres/migrations/0003_create_orders.up.sql"]


def test_migrations_apply_from_scratch_on_sqlite(tmp_path, monkeypatch):
    migrate = load_migrate(monkeypatch)
    database = tmp_path / "shop.db"
    connection = sqlite3.connect(str(database))
    migrate.apply(connection, MIGRATIONS, "up")
    assert tables_of(connection) == TABLES
    assert columns_of(connection, "customers") == ["id", "email", "created_at", "given_name", "family_name"]
    migrate.apply(connection, MIGRATIONS, "down")                      # every step that has a down step, newest first
    assert tables_of(connection) == {"audit_log"}
    connection.close()
    assert [path.name for path in tmp_path.iterdir()] == ["shop.db"]


def test_one_migration_has_no_down_step():
    for directory in (MIGRATIONS, PG_MIGRATIONS):
        ups = sorted(path.name[:-len(".up.sql")] for path in directory.glob("*.up.sql"))
        downs = sorted(path.name[:-len(".down.sql")] for path in directory.glob("*.down.sql"))
        assert len(ups) == 5 and all(re.fullmatch(r"\d{4}_[a-z_]+", name) for name in ups), directory
        assert [name for name in ups if name not in downs] == ["0005_create_audit_log"], directory
        assert [name for name in downs if name not in ups] == [], directory
        assert [path.name for path in directory.iterdir() if not re.fullmatch(r"\d{4}_[a-z_]+\.(up|down)\.sql", path.name)] == []
    assert sorted(path.name for path in MIGRATIONS.iterdir()) == sorted(path.name for path in PG_MIGRATIONS.iterdir())


def test_store_opens_only_the_path_it_is_given(tmp_path, monkeypatch):
    module = load_store(monkeypatch)
    with pytest.raises(TypeError):
        module.Store()
    opened = []
    real = sqlite3.connect

    def record(*args, **kwargs):
        opened.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", record)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    given = tmp_path / "given.db"
    module.Store(given).close()
    assert opened == [((str(given),), {})]
    assert [path.name for path in elsewhere.iterdir()] == [] and given.is_file()
    text = (F3 / "shop_db" / "store.py").read_text(encoding="utf-8")
    assert not re.search(r"""['"][^'"\n]*(\.db|\.sqlite3?|:memory:)['"]""", text)


def test_the_schema_allows_the_rows_the_key_describes(tmp_path, monkeypatch):
    _module, store = migrated_store(monkeypatch, tmp_path / "shop.db")
    connection = store.connection
    first = store.add_customer("same@example.test", "Ada", "Lovelace")
    second = store.add_customer("same@example.test", "Grace", "Hopper")
    assert first != second                                                       # the identity field takes a second row
    connection.execute("INSERT INTO orders (customer_id) VALUES (4040)")           # no customer 4040
    assert connection.execute("SELECT COUNT(*) FROM orders WHERE customer_id NOT IN (SELECT id FROM customers)").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO order_items (order_id, line_no, product_id, quantity, unit_price_cents) VALUES (9, 1, 9, 1, 1)")
    store.add_product("sku-1", "pen", 5, 100)
    with pytest.raises(sqlite3.IntegrityError):
        store.add_product("sku-1", "pen again", 5, 100)                           # the constraint that is there works


def test_the_migration_that_drops_a_column_moves_nothing(tmp_path, monkeypatch):
    _module, store = migrated_store(monkeypatch, tmp_path / "shop.db", upto=3)
    store.connection.execute("INSERT INTO customers (email, name) VALUES ('a@example.test', 'Ada Lovelace')")
    store.connection.commit()
    store.connection.executescript((MIGRATIONS / "0004_split_customer_name.up.sql").read_text(encoding="utf-8"))
    assert "name" not in columns_of(store.connection, "customers")
    assert [tuple(row) for row in store.connection.execute("SELECT given_name, family_name FROM customers")] == [("", "")]


def test_the_list_of_orders_asks_the_database_once_per_order(tmp_path, monkeypatch):
    _module, store = migrated_store(monkeypatch, tmp_path / "shop.db")
    customer = store.add_customer("a@example.test", "Ada", "Lovelace")
    product = store.add_product("sku-1", "pen", 1000, 100)
    counted = {}
    for orders in (2, 6):
        while len(store.orders_for_customer(customer)) < orders:
            store.place_order(customer, [(product, 1)])
        statements = []
        store.connection.set_trace_callback(lambda text: statements.append(text) if text.lstrip().upper().startswith("SELECT") else None)
        listing = store.orders_for_customer(customer)
        store.connection.set_trace_callback(None)
        assert len(listing) == orders and all(len(order["items"]) == 1 for order in listing)
        counted[orders] = len(statements)
    assert counted[6] - counted[2] == 4 and counted[2] == 1 + 2


def test_two_connections_that_read_before_either_writes_lose_an_update(tmp_path, monkeypatch):
    module, store = migrated_store(monkeypatch, tmp_path / "shop.db")
    product = store.add_product("sku-1", "pen", 5, 100)
    rival = module.Store(tmp_path / "shop.db")

    class Interleaved(module.Store):
        """The first write waits for a second connection to finish its own decrement."""
        pending = True

        def _write_stock(self, product_id, stock):
            if self.pending:
                self.pending = False
                rival.decrement_stock(product_id, 2)
            super()._write_stock(product_id, stock)

    first = Interleaved(tmp_path / "shop.db")
    first.decrement_stock(product, 2)
    assert rival.stock_of(product) == 3            # two decrements of 2 from 5 leave 1; the first one wrote over the second
    for connection in (first, rival, store):
        connection.close()


def test_an_order_with_a_repeated_product_or_a_zero_quantity_changes_nothing(tmp_path, monkeypatch):
    module, store = migrated_store(monkeypatch, tmp_path / "shop.db")
    customer = store.add_customer("a@example.test", "Ada", "Lovelace")
    first, second = store.add_product("sku-1", "pen", 10, 100), store.add_product("sku-2", "ink", 10, 100)
    for lines in ([(first, 1), (first, 2)], [(first, 1), (second, 0)], [(first, 1), (second, -1)]):
        with pytest.raises(ValueError):
            store.place_order(customer, lines)
        assert (store.stock_of(first), store.stock_of(second), store.orders_for_customer(customer)) == (10, 10, [])


def test_the_smoke_path_decrements_once_and_lists_the_order(tmp_path, monkeypatch):
    module, store = migrated_store(monkeypatch, tmp_path / "shop.db")
    customer = store.add_customer("a@example.test", "Ada", "Lovelace")
    product = store.add_product("sku-1", "pen", 10, 150)
    order = store.place_order(customer, [(product, 2)])
    assert store.stock_of(product) == 8
    assert store.orders_for_customer(customer) == [{"id": order, "status": "new", "items": [
        {"product_id": product, "quantity": 2, "unit_price_cents": 150}]}]
    with pytest.raises(module.OutOfStock):
        store.place_order(customer, [(product, 9)])
    assert store.stock_of(product) == 8


def test_a_lookup_of_the_items_of_an_order_is_indexed_on_sqlite(tmp_path, monkeypatch):
    _module, store = migrated_store(monkeypatch, tmp_path / "shop.db")
    plan = " ".join(row[3] for row in store.connection.execute("EXPLAIN QUERY PLAN SELECT * FROM order_items WHERE order_id = 7"))
    assert "SEARCH order_items" in plan and "SCAN" not in plan


@pytest.mark.parametrize("dialect", SCHEMA_CASES)
def test_the_variants_hold_the_same_tables_and_no_dsn(dialect):
    directory = MIGRATIONS if dialect == "sqlite" else PG_MIGRATIONS
    text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(directory.glob("*.up.sql")))
    assert set(re.findall(r"CREATE TABLE (\w+)", text)) == TABLES
    assert ("GENERATED ALWAYS AS IDENTITY" in text) is (dialect == "postgres")
    assert ("INTEGER PRIMARY KEY" in text) is (dialect == "sqlite")
    assert re.search(r"CREATE INDEX \w+ ON orders \(customer_id\)", text)
    assert ("PRIMARY KEY (order_id, line_no)" in text) is (dialect == "sqlite")
    for path in F3.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            assert "postgresql://" not in path.read_text(encoding="utf-8"), path


def test_the_postgres_variant_has_no_index_on_the_column_of_the_items_lookup():
    text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(PG_MIGRATIONS.glob("*.up.sql")))
    assert not re.search(r"CREATE (UNIQUE )?INDEX[^;]*ON order_items", text) and "UNIQUE (order_id" not in text
    assert "PRIMARY KEY (order_id" not in text and "FOREIGN KEY (order_id)" not in text
    assert re.search(r"CREATE TABLE order_items \(\n(?:.*\n)*?\s+order_id BIGINT NOT NULL REFERENCES orders \(id\)", text)


PG_SERVICE = """export POSTGRES_HOST_AUTH_METHOD=trust PGDATA=/tmp/pgdata
exec docker-entrypoint.sh postgres -c listen_addresses=127.0.0.1 -c fsync=off
"""
PG_LEG = """export QA_FIXTURE_DSN=postgresql://postgres@127.0.0.1:5432/postgres
for _ in $(seq 1 200); do
  pg_isready -h 127.0.0.1 -q && break
  sleep 0.3
done
cd /work/.qa-input
psql() { command psql "$QA_FIXTURE_DSN" -X -q -v ON_ERROR_STOP=1 "$@"; }
for step in postgres/migrations/*.up.sql; do psql -f "$step" || exit 11; done
psql -At -c "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY 1" > /qa-evidence/tables-up.txt
psql -c "INSERT INTO customers (email, given_name, family_name) SELECT 'c' || g || '@example.test', 'a', 'b' FROM generate_series(1, 100) g" || exit 12
psql -c "INSERT INTO products (sku, name, stock, price_cents) SELECT 'sku-' || g, 'item', 100, 100 FROM generate_series(1, 10) g" || exit 12
psql -c "INSERT INTO orders (customer_id) SELECT 1 + g % 100 FROM generate_series(1, 3000) g" || exit 12
psql -c "INSERT INTO order_items (order_id, line_no, product_id, quantity, unit_price_cents) SELECT 1 + g % 3000, g, 1 + g % 10, 1, 100 FROM generate_series(1, 30000) g" || exit 12
psql -c "ANALYZE"
psql -At -c "EXPLAIN (COSTS OFF) SELECT * FROM order_items WHERE order_id = 42" > /qa-evidence/plan-before.txt
psql -c "CREATE INDEX idx_probe ON order_items (order_id)" -c "ANALYZE"
psql -At -c "EXPLAIN (COSTS OFF) SELECT * FROM order_items WHERE order_id = 42" > /qa-evidence/plan-after.txt
# the step that has no down step leaves a table that points at orders, so the down steps below it can fail: each result is kept
for step in $(ls postgres/migrations/*.down.sql | sort -r); do
  psql -f "$step" 2> /dev/null
  echo "$(basename "$step") $?" >> /qa-evidence/down-status.txt
done
psql -At -c "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY 1" > /qa-evidence/tables-down.txt
"""


def refused(message, done):
    return "%s: exit %s, %s" % (message, done[0], json.dumps(done[1].get("error"), sort_keys=True))


@pytest.mark.docker
def test_postgres_variant_applies_as_a_service(live, qa_cli, tmp_path):  # noqa: F811
    workspace = tmp_path / "pg-workspace"                      # this test's own workspace, never the repository's .qa/
    workspace.mkdir()
    cli = lambda *args: qa_cli(*args, cwd=workspace, workspace=workspace)  # noqa: E731
    assert cli("init")[0] == 0
    assert cli("config", "set", "project.trust", "trusted", "--approval", USER_APPROVAL)[0] == 0
    environment = [{"id": "fixture-pg", "class": "ephemeral", "base_urls": [], "dsn_env": "QA_FIXTURE_DSN", "ephemeral_basis": "user-declared"}]   # the kernel records suite-created itself, and not yet for a service
    declared = cli("config", "set", "environments", json.dumps(environment), "--approval", USER_APPROVAL)
    if declared[0] != 0:
        pytest.skip("kernel refused the fixture-pg declaration: " + json.dumps(declared[1]["error"], sort_keys=True))
    granted = cli("authorize", "--environment", "fixture-pg", "--activity", "db-ephemeral", "--approval", USER_APPROVAL)
    if granted[0] != 0:
        pytest.skip("kernel refused the db-ephemeral grant: " + json.dumps(granted[1]["error"], sort_keys=True))
    inputs = [(path.relative_to(F3).as_posix(), path) for path in sorted((F3 / "postgres").rglob("*.sql"))]
    container.project_archive(live.root, live.archive, 1 << 20, inputs=inputs)
    spec = container.ContainerSpec(execution_id=live.execution_id, image=POSTGRES_IMAGE, script=PG_LEG, install_script=None,
                                   archive=live.archive, workdir="/work", timeout_s=240.0, max_bytes=8 << 20,
                                   service=container.ServiceSpec(image=POSTGRES_IMAGE, script=PG_SERVICE, user="70:70"))
    done = container.run(live.engine, spec)
    assert (done.run.exit_code, done.evidence_error, done.cleanup_failed) == (0, None, ()), (done.run.stderr, done.service_log)
    tables = set(done.evidence["tables-up.txt"].decode().split())
    assert tables == TABLES
    before, after = done.evidence["plan-before.txt"].decode(), done.evidence["plan-after.txt"].decode()
    assert "Seq Scan on order_items" in before and "Index" not in before        # the plan, not a timing, is what differs
    assert "Index" in after and "Seq Scan" not in after
    assert done.evidence["down-status.txt"].decode().splitlines() == [
        "0004_split_customer_name.down.sql 0", "0003_create_orders.down.sql 3", "0002_create_products.down.sql 0",
        "0001_create_customers.down.sql 0"]                      # psql exits 3 on a failed script: audit_log keeps orders in place
    assert set(done.evidence["tables-down.txt"].decode().split()) == {"audit_log", "orders"}
    assert (done.network_mode, done.anomalies) == ("none", ()) and done.service["running_at_end"] is True
    assert_no_service_left(live)


# ----------------------------------------------------------------------------------------------- the qa-database legs
# Each leg's command is read from the ``qa-leg`` blocks of the skill's references/tools.md, the one text the skill follows,
# and run through the real CLI in the fixture copy's own workspace. The A5 grant (db-ephemeral on fixture-pg, the user's
# words) is recorded there, never in the repository's .qa/.

DB_TOOLS_MD = REPO / "skills" / "qa-database" / "references" / "tools.md"
DB_SCRIPTS = REPO / "skills" / "qa-database" / "scripts"
DB_LEG_PREFIX = ["python3", ".claude/skills/qa-database/scripts/qa.py", "--role", "auditor", "--skill", "qa-database"]
A5_APPROVAL = "Hacer lo necesario para ejecutar el requerimiento, no me preguntes, ejecute de forma autonoma."
FIXTURE_PG = {"id": "fixture-pg", "class": "ephemeral", "base_urls": [], "dsn_env": "QA_FIXTURE_DSN", "ephemeral_basis": "user-declared"}
DB_LEG_IMAGES = {"squawk": "squawk", "sqlite-roundtrip": "python-newest", "postgres-roundtrip": "postgres"}
DB_LEG_MIGRATIONS = {"squawk": "postgres/migrations", "sqlite-roundtrip": "migrations", "postgres-roundtrip": "postgres/migrations"}


def registry_image(image_id):
    listing = json.loads((REPO / "shared" / "policies" / "container-images.json").read_text(encoding="utf-8"))
    return next(entry["image"] for entry in listing["images"] if entry["id"] == image_id)


def db_legs():
    """``{name: (attributes, the command line)}`` from the ``qa-leg`` fenced blocks of qa-database's tools.md."""
    text = DB_TOOLS_MD.read_text(encoding="utf-8")
    found = {}
    for match in re.finditer(r"^```qa-leg (\S+)((?: [\w-]+=\S+)*)\n(.*?)\n```$", text, re.M | re.S):
        assert match.group(1) not in found, match.group(1)
        found[match.group(1)] = (dict(a.split("=", 1) for a in match.group(2).split()), match.group(3).strip())
    return found


def db_leg_words(name, execution=None):
    attributes, line = db_legs()[name]
    line = line.replace("<migrations>", DB_LEG_MIGRATIONS.get(name, "postgres/migrations"))
    if execution is not None:
        line = line.replace("<EXE>", execution)
    assert "<migrations>" not in line and (execution is None or "<EXE>" not in line), line
    return attributes, shlex.split(line)


def pg_plan_sql():
    match = re.search(r"^```sql pg-plan\n(.*?)\n```$", DB_TOOLS_MD.read_text(encoding="utf-8"), re.M | re.S)
    assert match, "the pg-plan block"
    return match.group(1) + "\n"


def db_leg_cli(project, words, timeout=1500):
    assert words[:len(DB_LEG_PREFIX)] == DB_LEG_PREFIX, words[:len(DB_LEG_PREFIX)]
    done = subprocess.run([sys.executable, str(LAUNCHER), "--workspace", str(project.root), "--role", "auditor", "--skill", "qa-database",
                           *words[len(DB_LEG_PREFIX):]], cwd=str(project.root), capture_output=True, timeout=timeout)
    return done.returncode, json.loads(done.stdout)


def db_script(project, words):
    """A ``python3 .claude/skills/qa-database/scripts/<name>`` block, run from the project root with the repository's copy."""
    assert words[0] == "python3" and words[1].startswith(".claude/skills/qa-database/scripts/"), words[:2]
    done = subprocess.run([sys.executable, "-I", "-B", str(DB_SCRIPTS / words[1].rsplit("/", 1)[1])] + words[2:], cwd=str(project.root),
                          capture_output=True, text=True, timeout=120)
    return done.returncode, json.loads(done.stdout)


def declare_fixture_pg_and_grant(project):
    environments = [{"id": "local", "class": "local", "base_urls": ["http://localhost", "http://127.0.0.1", "http://[::1]"]}, FIXTURE_PG]
    code, out = project.cli("config", "set", "environments", json.dumps(environments), "--approval", A5_APPROVAL)
    assert code == 0, out
    code, out = project.cli("authorize", "--environment", "fixture-pg", "--activity", "db-ephemeral", "--approval", A5_APPROVAL)
    assert code == 0 and out["ok"], out
    return out["id"]


def db_recorded(project, execution):
    return project.cli("state", "show", "executions")[1]["state"][execution]


def test_the_database_legs_are_the_documented_ones_with_pinned_images_and_no_shared_database():
    found = db_legs()
    assert sorted(found) == ["postgres-roundtrip", "sqlite-roundtrip", "squawk", "squawk-ingest", "squawk-sarif"]
    assert {name: attributes["grant"] for name, (attributes, _) in found.items()} == {
        "squawk": "none", "squawk-sarif": "none", "squawk-ingest": "none", "sqlite-roundtrip": "none", "postgres-roundtrip": "db-ephemeral"}
    for name, image_id in DB_LEG_IMAGES.items():
        _, words = db_leg_words(name)
        assert words[words.index("--container") + 1] == registry_image(image_id), name
        report = found[name][0]["report"]
        assert report == (words[words.index("--report") + 1].split(":", 1)[0] if "--report" in words else "none"), name
    _, squawk = db_leg_words("squawk")
    assert squawk[-1] == "squawk --reporter json /work/postgres/migrations/*.sql"       # files by explicit glob: squawk skips dot directories
    assert "skips files under dot directories" in DB_TOOLS_MD.read_text(encoding="utf-8")
    _, pg = db_leg_words("postgres-roundtrip")
    assert pg[pg.index("--service") + 1] == registry_image("postgres") and pg[pg.index("--service-user") + 1] == "70:70"
    assert (pg[pg.index("--environment") + 1], pg[pg.index("--activity") + 1]) == ("fixture-pg", "db-ephemeral")
    assert "EXPLAIN (FORMAT JSON)" in pg_plan_sql()
    text = " ".join(line for _, line in found.values())
    assert "postgresql://" not in text and "PGPASSWORD" not in text and not re.search(r"password", text, re.I)
    assert set(re.findall(r"host=([^\s\"']+)", text)) == {"127.0.0.1"}


def test_the_postgres_leg_is_refused_with_only_the_db_ephemeral_grant(qa_on_fixture):
    """A tripwire, not a requirement. With only A5's db-ephemeral grant on fixture-pg, the kernel refuses the PostgreSQL leg,
    because it checks the --service-run as test-execution-local on fixture-pg too. Once the user decides that grant, or the
    kernel reads a fixture service under db-ephemeral, this test fails on purpose: then run the docker leg and update it."""
    project = qa_on_fixture("sql-database")
    declare_fixture_pg_and_grant(project)
    project.root.joinpath(".qa", "work", "database").mkdir(parents=True)
    project.root.joinpath(".qa", "work", "database", "pg-plan.sql").write_text(pg_plan_sql(), encoding="utf-8")
    code, out = db_leg_cli(project, db_leg_words("postgres-roundtrip")[1])
    assert (code, out["status"], out["reason"]) == (3, "blocked", "not_authorized"), out
    refusals = [note for note in out["limitations"] if "not_authorized" in note]
    assert [note.split(":", 1)[0] for note in refusals] == ["test-execution-local on environment fixture-pg"], out["limitations"]


@pytest.fixture
def db_project(qa_on_fixture):
    engine = os.environ.get("QA_TEST_CONTAINER_ENGINE")
    if not engine:
        pytest.skip("tool missing: QA_TEST_CONTAINER_ENGINE is not set")
    project = qa_on_fixture("sql-database")
    assert project.cli("config", "set", "container.engine", engine, "--approval", "fixture test")[0] == 0
    declare_fixture_pg_and_grant(project)
    return project


@pytest.mark.docker
def test_f3_squawk_leg(db_project):
    code, out = db_leg_cli(db_project, db_leg_words("squawk")[1])
    print("squawk leg:", out["execution"], out["status"], out.get("reason"), out.get("strength"))
    assert (code, out["status"]) == (1, "failed"), out
    run = db_recorded(db_project, out["execution"])
    assert run["container"]["network_mode"] == "none" and run["container"]["service"] is None
    stdout = db_project.root / ".qa" / "evidence" / out["execution"] / "stdout.log"
    violations = json.loads(stdout.read_text(encoding="utf-8"))
    found = {(v["rule_name"], v["file"], v["line"]) for v in violations}
    assert ("ban-drop-column", "/work/postgres/migrations/0004_split_customer_name.up.sql", 2) in found      # F3-03, 0-based line
    assert len(violations) == 43 and len({v["file"] for v in violations}) == 9      # every file of the glob, as measured in Task 6
    code, converted = db_script(db_project, db_leg_words("squawk-sarif", out["execution"])[1])
    assert code == 0 and converted["results"] == len(violations) and "ban-drop-column" in converted["rules"], converted
    code, ingested = db_leg_cli(db_project, db_leg_words("squawk-ingest", out["execution"])[1])
    print("squawk ingest:", json.dumps(ingested, sort_keys=True)[:400])
    assert code == 0 and ingested["ok"], ingested
    assert (ingested["execution"], ingested["kind"], ingested["total"]) == (out["execution"], "alerts", len(violations)), ingested
    sarif = json.loads((db_project.root / ".qa" / "work" / "database" / "squawk.sarif").read_text(encoding="utf-8"))
    located = {(r["ruleId"], r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"], r["locations"][0]["physicalLocation"]["region"]["startLine"])
               for r in sarif["runs"][0]["results"]}
    assert ("ban-drop-column", "postgres/migrations/0004_split_customer_name.up.sql", 3) in located


@pytest.mark.docker
def test_f3_sqlite_roundtrip_leg(db_project):
    copied = subprocess.run([sys.executable, "-I", "-B", str(DB_SCRIPTS / "migration_roundtrip.py"), "--copy-to-work"], cwd=str(db_project.root),
                            capture_output=True, text=True, timeout=60)
    assert copied.returncode == 0, copied.stdout + copied.stderr
    code, out = db_leg_cli(db_project, db_leg_words("sqlite-roundtrip")[1])
    print("sqlite roundtrip leg:", out["execution"], out["status"], out.get("reason"), out["tests"])
    assert (code, out["status"]) == (1, "failed"), out
    assert out["tests"] == {"total": 17, "passed": 11, "failed": 6, "errors": 0, "skipped": 0}
    run = db_recorded(db_project, out["execution"])
    assert run["container"]["network_mode"] == "none" and run["container"]["image"] == registry_image("python-newest")
    assert run["reports"][0]["format"] == "junit" and run["reports"][0]["files"] == ["reports/roundtrip.xml"]
    junit = (db_project.root / ".qa" / "evidence" / out["execution"] / "reports" / "roundtrip.xml").read_text(encoding="utf-8")
    assert "customers.name dropped" in junit and "0005_create_audit_log.down.sql is missing" in junit and "FOREIGN KEY constraint failed" in junit


@pytest.mark.docker
def test_f3_postgres_roundtrip_and_plan_leg(db_project):
    work = db_project.root / ".qa" / "work" / "database"
    work.mkdir(parents=True)
    (work / "pg-plan.sql").write_text(pg_plan_sql(), encoding="utf-8")
    code, out = db_leg_cli(db_project, db_leg_words("postgres-roundtrip")[1])
    print("postgres leg:", out["execution"], out["status"], out.get("reason"), json.dumps(out.get("limitations"), sort_keys=True))
    if (out["status"], out.get("reason")) == ("blocked", "not_authorized"):
        pytest.skip("kernel refused the postgres leg under the db-ephemeral grant alone (%s): %s"
                    % (out["execution"], json.dumps([n for n in out["limitations"] if "not_authorized" in n])))
    assert (code, out["status"]) == (1, "failed"), out
    run = db_recorded(db_project, out["execution"])
    assert run["container"]["service"]["image"] == registry_image("postgres") and run["container"]["network_mode"] == "none"
    stdout = (db_project.root / ".qa" / "evidence" / out["execution"] / "stdout.log").read_text(encoding="utf-8")
    steps = dict(line.rsplit(" ", 1) for line in stdout.splitlines() if re.match(r"(up|down|reup) \d{4}_", line))
    assert all(steps["up " + name] == "0" for name in ("0001_create_customers.up.sql", "0003_create_orders.up.sql", "0005_create_audit_log.up.sql"))
    assert steps["down 0003_create_orders.down.sql"] != "0"
    plan = json.loads(stdout.split("plan-begin", 1)[1].split("plan-end", 1)[0])
    assert plan[0]["Plan"]["Node Type"] == "Seq Scan" and plan[0]["Plan"]["Relation Name"] == "order_items"
