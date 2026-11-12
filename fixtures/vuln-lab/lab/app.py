"""The catalogue lab, served on a loopback address: ``python lab/app.py --host 127.0.0.1 --port 8080``.

It binds only 127.0.0.1 or ::1 and replies 421 to any request whose Host header is not a loopback name.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from string import Template
from typing import Callable, Dict, Tuple
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import catalog  # noqa: E402
import settings  # noqa: E402
import tokens  # noqa: E402

LOOPBACK = ("127.0.0.1", "::1")
LOOPBACK_NAMES = ("127.0.0.1", "::1", "localhost")
NOTES = os.path.join(HERE, "notes")
TEMPLATES = os.path.join(HERE, "templates")
HTML = "text/html; charset=utf-8"
JSON = "application/json"
TEXT = "text/plain; charset=utf-8"
NOTE_LIMIT = 1 << 16

Reply = Tuple[int, Dict[str, str], bytes]


def reply(status: int, content_type: str, body: bytes, request: Dict[str, str], extra: Dict[str, str] = None) -> Reply:
    headers = {"Content-Type": content_type}
    origin = request.get("origin")
    if origin:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Access-Control-Allow-Credentials"] = "true"
    headers.update(extra or {})
    return status, headers, body


def as_json(document: object) -> bytes:
    return json.dumps(document, sort_keys=True).encode("utf-8")


def page(template: str, **values: str) -> bytes:
    with open(os.path.join(TEMPLATES, template), encoding="utf-8") as handle:
        return Template(handle.read()).substitute(values).encode("utf-8")


def index(query: Dict[str, str], request: Dict[str, str]) -> Reply:
    return reply(200, HTML, page("index.html"), request)


def hello(query: Dict[str, str], request: Dict[str, str]) -> Reply:
    return reply(200, HTML, page("hello.html", name=query.get("name", "visitor")), request)


def products(query: Dict[str, str], request: Dict[str, str]) -> Reply:
    found = catalog.find(query.get("category", ""), int(query.get("limit", "10")))
    return reply(200, JSON, as_json({"items": found}), request)


def notes(query: Dict[str, str], request: Dict[str, str]) -> Reply:
    path = os.path.join(NOTES, query.get("name", "welcome.txt"))
    if not os.path.isfile(path):
        return reply(404, JSON, as_json({"error": "not found"}), request)
    with open(path, "rb") as handle:
        return reply(200, TEXT, handle.read(NOTE_LIMIT), request)


def session(query: Dict[str, str], request: Dict[str, str]) -> Reply:
    visitor = "visitor-" + secrets.token_hex(8)
    cookie = "%s=%s; Path=/; SameSite=Lax" % (settings.SESSION_COOKIE, tokens.issue(visitor))
    return reply(200, JSON, as_json({"visitor": visitor}), request, {"Set-Cookie": cookie})


def presented_token(request: Dict[str, str]) -> str:
    bearer = request.get("authorization", "")
    if bearer.startswith("Bearer "):
        return bearer[len("Bearer "):].strip()
    for part in request.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == settings.SESSION_COOKIE:
            return value
    return ""


def account(query: Dict[str, str], request: Dict[str, str]) -> Reply:
    claims = tokens.verify(presented_token(request))
    if claims is None:
        return reply(401, JSON, as_json({"error": "sign in first"}), request)
    return reply(200, JSON, as_json({"sub": claims.get("sub")}), request)


ROUTES: Dict[Tuple[str, str], Callable[[Dict[str, str], Dict[str, str]], Reply]] = {
    ("GET", "/"): index,
    ("GET", "/hello"): hello,
    ("GET", "/products"): products,
    ("GET", "/notes"): notes,
    ("GET", "/session"): session,
    ("GET", "/account"): account,
}
PATHS = {path for _method, path in ROUTES}


def loopback_host(value: str) -> bool:
    """Whether a Host header names a loopback host, with or without a port."""
    text = value.strip().lower()
    if text.startswith("["):
        name, bracket, rest = text[1:].partition("]")
        if not bracket:
            return False
    else:
        name, colon, rest = text.partition(":")
        rest = colon + rest
    return name in LOOPBACK_NAMES and (rest == "" or (rest[:1] == ":" and rest[1:].isdigit()))


def dispatch(method: str, target: str, headers: Dict[str, str]) -> Reply:
    request = {str(name).lower(): str(value) for name, value in headers.items()}
    if not loopback_host(request.get("host", "")):
        return 421, {"Content-Type": TEXT}, b"misdirected request: this server serves only a loopback host name\n"
    parts = urlsplit(target)
    handler = ROUTES.get((method, parts.path))
    if handler is None:
        status = 405 if parts.path in PATHS else 404
        return reply(status, JSON, as_json({"error": "not found" if status == 404 else "method not allowed"}), request)
    query = {name: values[0] for name, values in parse_qs(parts.query).items()}
    try:
        return handler(query, request)
    except Exception:
        detail = traceback.format_exc() if settings.DEBUG else "internal error\n"
        return reply(500, TEXT, detail.encode("utf-8"), request)


class Handler(BaseHTTPRequestHandler):
    def version_string(self) -> str:
        return "lab"

    def serve(self) -> None:
        status, headers, body = dispatch(self.command, self.path, dict(self.headers.items()))
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def __getattr__(self, name: str) -> Callable[[], None]:
        if name.startswith("do_"):          # every method goes through dispatch, and so through the Host check
            return self.serve
        raise AttributeError(name)

    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write("%s %s\n" % (self.command, format % args))


class Server(ThreadingHTTPServer):
    daemon_threads = True


class Server6(Server):
    address_family = socket.AF_INET6


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="The catalogue lab on a loopback address.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    if args.host not in LOOPBACK:
        print("refused: the host must be a loopback address (127.0.0.1 or ::1)", file=sys.stderr)
        return 2
    server = (Server6 if ":" in args.host else Server)((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
