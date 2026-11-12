# Tools

This skill installs nothing and downloads nothing. Its own scripts use only Python's standard library (`sqlite3` included) and run through the kernel. Every external tool (squawk, PostgreSQL) runs only through the kernel, in a container from an image pinned by digest, with no network except the loopback it shares with a service the same run starts. The kernel never pulls an image. An engine that does not answer, or an image that is not on the machine, makes the run `blocked`: that record is the result. Never run the tool another way (a local install, `brew`, `pip install`, `npx`, a binary found on the PATH, a `psql` on the host).

Every kernel call is written out in full, from the project root:

`python3 .claude/skills/qa-database/scripts/qa.py --role auditor --skill qa-database <command>`

## Kernel commands used

| Command | Use |
|---|---|
| `init`, `config show` | The workspace; `project.trust` and the declared environments |
| `authorize --environment local --activity db-ephemeral --approval "<the user's words>"` | The grant a migration roundtrip on a throwaway SQLite file needs. Only words in which the user approves throwaway or test databases on this machine; it expires at the end of the day |
| `run --activity db-ephemeral --category data-integrity --report junit:{evidence}/roundtrip.xml -- python3 .claude/skills/qa-database/scripts/migration_roundtrip.py --migrations <dir> --db .qa/work/database/roundtrip.sqlite --junit {evidence}/roundtrip.xml` | The roundtrip (up, down, up) on a fresh SQLite file under `.qa/work/database/`, reported as JUnit. Exit codes of `run`: 0 passed, 1 failed, 3 blocked, 4 inconclusive, 5 not_run |
| `config set project.trust trusted --approval "<the user's words>"` | Only from words that approve running the project's code on this machine; needed for a race test or a query trace, which import the project |
| `run --category data-integrity --report junit:{evidence}/race.xml -- python3 -B -m pytest -p no:cacheprovider .qa/work/database/<test>.py --junitxml={evidence}/race.xml` | A race test you wrote from `assets/race_template.py` (`references/integrity-checks.md`) |
| `run --category performance --report junit:{evidence}/trace.xml -- python3 .claude/skills/qa-database/scripts/query_trace.py --driver .qa/work/database/trace_driver.py --junit {evidence}/trace.xml` | Statements per call, through `sqlite3.set_trace_callback` |
| `run --container IMAGE@DIGEST [--service IMAGE@DIGEST --service-run SHELL --service-user UID:GID] [--input .qa/work/database/FILE] [--report FORMAT:{evidence}/FILE] --shell -- 'SHELL'` | One tool run in a container. The project is copied to `/work`; an `--input` file under `.qa/work/` is at `/work/.qa-input/<path below .qa/work/>`; `{evidence}` is `/qa-evidence` inside. Without a `--report`, the run's stdout is what is kept (`.qa/evidence/<EXE>/stdout.log`) |
| `ingest sarif .qa/work/database/<file>.sarif --from <EXE> --category data-integrity` | Attach a converted SARIF report to the run that produced it |
| `finding add --json .qa/work/database/<name>.json` | One finding (template in `references/integrity-checks.md`) |
| `state show [executions\|findings\|authorizations]`, `ledger verify` | What was recorded |

The scripts that only read files run directly, from the project root, and write only under `.qa/work/database/`:

- `python3 .claude/skills/qa-database/scripts/schema_model.py --migrations <dir> [--code <package dir>]` writes `.qa/work/database/schema-model.json` and prints the leads (`missing-foreign-key`, `missing-uniqueness`, `drop-without-copy`, `missing-down-step`, `unindexed-foreign-key`, `not-null-without-value`), each with `path:line` and `approximate: true`, and the statements it could not read (`unreadable`).
- `python3 .claude/skills/qa-database/scripts/squawk_to_sarif.py --input .qa/evidence/<EXE>/stdout.log --out .qa/work/database/squawk.sarif` turns a stored squawk JSON report into SARIF 2.1.0.
- `python3 .claude/skills/qa-database/scripts/migration_roundtrip.py --copy-to-work` copies the roundtrip script to `.qa/work/database/`, for a container run's `--input`.

