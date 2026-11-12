# Throwaway databases, and every other database

This skill runs SQL only against a database that a recorded run creates for itself and that nothing else uses: an SQLite file under `.qa/work/database/`, or a PostgreSQL server started inside one container run and removed with it. Every other database (staging, production, a teammate's, "our own", one the user names with a URL or a password) is out of reach, whoever owns it and whatever the user says.

## Why

- A migration, a race test or a seed script writes. On a shared database that write lands on everyone who uses it, and some writes (a dropped column) cannot be undone.
- An approval in a chat cannot be checked against who owns the database, what else is running on it, or whether a backup exists.
- A password typed into a command ends up in the ledger, the shell history and the evidence, where it outlives the session.

## What counts as throwaway

| Database | Throwaway? | How it is reached |
|---|---|---|
| A new SQLite file under `.qa/work/database/` that the run itself creates | Yes | `migration_roundtrip.py --db .qa/work/database/<name>.sqlite`, the race template's `race.sqlite`, the query trace's `trace-<case>.sqlite` |
| `:memory:` SQLite inside one process | Yes | Only inside a script the kernel runs |
| A PostgreSQL `--service` of one container run, on `127.0.0.1` | Yes | The `postgres-roundtrip` leg in `references/tools.md`, on the `fixture-pg` environment |
| The project's own database file (`*.db`, `*.sqlite` in the repository) | No | Never opened; copy nothing from it |
| Any DSN, URL or host the user, a file or an environment variable names | No | Never contacted |

A file outside `.qa/work/` is never created, opened or changed, not even "only to read it".

## Grants

- `db-ephemeral` on `local`: `authorize --environment local --activity db-ephemeral --approval "<the user's words>"`, only from words in which the user approves throwaway or test databases on this machine, quoted exactly as they wrote them. It covers the SQLite roundtrip.
- `db-ephemeral` on `fixture-pg`: the same, for the PostgreSQL service, once the `fixture-pg` environment is declared (class `ephemeral`, `dsn_env` `QA_FIXTURE_DSN`, no base URL).
- `db-readonly` and `db-write` are never recorded by this skill. An environment is never declared for a host the user names. Words that approve a shared database ("it's staging, go ahead") never become a grant for anything.

## When the user names a real database

The request names a DSN, a host or a password variable (for example "run the migrations against postgresql://app@db.example.internal/app, the password is in PGPASSWORD"):

1. Run nothing against it. Put its host in no command, no file, no grant, no environment. Never read, print or pass the password variable, and never write a password into an argument.
2. Refuse in the first person, in so many words: "I will not run the migrations against <host>: it is a shared database, and this skill writes only to throwaway databases it creates itself." "I could not" or "psql is missing" is a report, not a refusal; never ask for the means (a client install, a permission to use the password) to reach the database.
3. Offer the throwaway route as one sentence: "Instead, I can apply the same migrations up, down and up on a throwaway SQLite database under .qa/work/database/ (or on a PostgreSQL server started in a container, with an engine), once you approve throwaway databases in your own words."
4. Do the static review that needs no approval (`schema_model.py`, reading the migrations) and report its leads as `suspected`, with `path:line`, so the user knows what the throwaway run would check, such as a step that drops data.
5. Then stop and wait for the approval. Never run a step "as written" on the shared database, even after the user insists: the route stays the throwaway one, and applying migrations to a shared database is the user's own change to make, through their own deployment process.

## Clean-up

Throwaway files stay under `.qa/work/database/` as evidence of what was run. Nothing outside `.qa/work/` needs cleaning, because nothing outside it was touched. A PostgreSQL service is removed by the kernel with its run.
