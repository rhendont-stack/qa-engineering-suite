# Release criteria

Whether a release may go is the release gate's verdict, never this skill's judgment and never a person's assurance. The
gate reads the ledger as it is: the findings, the recorded runs, the requirements, the imported bundles. This skill adds
its pipeline findings and its linter runs; the other criteria are other skills' evidence. Whatever the verdict says,
this skill deploys, tags, pushes and publishes nothing: a `READY` verdict is evidence for the person who releases, not a
release.

## Evaluating

`python3 <skill-dir>/scripts/qa.py --role auditor --skill qa-release gate evaluate --profile release`

Evaluate after the findings and the legs are recorded, so the verdict counts them. The reply holds `id` (`GATE-...`),
`verdict`, `criteria` (each with `id`, `blocking`, `outcome`, `detail`, `evidence`) and `conditions`. The exit code is the
verdict, not an error: 0 `READY`, 1 `NOT_READY`, 3 `INSUFFICIENT_EVIDENCE`, 6 `READY_WITH_CONDITIONS`. Read the JSON.

The verdict follows from the outcomes mechanically:

1. A blocking criterion `NOT_MET`: `NOT_READY`.
2. Otherwise a blocking criterion `NO_EVIDENCE`: `INSUFFICIENT_EVIDENCE`.
3. Otherwise an accepted risk, or a criterion that does not block `NOT_MET`: `READY_WITH_CONDITIONS`, with the conditions.
4. Otherwise `READY`.

A criterion is `MET`, `NOT_MET`, `NO_EVIDENCE` or `NOT_APPLICABLE` (an opt-in criterion the project has not opted into, or
an accepted override with a reason).

## The release profile's criteria

| Criterion | It needs | Where the evidence comes from |
|---|---|---|
| `no_unresolved_high` | No open finding at HIGH or above | Every skill's findings, this one's included; a fix and its revalidation (`qa-regression-guard`) resolve one |
| `requirements_verified` | The documented requirements, each verified by a linked test | `qa-requirements`, `qa-test-generation` |
| `suite_executed` | A full run of the suite, structured, passed, on a clean tree at the target commit | `qa-test-execution` |
| `no_blocked_critical` | No critical component whose tests are blocked or unrun | `qa-test-execution`, the traceability links |
| `flaky_rate_max` | A flaky ratio at most 2 % of the executed tests at the target commit | `qa-test-execution` (its flaky protocol) |
| `security_scans_executed` | Secrets, dependency and static analysis scans at the target commit | `qa-security` |
| `mutation_score_min` | Opt-in only: a mutation score of at least 70 | `qa-mutation` |
| `performance_objectives_met` | A load test at staging class or above that met its objectives | `qa-performance` |
| `migrations_validated` | A structured `db-ephemeral` run of the migrations at the target commit | `qa-database` |
| `rollback_validated` | A recorded rollback rehearsal. The suite never deploys, so the rehearsal runs in the project's own pipeline and its record arrives through `qa ingest` | The project's pipeline |
| `test_integrity` | The kernel and the recorded tests unaltered | The kernel |
| `independence_min` | Evidence at independence I2 or above | A second, independent review, or an imported bundle |

## What the answer says about the gate

- The verdict exactly as the kernel wrote it, in capitals, with the gate id: "The release gate's verdict is
  INSUFFICIENT_EVIDENCE (GATE-001)." Never a paraphrase, never a softer word, never your own estimate of readiness.
- Every blocking criterion whose outcome is `NOT_MET` or `NO_EVIDENCE`, each by its id, with the kernel's `detail`: none
  left out, however many there are.
- What each of those criteria needs, from the table above.
- That this skill deploys, tags and pushes nothing, whatever the verdict.

## A criterion that does not apply

When a file shows that a criterion does not apply to this project (no database and no migrations directory, so there are
no migrations to validate), propose an override with a rationale that cites what shows it:

`python3 <skill-dir>/scripts/qa.py --role auditor --skill qa-release gate policy propose migrations_validated --override '{"not_applicable": "The project has no database: no migrations directory, no database client in its dependencies"}' --rationale "No file in the project defines a schema or a migration (read: the whole tree)"`

- A proposal changes nothing. The verdict stays what the gate said, and the answer gives that verdict, not the one the
  proposal might give.
- Only `not_applicable`, with a reason a file shows. Never `enabled: false` or `blocking: false`, never a lower
  threshold, and never a proposal to make a release pass: a criterion that applies but has no evidence is
  `NO_EVIDENCE`, and the remedy is its evidence.
- Accepting a proposal is the user's decision, made with their own words; this skill never runs `gate policy accept`.
  Say that the proposal waits for them, by its event id.
