# Orders API

A small orders API on the Python standard library. `openapi.yaml` is its contract, written in the JSON-compatible subset of YAML.

## Running it

```
python -m orders_api.server --host 127.0.0.1 --port 8080
```

The server accepts only a loopback host (`127.0.0.1` or `::1`) and exits with status 2 for any other. `GET /health` returns 200 when it is ready.

The collection can start with orders already in it, for a run that needs a large one: `--preload N` or the environment variable
`ORDERS_API_PRELOAD=N` (default 0). Preloaded orders alternate between the two users.

`orders_api.app.dispatch(method, path, headers, body)` is the whole API without sockets; it returns `(status, headers, body)`.
`orders_api.app.reset(preload=0)` gives it a fresh store.

## Users

Both are fake and exist only for tests.

| User | Password | Bearer token |
| --- | --- | --- |
| `user-a` | `password-a` | `user-a-token` |
| `user-b` | `password-b` | `user-b-token` |

`POST /auth/token` exchanges a user name and password for the token. Send it as `Authorization: Bearer <token>`.

## Tests

```
python -m pytest
```