## The images

| Tool | Version | Image (use exactly this) |
|---|---|---|
| squawk | 2.72.0 | `ghcr.io/sbdchd/squawk@sha256:b7c8c1ad82439744e676c0c0b2447d95ea0866f72ae1f4095d8653dfc89db916` (linux/amd64 only) |
| PostgreSQL | 17.11 (alpine) | `postgres@sha256:e5d2edd28b1f8d40a0a6c0c9cdc545b19b56a0d5d88f94bc11a97d970d4700d0` (server user uid 70) |
| Python, for the SQLite roundtrip | 3 (slim) | `python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928` |

An image not in `.claude/skills/qa-database/scripts/_kernel/policies/container-images.json` is not used.

## squawk: lint PostgreSQL migrations

1. Function: reads PostgreSQL migration files and flags statements that are unsafe to run on a live database: dropping a column or a table, adding a `NOT NULL` column without a default, changing a column type, creating an index without `CONCURRENTLY`, adding a foreign key that locks both tables.
2. Use cases: a PostgreSQL migration directory under review; backing a "this step loses data or blocks writes" claim with a rule id and a line.
3. Compatibility: PostgreSQL SQL only (not SQLite or MySQL dialects); plain `.sql` files. Reporters: `tty`, `gcc`, `json`, `gitlab`. It has no SARIF reporter, so `scripts/squawk_to_sarif.py` converts its JSON (lines and columns there are 0-based; the converter makes them 1-based and strips `/work/`).
4. Dependencies: the image above; no service, no network, no grant (`--activity static-analysis`).
5. Alternatives: Atlas `migrate lint` (Apache-2.0, needs a dev database), strong_migrations (MIT, Rails only), sqlfluff (MIT, style rather than safety), `scripts/schema_model.py` (this skill's static leads, both dialects).
6. License, costs, restrictions: GPL-3.0, free; used as an unmodified binary in its own image, nothing of it is distributed. It sends nothing; the container has no network.
7. Reason selected: the reference safety linter for PostgreSQL migrations, with machine-readable output and stable rule ids.

squawk skips files under dot directories (`.qa/`, `.git/`, `.github/`) when it walks a directory, so pass the files by an explicit glob or path, as below. Exit 1 means violations were found, so a run with violations is `failed`; the violations are in the run's `stdout.log`.

```qa-leg squawk grant=none report=none
python3 .claude/skills/qa-database/scripts/qa.py --role auditor --skill qa-database run --container ghcr.io/sbdchd/squawk@sha256:b7c8c1ad82439744e676c0c0b2447d95ea0866f72ae1f4095d8653dfc89db916 --activity static-analysis --category data-integrity --timeout 300 --shell -- 'squawk --reporter json /work/<migrations>/*.sql'
```

Then convert the stored JSON and attach it to that run, `<EXE>` being the run's execution id:

```qa-leg squawk-sarif grant=none report=sarif
python3 .claude/skills/qa-database/scripts/squawk_to_sarif.py --input .qa/evidence/<EXE>/stdout.log --out .qa/work/database/squawk.sarif
```

```qa-leg squawk-ingest grant=none report=sarif
python3 .claude/skills/qa-database/scripts/qa.py --role auditor --skill qa-database ingest sarif .qa/work/database/squawk.sarif --from <EXE> --category data-integrity
```

Quote rule ids and lines from the stored SARIF, never from memory. A squawk alert is provisional: confirm a data loss with the roundtrip.

## SQLite roundtrip: up, down, up on a throwaway file

1. Function: `scripts/migration_roundtrip.py` applies every up step oldest first, every down step newest first, then every up step again, on a fresh SQLite file under `.qa/work/database/`, with synthetic rows before each step so a step that drops data shows as a `loss` failure. One JUnit test case per step.
2. Use cases: "do the migrations apply and roll back cleanly"; showing a missing down step, a down step that fails, a step that loses a column's values, a down that leaves tables behind.
3. Compatibility: SQLite-dialect migrations named `NNNN_<name>.up.sql` and `NNNN_<name>.down.sql`. PostgreSQL-only syntax fails there as an SQLite error; use the PostgreSQL leg for it.
4. Dependencies: Python 3.9 or later and its `sqlite3`; on the host, the `db-ephemeral` grant on `local` from the user's words; in a container, the Python image above.
5. Alternatives: the project's own migration tool against a throwaway file (Alembic, Flyway, golang-migrate), when the user approves running it; the PostgreSQL leg below.
6. License, costs, restrictions: part of this skill (Apache-2.0); SQLite is public domain. It refuses a `--db` outside `.qa/work/`, any symlink on the way, and `ATTACH`, `DETACH` and `VACUUM INTO`.
7. Reason selected: needs no server and no engine, so the roundtrip runs wherever Python runs, under a recorded grant.

On the host (the usual route; see Kernel commands used), or in the pinned Python image after `migration_roundtrip.py --copy-to-work`:

```qa-leg sqlite-roundtrip grant=none report=junit
python3 .claude/skills/qa-database/scripts/qa.py --role auditor --skill qa-database run --container python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 --category data-integrity --timeout 300 --input .qa/work/database/migration_roundtrip.py --report 'junit:{evidence}/roundtrip.xml' --shell -- 'cd /work && python3 -I -B /work/.qa-input/database/migration_roundtrip.py --migrations <migrations> --db .qa/work/database/roundtrip.sqlite --junit /qa-evidence/roundtrip.xml'
```

## PostgreSQL: roundtrip and query plan on a throwaway server

1. Function: a PostgreSQL server started for one run, inside the run's own network namespace, with the migrations applied up, down and up by `psql`, and `EXPLAIN (FORMAT JSON)` of a query kept in the run's stdout.
2. Use cases: migrations in the PostgreSQL dialect; a missing index that only a plan on realistic row counts shows (a sequential scan where an index scan is expected); behaviour SQLite does not share (identity columns, `ALTER TABLE` forms, lock levels).
3. Compatibility: PostgreSQL 17; any migration directory of plain `.sql` files.
4. Dependencies: the PostgreSQL image, as both the service and the tool; the environment `fixture-pg` (class `ephemeral`, `dsn_env` `QA_FIXTURE_DSN`) declared in config; a `db-ephemeral` grant on it recorded from the user's words; the service also runs as test execution (`--service-run`), which the kernel checks on `fixture-pg` as well, so without a grant or standing authorization for that too the run is `blocked`, `not_authorized`. The plan's seed rows and query in `.qa/work/database/pg-plan.sql`, passed with `--input`.
5. Alternatives: Testcontainers (MIT) from the project's own tests; pg_tmp; a disposable cloud branch (Neon, Supabase), which needs network and a credential, so it is out of scope here.
6. License, costs, restrictions: PostgreSQL License, free. The server trusts loopback connections, listens on `127.0.0.1` only, and its data lives in `/tmp` of the service container, removed with it.
7. Reason selected: the real dialect and planner, with nothing outside the run and nothing kept but the run's output; exit-code strength (no report format), so the run's status is the loop's exit status.

Write the plan file first with the Write tool, adapted to the schema (rows enough for the planner to prefer an index, then `ANALYZE`, then the query):

```sql pg-plan
INSERT INTO customers (email, given_name, family_name) SELECT 'c' || g || '@example.test', 'a', 'b' FROM generate_series(1, 100) g;
INSERT INTO products (sku, name, stock, price_cents) SELECT 'sku-' || g, 'item', 100, 100 FROM generate_series(1, 10) g;
INSERT INTO orders (customer_id) SELECT 1 + g % 100 FROM generate_series(1, 3000) g;
INSERT INTO order_items (order_id, line_no, product_id, quantity, unit_price_cents) SELECT 1 + g % 3000, g, 1 + g % 10, 1, 100 FROM generate_series(1, 30000) g;
ANALYZE;
\echo plan-begin
EXPLAIN (FORMAT JSON) SELECT * FROM order_items WHERE order_id = 42;
\echo plan-end
```

```qa-leg postgres-roundtrip grant=db-ephemeral report=none
python3 .claude/skills/qa-database/scripts/qa.py --role auditor --skill qa-database run --environment fixture-pg --activity db-ephemeral --category data-integrity --container postgres@sha256:e5d2edd28b1f8d40a0a6c0c9cdc545b19b56a0d5d88f94bc11a97d970d4700d0 --service postgres@sha256:e5d2edd28b1f8d40a0a6c0c9cdc545b19b56a0d5d88f94bc11a97d970d4700d0 --service-run 'export POSTGRES_HOST_AUTH_METHOD=trust PGDATA=/tmp/pgdata; exec docker-entrypoint.sh postgres -c listen_addresses=127.0.0.1 -c fsync=off' --service-user 70:70 --timeout 600 --input .qa/work/database/pg-plan.sql --shell -- 'export QA_FIXTURE_DSN="host=127.0.0.1 port=5432 user=postgres dbname=postgres"
for i in $(seq 1 200); do pg_isready -q -d "$QA_FIXTURE_DSN" && break; sleep 0.3; done
cd /work/<migrations> || exit 10
failed=0
for f in $(ls *.up.sql | sort); do psql "$QA_FIXTURE_DSN" -X -q -v ON_ERROR_STOP=1 -f "$f" > /dev/null; s=$?; echo "up $f $s"; [ $s -eq 0 ] || failed=1; done
psql "$QA_FIXTURE_DSN" -X -q -At -v ON_ERROR_STOP=1 -f /work/.qa-input/database/pg-plan.sql || exit 12
for f in $(ls *.down.sql | sort -r); do psql "$QA_FIXTURE_DSN" -X -q -v ON_ERROR_STOP=1 -f "$f" > /dev/null; s=$?; echo "down $f $s"; [ $s -eq 0 ] || failed=1; done
for f in $(ls *.up.sql | sort); do psql "$QA_FIXTURE_DSN" -X -q -v ON_ERROR_STOP=1 -f "$f" > /dev/null; s=$?; echo "reup $f $s"; [ $s -eq 0 ] || failed=1; done
exit $failed'
```

- The DSN is a keyword string on loopback with no password; `QA_FIXTURE_DSN` is the variable the `fixture-pg` environment names, so the guard reads the run as `db-ephemeral` on it. Never a URL with a host other than `127.0.0.1`, never a password.
- The stored `stdout.log` holds one line per step (`up|down|reup <file> <psql exit>`) and, between `plan-begin` and `plan-end`, the plan as JSON. A `Seq Scan` on a table filtered by a column that references another table is the missing-index lead's measurement; quote the node from the stored file.
- Exit 1 means a step failed; which one is in `stdout.log`. Exit 10 or 12: the directory or the plan file is wrong; fix your file and run again.

## When a tool cannot run

| The run's record | What it means | What you say |
|---|---|---|
| `blocked`, `dependency_unavailable`, limitation `container_engine_unavailable` | No container engine answered | "<tool> did not run: no container engine on this machine (EXE-…)". No result, no estimate |
| `blocked`, `container_image_missing` | The pinned image is not on the machine; the kernel never pulls | The image the user would need, by digest |
| `blocked`, `not_authorized` | No grant covers an activity the run needs (the reason names it) | Ask for the user's approval in their words, for a throwaway database only; never for a shared one |
| `blocked`, `policy_denied` | The guard refused the command | Read the reason; never rewrite the command to slip past it |
| `failed` | The tool ran and reported failures or violations | Quote them from the stored report or `stdout.log` |

A blocked tool's checks are not done. Your SQLite roundtrip, race test and static leads still stand on their own records, under their own names, never relabelled as the tool's result.
