"""Two-connection race test. Copy it to .qa/work/database/test_race_<name>.py, fill the parts marked FILL, run it with qa run.

Run from the project root, after `config set project.trust trusted` from the user's words:
    python3 .claude/skills/qa-database/scripts/qa.py --role auditor --skill qa-database run --category data-integrity \
        --report junit:{evidence}/race.xml -- python3 -B -m pytest -p no:cacheprovider \
        .qa/work/database/test_race_<name>.py --junitxml={evidence}/race.xml

What it does: it builds a fresh SQLite file beside this test (under .qa/work/database/, never elsewhere) and opens TWO
connections to it through the project's own code, one per thread. The method that reads the contested value is wrapped
so that each caller, after reading, waits until the other has read too; then both go on and write. That forces the
interleaving a lost update needs instead of hoping thread timing finds it, so each run gives the same answer.

The test asserts the invariant the user cares about (for an oversell: at most one caller gets the last unit, and the
stock never goes negative). When the code loses an update, the assertion fails with a message that starts "lost update",
and that failing execution is the evidence. A pass means the code serialised the operation in this interleaving (a
transaction that locks before reading, or a conditional UPDATE ... WHERE stock >= ?).

If the operation never calls the wrapped read (a conditional update does not read first), both callers simply run, and
the invariant check alone decides.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

DB = Path(__file__).resolve().parent / "race.sqlite"
MIGRATIONS = Path.cwd() / "migrations"  # FILL: the project's migration directory, relative to the project root.
READ_METHOD = "read_the_value"  # FILL: the name of the method that reads the contested value (a stock or balance lookup).


def open_store():
    """FILL: open ONE connection through the project's own code, on DB; for example
    ``from shop.store import Store`` then ``return Store(DB)``."""
    raise NotImplementedError("FILL open_store")


def prepare(store):
    """FILL: build the schema with the project's own migration code, then the state just before the race (for an
    oversell: one product with one unit left, and two customers). Return the ids ``operation`` needs, as a dict."""
    raise NotImplementedError("FILL prepare")


def operation(store, ids, caller):
    """FILL: the call the two callers compete for, as the application makes it (for an oversell: caller 0 and caller 1
    each place an order for the last unit). An exception it raises is a refusal, which is a correct outcome."""
    raise NotImplementedError("FILL operation")


def invariant(connection, ids, successes):
    """FILL: read the result with plain SQL and return ``(holds, what was found)``; for an oversell, for example
    ``stock = connection.execute("SELECT stock FROM products WHERE id = ?", (ids["product"],)).fetchone()[0]`` and
    ``holds = successes <= 1 and stock >= 0``."""
    raise NotImplementedError("FILL invariant")


def fresh_database():
    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = Path(str(DB) + suffix)
        if candidate.is_symlink() or candidate.exists():
            candidate.unlink()


def test_two_callers_cannot_break_the_invariant():
    fresh_database()
    setup = open_store()
    try:
        ids = prepare(setup)
    finally:
        setup.close()

    both_read = threading.Barrier(2, timeout=10)
    outcomes = {}

    def attempt(caller):
        store = open_store()  # each caller has its own connection, opened in its own thread
        original = getattr(store, READ_METHOD)

        def pausing_read(*args, **kwargs):
            value = original(*args, **kwargs)
            both_read.wait()  # wait here until the other caller has read the same value
            return value

        setattr(store, READ_METHOD, pausing_read)
        try:
            operation(store, ids, caller)
            outcomes[caller] = "ok"
        except threading.BrokenBarrierError:
            outcomes[caller] = "no interleaving: the other caller stopped before reading"
        except Exception as exc:  # an out-of-stock answer or a lock timeout is a correct refusal
            outcomes[caller] = "%s: %s" % (type(exc).__name__, exc)
        finally:
            store.close()
            both_read.abort()

    threads = [threading.Thread(target=attempt, args=(caller,)) for caller in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    successes = sum(1 for outcome in outcomes.values() if outcome == "ok")
    check = sqlite3.connect(str(DB))
    try:
        holds, found = invariant(check, ids, successes)
    finally:
        check.close()
    assert holds, "lost update: %d of 2 callers succeeded; %s; outcomes %r" % (successes, found, outcomes)
