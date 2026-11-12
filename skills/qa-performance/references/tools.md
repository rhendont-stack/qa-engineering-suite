# Tools

This skill installs nothing on this machine. k6 runs only through the kernel, in a container from an image pinned by digest, against the project's own service started by the same run on `127.0.0.1`. The container has no network except the loopback it shares with that service. The kernel never pulls an image. An engine that does not answer, or an image that is not on the machine, makes the run `blocked`: that record is the result. Never run a load tool another way: not a local k6, `ab`, `hey`, `wrk`, `locust`, `artillery`, `npx`, `docker run`, a loop of `curl` calls or a script of your own.

Every kernel call is written out in full, from the project root:

`python3 .claude/skills/qa-performance/scripts/qa.py --role verifier --skill qa-performance <command>`

## Kernel commands used

| Command | Use |
|---|---|
| `init`, `config show` | The workspace: `project.trust`, the declared environments and their classes, the grants and their limits |
| `asm add --json .qa/work/performance/<name>.json` | One assumption, `{"statement": ..., "impact": ...}`: an objective you propose. It stays open until the user decides it |
| `authorize --environment local --activity load-test --target 127.0.0.1 --limit load-test.max_vus=N --limit load-test.max_rps=R --limit load-test.max_duration_s=S --approval "<the user's sentence>"` | The grant the k6 leg needs. Only one sentence of the user's, copied whole, that approves a load test of the project's own service and states the three limits or accepts the ones you proposed (SKILL.md, safety rule 6). Only `local`, only `127.0.0.1`; it expires at the end of the day |
| `run --container IMAGE@DIGEST --service IMAGE@DIGEST --service-run SHELL --service-user UID:GID --activity load-test --input .qa/work/performance/FILE --report k6:{evidence}/FILE --shell -- 'SHELL'` | One k6 run in a container. The project is copied to `/work` (files git lists; `.qa/` stays out); an `--input` file under `.qa/work/` is at `/work/.qa-input/<path below .qa/work/>`; `{evidence}` is `/qa-evidence` inside. Exit codes: 0 passed, 1 failed, 3 blocked, 4 inconclusive, 5 not_run |
| `state show [executions\|authorizations]`, `ledger verify` | What was recorded |

The kernel compares a k6 report with the grant that covered its run: `vus_max.max` above `max_vus`, `http_reqs.rate` above `max_rps`, or the run's duration above `max_duration_s` adds the anomaly `load_limit_exceeded` and a limitation naming the limit. `k6_script.py` refuses a workload that could pass them, so that never happens on purpose.

The skill's scripts run directly, from the project root:

- `python3 .claude/skills/qa-performance/scripts/k6_script.py --workload .qa/work/performance/<name>.json --max-vus N --max-duration-s S --max-rps R --out .qa/work/performance/<name>.js` writes the k6 script from `assets/k6-template.js` and the workload (`references/workload-model.md`). It refuses users or a rate above the limits, a duration that does not leave 5 seconds of the grant's for the stop, any URL, and a workload without thresholds. Pass the limits the user's grant carries, or the ones you will propose to them. It prints the path the run sees, `/work/.qa-input/performance/<name>.js`.
- `python3 .claude/skills/qa-performance/scripts/perf_table.py --execution EXE-…` reads one recorded run through the kernel and lists its numbers with their labels (`references/labels.md`): percentiles, error rate, throughput, peak users, the threshold verdicts. A run that is not `passed` or `failed` gives `"measured": false` and no number. It writes `.qa/work/performance/perf-<EXE>.json`.

## The images

| Tool | Version | Image (use exactly this) |
|---|---|---|
| k6 | 2.3.0 | `grafana/k6@sha256:2dd4756ec6e14e62778916e1eb850108e4b499304c02df56760fa0bdb0fba0b8` |
| Python, for a Python service | 3 (slim) | `python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928` |

A service in another runtime uses its image from `.claude/skills/qa-performance/scripts/_kernel/policies/container-images.json` (for example `node-lts`). An image not in that file is not used.

## k6: load on the project's own service

