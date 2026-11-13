# Workload model

A workload says who sends what, how often, for how long, and what counts as holding up. Write it as JSON under `.qa/work/performance/<name>.json` with the Write tool, then turn it into a script with `k6_script.py` (`references/tools.md`). The workload holds paths, never an address: the run gives the base URL.

## The fields

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Lower-case letters, digits and hyphens; also the script's name |
| `model` | yes | `closed` or `open` (below) |
| `vus` | yes | Virtual users: the closed model's users, or the most the open model may use |
| `duration_s` | yes | Seconds of load. The grant's `max_duration_s` must be at least `duration_s + 5` |
| `think_time_s` | closed only | Seconds each user waits after a response, above 0 |
| `rate_per_s` | open only | Requests started each second, whatever the responses do |
| `request_timeout_s` | no | Seconds before a request counts as failed; default 10 |
| `requests` | yes | One to twenty entries: `name`, `method`, `path` (starting with one `/`), `headers` (no `Host`), `expect` (the status that counts as a success), `weight` (how often, relative to the others; default 1), `body` (a string or a JSON value; none for GET) |
| `thresholds` | yes | The objectives: `{metric: [expression, …]}`. Each becomes `{threshold, abortOnFail: true, delayAbortEval: '5s'}` |

## Closed or open

- **Closed** (`constant-vus`): `vus` users each send a request, wait for the answer, wait `think_time_s`, and go again. The load follows the service: when it slows down, fewer requests are sent. Use it for "N users at once", which is what most requests mean by users. The most requests a second it can send is `vus / think_time_s`; that must not pass the grant's `max_rps`.
- **Open** (`constant-arrival-rate`): `rate_per_s` requests start every second, from up to `vus` users, whatever the answers do. The load does not ease when the service slows down, so queues show. Use it for "R requests a second", an arrival rate from production traffic, or when the closed model would hide a slowdown. `rate_per_s` must not pass `max_rps`; when every user is busy, k6 drops iterations (`dropped_iterations`), which is itself a result.

Users in a request ("20 users") are VUs of a closed model unless the user speaks of a rate. If they give neither think time nor rate, propose one (an assumption, `asm add`) and say which you used.

## Objectives as thresholds

| Objective | Threshold |
|---|---|
| 95% of requests under 200 ms | `"http_req_duration": ["p(95)<200"]` |
| one endpoint's p99 under 500 ms | `"http_req_duration{name:list orders}": ["p(99)<500"]` (the request's `name`) |
| fewer than 1% failed requests | `"http_req_failed": ["rate<0.01"]` |
| at least 50 requests a second served | `"http_reqs": ["rate>=50"]` |
| every response the expected status | `"checks": ["rate==1"]` |

Durations are milliseconds; `http_req_failed` counts responses outside 200–399 and transport errors. Thresholds are the run's tests: the run is `failed` when one is crossed. An objective the user did not state is never a threshold of yours without an open assumption that proposes it.

## Choosing the numbers

- Users, rate and duration come from the user's objective or their grant. The grant's limits are the ceiling, never a target.
- A run shorter than about 30 seconds measures start-up more than the steady state. Two runs, one after the other, show the noise; a difference smaller than the gap between them is not a difference.
- The data volume changes the result (a listing over 10 orders and over 10,000 are different tests). Start the service with the volume the objective is about, through its own options (for example `--preload N`), and say which volume you used.
- A run on this machine against a local copy is `simulated` for any question about the real deployment (`references/labels.md`).

## The leg on fixture F2

The workload the suite's own k6 leg runs on the orders API (5 users for 30 seconds, `--max-vus 5 --max-duration-s 60 --max-rps 100`), with the service started with `--preload 300`:

```qa-workload f2-orders-listing
{
  "name": "f2-orders-listing",
  "model": "closed",
  "vus": 5,
  "duration_s": 30,
  "think_time_s": 0.2,
  "requests": [
    {"name": "list orders", "method": "GET", "path": "/orders", "headers": {"Authorization": "Bearer user-a-token"}, "expect": 200, "weight": 1}
  ],
  "thresholds": {
    "http_req_duration": ["p(95)<200"],
    "http_req_failed": ["rate<0.01"]
  }
}
```
