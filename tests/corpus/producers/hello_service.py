"""The corpus's fixture service: a standard-library HTTP server on loopback, for the producers that need a target.

Usage: python3 hello_service.py PORT

It runs only as the ``--service`` of a ``qa run --container`` (network none), never on the host. Routes:

- ``GET /``: a small HTML page that links to ``/hello``, for a spider to find, with a cookie set without the HttpOnly
  and SameSite attributes, so a passive scanner has a finding whose evidence is response text;
- ``GET /hello``: ``{"message": "hello"}``, as the sample OpenAPI documents promise;
- ``GET /broken``: a 500 with a plain-text body, which the same documents do not allow, so a schema-driven
  tool has one failing operation to report;
- any other path: 404; any other method: 405.
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PAGE = b"<!doctype html><html><head><title>hello</title></head><body><a href=\"/hello\">hello</a></body></html>\n"


class Hello(BaseHTTPRequestHandler):
    server_version = "hello"
    sys_version = ""

    def _send(self, code: int, media_type: str, body: bytes, cookie: bool = False) -> None:
        self.send_response(code)
        if cookie:
            self.send_header("Set-Cookie", "corpus=hello; Path=/")
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (the name http.server calls)
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE, cookie=True)
        elif path == "/hello":
            self._send(200, "application/json", json.dumps({"message": "hello"}).encode("utf-8"))
        elif path == "/broken":
            self._send(500, "text/plain", b"broken on purpose\n")
        else:
            self._send(404, "text/plain", b"not found\n")

    do_HEAD = do_GET  # noqa: N815

    def _not_allowed(self) -> None:
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def __getattr__(self, name: str):
        """Every other method (http.server looks up ``do_<METHOD>``) is answered 405, as HTTP asks, not 501."""
        if name.startswith("do_"):
            return self._not_allowed
        raise AttributeError(name)


def main(argv: list) -> int:
    if len(argv) != 2 or not argv[1].isdigit():
        print("usage: hello_service.py PORT", file=sys.stderr)
        return 2
    HTTPServer(("127.0.0.1", int(argv[1])), Hello).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
