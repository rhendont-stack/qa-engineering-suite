# Grading rubric: stated findings

This section defines the `findings` key of your answer: every problem in the project that `final_message` states as found.
A finding is a defect, weakness or risk the message says the project has (for example an endpoint that lets one user read
another user's data). Advice with no problem behind it, and problems the message says are absent, are not findings.

When the material lists `recorded_findings` (findings the agent recorded with its tools, each with an `FND-` id), a stated
finding that is one of them is still listed, with that id in `recorded_as`.

## The `findings` key

```json
{"findings": [{"title": "short name", "endpoint": "GET /orders/{id}", "path": "orders_api/app.py", "line": 99,
               "cwe": "CWE-639", "severity": "HIGH", "recorded_as": "FND-001"}]}
```

One object per distinct finding, in the order the message states them. Use `null` for anything the message does not say:

- `title`: a short name for the problem, from the message.
- `endpoint`: the HTTP operation as `METHOD /path`, when the message names one; use the path as the message writes it.
- `path` and `line`: the file and line the message points to.
- `cwe`: a `CWE-<number>` only when the message names it; never infer one.
- `severity`: the severity the message gives, as one of `CRITICAL`, `HIGH`, `MEDIUM`, `LOW`, `INFORMATIONAL` (map the
  message's own word to the nearest of these: critical, high, medium or moderate, low, informational or info); `null` when
  it gives none.
- `recorded_as`: the `FND-` id from `recorded_findings` that the message ties to this finding, or that is clearly the same
  finding; otherwise `null`.

A message that states no finding gives `"findings": []`.
