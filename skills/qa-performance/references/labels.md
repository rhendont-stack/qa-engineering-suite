# Labels

Every number in an answer about performance carries one of four labels, in the same sentence or table row. A number without a label is read as a measurement of the real thing, which it almost never is.

| Label | What it is | Where it comes from | How to write it |
|---|---|---|---|
| `measured` | Read from the report of a recorded run whose status is `passed` or `failed`, in an environment of class `staging` or `production` | `perf_table.py --execution EXE-…` | "p(95) 143 ms (measured, EXE-…)" |
| `simulated` | Read from the report of a recorded run in a class below `staging` (`local`, `ci`, `ephemeral`, `shared-test`): this copy on this machine, standing in for the real deployment | `perf_table.py`, which labels it so | "p(95) 143 ms on the local copy with 300 orders (simulated, EXE-…); production may differ" |
| `derived` | Computed from labelled numbers, with the formula | `perf_table.py`'s derived rows, or your arithmetic on rows you cite | "41.2 successful requests a second (derived: rate times one minus the error rate, EXE-…)" |
| `estimated` | Reasoned or modelled, not observed: a count from the code, a capacity from a formula | your reading, said as such | "each listing copies the store once per order it returns, about 150 copies at 300 orders (estimated from orders_api/app.py:73-80)" |

## Rules

- **No percentile without a run.** A percentile (`p(95)`, `p99`, the median) appears only as a row of a `passed` or `failed` run's report, with that run's EXE in the same sentence. Not estimated, not "expected", not "should be around", not from another project, not from memory. With no such run, write that the percentile is unknown and why.
- **An objective is not a number of the system.** Write it as a target, with its word: "p95 under 200 ms (objective)", "error rate under 1% (assumed objective, ASM-…)". It needs no label of the four, and it never stands where a result would.
- **A blocked run has no numbers.** Its record proves it did not run. Say what it would have measured and what it needs.
- **A comparison needs two labelled numbers of one kind.** Measured against measured, simulated against simulated, from runs of one workload. A difference smaller than the gap between two repeats of one run is noise.
- **Simulated is not a weaker word for measured.** It says what the number stands in for. "The local copy held p(95) under 200 ms at 20 users (simulated, EXE-…)" says nothing yet about production at 20 users.
- **Derived numbers name their inputs.** "Throughput 38 requests a second" is `measured` or `simulated` when it is k6's `http_reqs.rate`, `derived` when you computed it.
- **The workload's own numbers are simulated load.** Users, a rate, seconds, think time and the data volume you set are what k6 will send, not results: k6's virtual users are simulated users. Write them once, on the answer's `Workload` line, as "(simulated load, not a measurement)", and keep them off the `Objectives` line unless they are part of an objective ("p95 under 200 ms at 20 users").
- **The record's `measured` is the kernel's word, not the answer's.** The kernel's `report.label` on an execution is `measured` whenever the run's report carries metrics, whatever the environment's class. The answer uses `perf_table.py`'s label, which reads the class: a run on `local` is `simulated`.
- **An attested summary is no run's number.** A k6 summary attached to a run after it, which the run did not produce (`perf_table.py` lists it under `attested`), gives no row and no label. Only the run's own report, or a summary its evidence manifest lists (`structured`), does.

## Verdicts

A verdict ("holds the objective", "does not") needs both an objective the user stated or accepted and a `passed` or `failed` run that measured it. Without objectives, propose them as assumptions and ask; without a run, say it did not run and why. "Fast", "slow", "fine", "fast enough", "yes" and "no" about speed are verdicts too: never give one without both.
