# Checks

Your own checks are pytest files you write under `.qa/work/api/` with the Write tool and run through the kernel. They call the API in-process, so they need no container, no port and no network, and they run even when every external tool is `blocked`. They are your evidence, recorded under their own execution: never report them as Spectral, Schemathesis or Hurl results.

## Calling the API in-process

Read the code to find the entry point, then call it directly:

| The code exposes | Call it with |
|---|---|
| A dispatch function (`(method, path, headers, body) -> (status, headers, body)`) | The function itself |
| A WSGI app (Flask, Django, Bottle) | `app.test_client()` (Flask), `django.test.Client`, or a WSGI environ built with `wsgiref.util.setup_testing_defaults` |
| An ASGI app (FastAPI, Starlette) | `fastapi.testclient.TestClient(app)` when `httpx` is installed; otherwise the run is `blocked` and says so |
| Express or another Node server | Not in-process from Python: use Hurl in a container (`references/tools.md`) |

If the app keeps state in memory, reset it in a fixture before each test, through whatever reset the code itself provides. Never edit the project to make it testable.

Template (placeholders in angle brackets; one test per documented operation and status, named after the operation):

```python
"""Contract checks for <contract path>, written by qa-api-testing. Each test names the operation it checks."""
import json

import pytest

from <package>.<module> import <entry point>      # the project root is on sys.path when pytest runs from it


def call(method, path, token=None, body=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    raw = json.dumps(body).encode() if body is not None else b""
    status, _headers, payload = <entry point>(method, path, headers, raw)
    return status, (json.loads(payload) if payload else None)


@pytest.fixture(autouse=True)
def fresh_state():
    <the code's own reset, if it has one>


def test_post_items_answers_a_documented_status():
    status, _ = call("POST", "/items", token="<test token>", body={<a valid body from the contract's schema or example>})
    assert status in (<the documented codes>), "documented: <codes>; got %d" % status
```

Run it, then mark the inventory from the result:

- `python3 .claude/skills/qa-api-testing/scripts/qa.py --role verifier --skill qa-api-testing run --report junit:{evidence}/junit.xml -- python3 -B -m pytest -p no:cacheprovider .qa/work/api/test_contract.py --junitxml={evidence}/junit.xml`
- `python3 .claude/skills/qa-api-testing/scripts/endpoint_inventory.py mark --operation "POST /items" --operation "GET /items/{id}" --status verified --execution EXE-…`

`verified` means a recorded execution checked the operation, whatever the outcome: a failing check is a verified operation with a finding. An operation no check reached stays `not_run` with the reason; one whose only check could not run is `blocked` with the reason.

The assertion is what the contract says, not what the code does. A test that fails is the result: do not change the expected status to make it pass.

## What to check, per operation

1. Status: every response the code can give is one the operation documents (the inventory's `status_mismatch` lists the static candidates; a check confirms each).
2. Body: a documented success response has the documented shape (required fields, types). An error response matches the documented error schema.
3. Undocumented operations: the inventory's `undocumented` list. Call each once, without and with a test token, and record what it answers. An undocumented operation that answers with data is a finding, and a security one when it skips authorization.
4. Unimplemented operations: documented, no route found; one call confirms (usually 404 or 405).

## Role matrix (authorization as documented)

Build the matrix from the contract's `security` per operation and from the roles the project's docs or fixtures name. For each protected operation:

| Caller | Expected |
|---|---|
| No credentials | 401 |
| A malformed or unknown token | 401 |
| A valid user on their own object | the documented success status |
| A valid user on another user's object | 403 or 404, never the object (BOLA) |
| A non-admin on an admin-only operation | 403 |

Each cell is one test. A cell that answers otherwise is a `security` finding with the request, the response status and the execution. Only test users and tokens the project itself defines for tests are used.

## Bounded rate-limit probe

Only when the contract or the docs state a limit, and only in-process or against the loopback service of a container run, never against a shared or external host. Send at most limit + 5 requests to one operation, with no concurrency, and check that the documented answer (usually 429 with `Retry-After`) appears after the limit. Without a documented limit, do not probe: say that no limit is documented. A missing limit on an authentication operation is worth a finding (`SEC-M1`).

## Idempotency

- `PUT` and `DELETE`: repeat the same request; the second answer is the documented one (the same state for `PUT`; 204 or 404 for `DELETE`, as documented), and the stored state is unchanged by the repeat.
- `POST` with an idempotency key (when the contract defines the header): the same key twice creates one resource.
- `GET`, `HEAD`, `OPTIONS`: never change state; read the state before and after.

## Pagination

When the contract documents `limit`/`offset`, `page`/`size` or a cursor: the first page, the last page, an empty page past the end, `limit=0`, a negative or non-numeric value (documented 400 or 422, never 500), and a limit above the documented maximum (capped or refused, as documented). Count that the pages together return each item once.

## Webhooks and callbacks

When the contract has `webhooks` or `callbacks`: check the payload the code would send against the documented schema, in-process, by calling the function that builds it. Never send a webhook to a URL outside the machine. A receiver endpoint is checked like any other operation (signature header missing or wrong: the documented refusal).

## Dependency failures

When the code calls a database, a queue or another service: if the code offers a seam (an injected client, a setting), make that dependency fail in the test (raise, time out) and check the API answers with a documented status, usually 503 or 500 with the documented error body, and without a stack trace or internal detail in the body. A stack trace in a response body is a `security` finding (`SEC-M1`). Never stop or break a real shared dependency.

## Finding template

Write each finding with the Write tool under `.qa/work/api/`, then `python3 .claude/skills/qa-api-testing/scripts/qa.py --role verifier --skill qa-api-testing finding add --json .qa/work/api/<name>.json`. Placeholders are in angle brackets.

```json
{
  "title": "<METHOD /path: what differs from the contract, under 120 characters>",
  "description": "<what the contract says, what the API did, in which check>",
  "category": "<functional for a contract mismatch; security for authorization or exposed internals>",
  "severity_rationale": {"anchor": "<FUNC-M1 | FUNC-M2 | FUNC-H1 | SEC-H1 | SEC-M1 | SEC-L1 | FUNC-L1>", "modifiers": []},
  "component": {"path": "<source file>", "endpoint": "<METHOD /path>", "criticality": "<critical|high|normal|low>"},
  "verification_status": "<confirmed when a recorded check showed it; suspected when only the inventory's scan shows it>",
  "evidence": ["<EXE of the check run>", "<path>:<line>"],
  "reproduction_steps": ["<the qa run command>", "<the test id in the JUnit report>"],
  "impact": "<what a client relying on the contract gets wrong>",
  "probable_cause": "<path>:<line> <one sentence>",
  "recommendation": "<fix the code or the contract; nothing was changed here>",
  "refs": {"rule": "<status-mismatch | undocumented-endpoint | unimplemented-endpoint | schema-mismatch | missing-authorization | bola | stack-trace-exposure | method-mismatch>", "location": "<path, no line number>"},
  "key_attributes": {"rule": "<the same rule>", "operation": "<METHOD /path>"}
}
```

`probable_cause` must cite a `path:line`, an `EV-` or an `EXE-` id, or the kernel refuses it. If `finding add` refuses, read `error`, correct the JSON and retry once.
