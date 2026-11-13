# Integrity checks

What to look for in a schema, its migrations and the code that uses them; how each kind of weakness is shown on a throwaway database; and the finding template. A static lead is where you start. A recorded execution is what shows it.

## The classes, and what shows each

| Class | Lead (static) | What shows it (recorded) | Category, anchor |
|---|---|---|---|
| A `*_id` column that references nothing | `schema_model.py`: `missing-foreign-key`, `path:line` of the column | A row pointing at a missing parent is accepted (a check you run, or the roundtrip's rows) | `data-integrity`, `FUNC-H2` |
| A natural identifier (email, username, sku, code) with no unique constraint or unique index | `missing-uniqueness` | Two rows with the same value are accepted | `data-integrity`, `FUNC-H2` |
| A step that drops a column or table without copying its data first | `drop-without-copy`; squawk `ban-drop-column` (PostgreSQL) | The roundtrip's `loss` failure on that step | `data-integrity`, `FUNC-C2` |
| An up step with no down step | `missing-down-step` | The roundtrip's failure "has no down step", and any down below it that then fails or leaves tables | `data-integrity`, `FUNC-M1` |
| A down step that fails or leaves tables behind | none | The roundtrip's `down` or "leaves no table" failure, with the SQLite error | `data-integrity`, `FUNC-M1` |
| `NOT NULL` without a default, left out of an insert in the code | `not-null-without-value` | The insert fails in a test | `data-integrity`, `FUNC-M2` |
| Read, then write, of the same value on two connections (stock, balance, counter) | Reading the code: a `SELECT` then an `UPDATE … SET x = ?` computed in Python, with no transaction or condition between them | The race test (below): both callers succeed where one should fail | `data-integrity`, `FUNC-H2` |
| One query per parent row (N+1) | Reading the code: a query inside a loop over rows | `query_trace.py`: the statement repeated once per row | `performance`, `PERF-M2` |
| A lookup or join column with no index | `unindexed-foreign-key` | The PostgreSQL plan: `Seq Scan` on that table at realistic row counts (`references/tools.md`) | `performance`, `PERF-M1` |

Read every `unreadable` statement that `schema_model.py` lists yourself; never guess what it does. Leads are `approximate`: a lead you could not show stays `suspected`, and you say which check would show it.

Cite the line that holds the weakness: the column's line for a missing constraint, the `DROP` line for a loss, line 1 of the up file for a missing down step, the line of the query in the loop for N+1, the read and the write for a race.

## The roundtrip

Run it as `references/tools.md` gives it, after the `db-ephemeral` grant. Then read the stored JUnit (`.qa/evidence/<EXE>/reports/roundtrip.xml`) with Read or Grep. Each failure names its step and phase:

- `roundtrip.loss`: the step dropped values that are in no other column of the same row. That is a data loss on upgrade.
- `roundtrip.down` "has no down step": the step cannot be rolled back.
- `roundtrip.down` with an SQLite error such as `FOREIGN KEY constraint failed`: a down step that cannot run, often because a table a later step created (and could not drop) still refers to it.
- `roundtrip.downs` "leaves tables": what the downs could not remove.
- `roundtrip.reup` with "already exists": the up steps cannot run again on what the downs left.

One roundtrip execution can confirm several findings; each finding cites it.

## The race test

The question "can two orders oversell the last item?" is answered by a run, not by reading. Copy `assets/race_template.py` with Read and Write to `.qa/work/database/test_race_<name>.py` and fill its three marked parts:

1. the migration directory, from the project root;
2. how one connection is opened through the project's own code (its store, repository or session class), so the test exercises the code under review, not a copy of it;
3. the state just before the race (for an oversell: one unit in stock) and the operation the two callers compete for.

The template opens the two connections in two threads and wraps the read so each caller waits after reading until the other has read too. Both then write. The test asserts the invariant (at most one sale of the last unit, stock never negative) and fails with a message that starts `lost update` when the code does not hold it. The database is `race.sqlite` beside the test, under `.qa/work/database/`.

Run it with `qa run` and its JUnit report (`references/tools.md`); it needs `project.trust` trusted from the user's words, since it imports the project. A failing run is the evidence of the lost update: the finding is `confirmed` and cites that EXE. A passing run means the code serialised the operation in that interleaving: say so, cite the EXE, and record no finding. If the run is `blocked`, the answer is "not shown": give the reading as a lead, `suspected`, and never as a yes the user can rely on.

When the code reads in one method and writes in another, wrap the method that reads (the template wraps `stock_of`). When the operation does not read first (a conditional `UPDATE … WHERE stock >= ?`), there is nothing to interleave: that design is the fix, and the test passes.

## The query trace

Write a driver with the Write tool at `.qa/work/database/trace_driver.py`: one function `case_<name>(db, measure)` per call to measure. Build the schema on `db` through the project's own code, add enough rows to show a pattern (several parents with children), then make the call inside `with measure():`. Run `query_trace.py` through `qa run` (`references/tools.md`). A statement repeated once per parent row, at one `path:line`, is the N+1 pattern; quote its count and site from the stored trace.

## Finding template

Write each finding with the Write tool under `.qa/work/database/`, then `python3 .claude/skills/qa-database/scripts/qa.py --role auditor --skill qa-database finding add --json .qa/work/database/<name>.json`. Placeholders are in angle brackets.

```json
{
  "title": "<table.column or migration step: what can go wrong, under 120 characters>",
  "description": "<what the schema or code allows, what a recorded check showed, or that only the static reading shows it>",
  "category": "<data-integrity | performance>",
  "severity_rationale": {"anchor": "<FUNC-C2 | FUNC-H2 | FUNC-M1 | FUNC-M2 | PERF-M1 | PERF-M2>", "modifiers": []},
  "component": {"path": "<file>", "criticality": "<critical|high|normal|low>"},
  "verification_status": "<confirmed only when the evidence holds the EXE of a run that showed it; suspected otherwise>",
  "evidence": ["<EXE of the run that showed it, when there is one>", "<path>:<line>"],
  "reproduction_steps": ["<the qa run command>", "<the test case in the JUnit report>"],
  "impact": "<the data that is lost, duplicated, orphaned or oversold, and when>",
  "probable_cause": "<path>:<line> <one sentence>",
  "recommendation": "<the constraint, the copy step, the down step, the transaction or conditional update; nothing was changed here>",
  "refs": {"rule": "<missing-foreign-key | missing-uniqueness | drop-without-copy | missing-down-step | failing-down-step | lost-update | n-plus-one | unindexed-foreign-key | not-null-without-value>", "location": "<path, no line number>"},
  "key_attributes": {"rule": "<the same rule>", "table": "<table>", "column": "<column or empty>"}
}
```

- `probable_cause` must cite a `path:line`, an `EV-` or an `EXE-` id, or the kernel refuses it. `refs.location` holds a path with no line number.
- `confirmed` needs the EXE of a run whose result shows the weakness in `evidence`. A lead from `schema_model.py` or from reading the code is `suspected`, and the description says no run showed it.
- One finding per weakness. If `finding add` refuses, read `error`, correct the JSON and retry once.
