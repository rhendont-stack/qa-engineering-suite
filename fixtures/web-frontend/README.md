# Corner Stationery

A small static web front end for a stationery shop: a shop page with a product list and a sign-up form, and an about page. Everything is
served from `site/`; nothing is loaded from another host.

## Running it

```
python3 server.py --host 127.0.0.1 --port 8080
```

The server accepts only a loopback host (`127.0.0.1` or `::1`) and exits with status 2 for any other. Then open `http://127.0.0.1:8080/`.

`GET /api/products` returns the product list. `--products` picks what it returns: `normal` (three products, the default), `empty`
(`[]`) or `error` (HTTP 500).

`server.dispatch(method, path)` is the whole server without sockets; it returns `(status, headers, body)`.

## Tests

```
python3 -m unittest discover tests
python3 tests/run_junit.py --out report.xml
```

The second command writes a JUnit XML report and exits 1 when a test fails.
