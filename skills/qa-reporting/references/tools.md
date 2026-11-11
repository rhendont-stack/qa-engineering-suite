# Tools

Nothing here is installed by this skill, and none of it runs the project's tests. A command that is refused is reported with the kernel's own words.

## Kernel commands used

All through `python3 .claude/skills/qa-reporting/scripts/qa.py --role evaluator --skill qa-reporting <command>`, written literally in each Bash call, one command per call, never through a shell variable. Every reply is one JSON object on stdout.

| Command | Use |
|---|---|
| `ledger verify` | Check every chain, event, evidence file and kernel file. `ok: false` with a `problems` list of `kind`, `where`, `detail` (kinds such as `modified`, `missing`, `invalid`). Exit 1 when there is a problem. Read-only. |
| `state rebuild` | Fold the ledger and write `.qa/state/`. Needed after the ledger changed under you. |
| `state show [KEY]` | The state, or one key of it: `executions`, `results`, `tests`, `findings`, `gates`, `reports`, `requirements`, `criteria`, `testcases`, `violations`. A violation is something the fold refused, such as an unknown reference. |
| `finding list` | Every finding with severity, priority, state and verification status. |
| `finding dedupe [--confirm KEEP:DUPLICATE]...` | Without `--confirm`, only proposes: pairs with the same file, the same CWE and overlapping lines, each pair as `KEEP:DUPLICATE`, the kept finding first; it may hold the higher ID. At most 200. With it, links the named proposed pairs: the second finding becomes a `duplicate` of the first, and the first keeps both evidence lists and both sources. A pair that is not proposed is refused (`not_a_duplicate`); a pair already linked does nothing. A pair that would make a chain, or whose second finding is not open, is listed under `refused` with a reason and records nothing; confirm the pairs one level deep, against the root finding. |
| `plan status [PLAN]` | A plan's steps against the ledger: executed, blocked, not run. |
| `ingest bundle FILE` | Import a CI evidence bundle. It is data; the import is refused if it does not verify. |
| `compare BASE HEAD` | Test-by-test difference between two recorded runs: new failures, fixes, appeared, disappeared, flaky. Comparing a local run with an imported bundle run is how independent re-execution is shown. |
| `gate evaluate [--profile P] [--target COMMIT]` | Evaluate the profile's criteria against the ledger and record the verdict. Reply: `verdict`, `profile`, `criteria`, `conditions`, `id` (`GATE-`). Exit codes: 0 `READY`, 1 `NOT_READY`, 3 `INSUFFICIENT_EVIDENCE`, 6 `READY_WITH_CONDITIONS`. These are verdicts, not failures of the command. |
| `trace matrix [--target COMMIT]` | Compute the traceability matrix for one commit (no target: the current HEAD, the gate's default) and write `.qa/trace/matrix.json`: `target`, `requirements` (each with its criteria and test cases), `testcases`, `gaps` (each a `kind` and an `id`) and `counts`. Records no ledger event; the same state and target give the same bytes. Gap kinds, in the order it lists them: `req_without_ac`, `ac_without_tc`, `tc_not_automated`, `tc_never_executed`, `tc_last_blocked_or_inconclusive`, `req_only_characterization_or_assumed`, `tc_unproven`, `fnd_without_fix`, `fix_without_rvl`, `rvl_off_target`. Every one goes in the answer. |
| `report render [--format md\|json\|sarif\|executive]...` | Write `.qa/reports/RPT-n/`. Default: `report.md` and `report.json`. `executive` adds `executive.md`, the one-page summary: verdict, counts, recommendations ordered by priority, and the gate follow-up. `sarif` adds `report.sarif`. Same state and same latest gate give the same bytes. Render verifies the ledger first: if that finds a problem, the report opens with an integrity failure and shows the recorded verdict as not current. The executive page lists at most 10 recommendations and counts the rest. |
| `report check FILE` | Every claim in a Markdown report must cite a record that exists: reports `unknown_id`, `uncited_claim`, `status_mismatch` and `universal_claim` problems, and a `ledger_integrity` problem when ledger verification fails at that moment. Exit 1 when there are any. |
| `report bundle --output PATH` | Pack the ledger, evidence and reports as one portable archive. The path must not exist. |
| `doctor` | The kernel, ledger, configuration and the machine's tools. Use when something does not behave. |

## Reading a gate criterion

| Outcome | Meaning |
|---|---|
| `MET` | The recorded evidence satisfies it. |
| `NOT_MET` | The evidence contradicts it. A blocking one makes the verdict `NOT_READY`. |
| `NO_EVIDENCE` | Nothing recorded can decide it. A blocking one with no `NOT_MET` makes the verdict `INSUFFICIENT_EVIDENCE`. |
| `NOT_APPLICABLE` | Excluded with a recorded justification. Never assume it. |

The verdict follows mechanically: a blocking `NOT_MET` gives `NOT_READY`; otherwise a blocking `NO_EVIDENCE` gives `INSUFFICIENT_EVIDENCE`; otherwise an advisory `NOT_MET` or an accepted risk in scope gives `READY_WITH_CONDITIONS`; otherwise `READY`. The `test_integrity` criterion is `NOT_MET` when `ledger verify` finds a problem, which is why an integrity failure always blocks.

## Reading the rendered files

`report.md` begins with the title, then, only when there is an integrity failure, a line starting `**Integrity failure:**`, then the line starting `**Verdict:**` with the verdict, the `GATE-` ID, the profile, the independence level and the state hash. Sections: Gate, Executions, Findings, Integrity, Limitations. The Limitations section always holds the kernel's fixed statements of what it cannot do; add nothing to the file by hand.

## Project tools

None. If a user asks for a conversion the kernel does not do (for example an HTML page from the Markdown), say it is outside this skill and give them the rendered files.
