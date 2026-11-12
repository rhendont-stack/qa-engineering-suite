# Shop database

A small shop (customers, products, orders) as SQL migrations, with an access layer on the Python standard library's `sqlite3`.

## Layout

| Path | What it holds |
| --- | --- |
| `migrations/` | The SQLite schema, one `NNNN_<name>.up.sql` per step and an optional `NNNN_<name>.down.sql` that undoes it. |
| `postgres/migrations/` | The same steps in PostgreSQL dialect. |
| `shop_db/store.py` | `Store(path)`: opens the SQLite file at the path it is given, and never any other. |
| `shop_db/migrate.py` | `apply(connection, directory, "up" or "down")` runs a directory's files in order. |
| `tests/` | A smoke suite. |

## Using it

```
python -c "from shop_db.store import Store; from pathlib import Path; s = Store('shop.sqlite'); print(s.migrate(Path('migrations')))"
python -m pytest
```

## PostgreSQL

The files under `postgres/migrations/` are plain `psql` scripts. A script names the database only through the environment variable
`$QA_FIXTURE_DSN`, which the environment that runs it sets; no connection string is kept here. They are meant for a throwaway server
and nothing else.