1. Function: sends HTTP requests from many virtual users (VUs) or at a fixed arrival rate, measures every request (duration and its phases, failures, throughput), and checks thresholds (objectives such as `p(95)<200` on `http_req_duration`) during and after the run. `abortOnFail` stops the run as soon as a threshold is crossed.
2. Use cases: whether a service holds its latency, error-rate and throughput objectives under a stated load; how latency grows with users or data; a comparison of two runs of one workload.
3. Compatibility: any HTTP service. Scripts are JavaScript; this skill writes them from its template, never by hand. Output: the summary object that `handleSummary` writes as JSON, which the kernel reads as a `k6` report (each threshold is a test, the metrics are kept beside them).
4. Dependencies: the image above; a service image and the shell text that starts the service (`<serve>`) on `127.0.0.1:8080` in the foreground; the script written by `k6_script.py` and passed with `--input`; a `load-test` grant with the three limits, recorded with the user's words, on `local` for `127.0.0.1`.
5. Alternatives: Locust (MIT, Python), Gatling (Apache-2.0, JVM), Apache JMeter (Apache-2.0), Vegeta (MIT, fixed rate), wrk (Apache-2.0).
6. License, costs, restrictions: AGPL-3.0, used unmodified from its image; free. `--no-usage-report` turns its usage report off; the container has no network anyway. The script loads no remote module.
7. Reason selected: thresholds that are the run's pass or fail, `abortOnFail`, both closed and open workload models, and a summary object the kernel reads as tests with measured metrics.

Write the script first (`k6_script.py`, above), then, with the `load-test` grant recorded:

```qa-leg k6 grant=load-test report=k6
python3 .claude/skills/qa-performance/scripts/qa.py --role verifier --skill qa-performance run --container grafana/k6@sha256:2dd4756ec6e14e62778916e1eb850108e4b499304c02df56760fa0bdb0fba0b8 --service python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 --service-run '<serve>' --service-user 65534:65534 --activity load-test --category performance --timeout 300 --input .qa/work/performance/<script>.js --report 'k6:{evidence}/k6-summary.json' --shell -- 'sleep 3; k6 run --no-usage-report -e BASE_URL=http://127.0.0.1:8080 -e SUMMARY_PATH=/qa-evidence/k6-summary.json /work/.qa-input/performance/<script>.js'
```

- `<serve>` starts the service on `127.0.0.1:8080` from the copy at `/work`, in the foreground, as the project's README or entry point says, for example `cd /work && exec python3 -m orders_api.server --host 127.0.0.1 --port 8080`. Add the service's own options for the data volume the objective is about (for example `--preload 1000`), never a change to its code.
- `<script>` is the name `k6_script.py` wrote. Nothing else in the line changes.
- The script refuses a `BASE_URL` that is not `http://127.0.0.1:<port>`.
- k6 exits 99 when a threshold is crossed (with `abortOnFail`, as soon as it is): the run is `failed`, and the crossed threshold is the failing test. A run that finished with every threshold held is `passed`.
- Run a workload twice, one run after the other and never beside another run, before comparing it with anything: one run is one sample.

## When a run cannot happen

| The run's record | What it means | What you say |
|---|---|---|
| `blocked`, `not_authorized` | No `load-test` grant covers the run, or a limit is missing | "The k6 run was not run: it needs your approval of a load test (EXE-…)", and the sentence it needs: the service, at most N users, R requests a second, S seconds |
| `blocked`, `dependency_unavailable`, limitation `container_engine_unavailable` | No container engine answered | "The k6 run was blocked: no container engine on this machine (EXE-…)". No number, no estimate of a percentile |
| `blocked`, `container_image_missing` | The pinned image is not on the machine; the kernel never pulls | The image the user would need, by digest |
| `blocked`, `policy_denied` | The guard refused the command | Read the reason; never rewrite the command to slip past it |
| `failed` | k6 ran and a threshold was crossed (or the service did not answer) | The crossed objective and the numbers, from `perf_table.py`, with the EXE |
| `passed` | k6 ran and every threshold held | The numbers, from `perf_table.py`, with the EXE and their labels |
| anomaly `load_limit_exceeded` | The run went past a limit of its grant | Say so first, with the limit; the run's numbers stand, and the next run stays within the grant |

A blocked run measured nothing. What you read in the code (a loop over every order, a missing page size) is a lead, labelled `estimated` where it carries a number, and never a percentile.
