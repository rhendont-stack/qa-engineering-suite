"""Serves ``site/`` and ``GET /api/products`` on a loopback address: ``python server.py --host 127.0.0.1 --port 8080``.

``dispatch`` is the whole server without sockets. ``--products`` picks what the product list returns: ``normal`` (a short list),
``empty`` (``[]``) or ``error`` (HTTP 500).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Tuple

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site")
LOOPBACK = ("127.0.0.1", "::1")
MODES = ("normal", "empty", "error")
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".svg": "image/svg+xml"}
PRODUCTS = [
    {"id": 1, "name": "Notebook", "price": "4.50"},
    {"id": 2, "name": "Fountain pen", "price": "12.00"},
    {"id": 3, "name": "Graph paper pad", "price": "3.25"},
]
MODE = "normal"

Reply = Tuple[int, Dict[str, str], bytes]


def set_products(mode: str) -> None:
    global MODE
    if mode not in MODES:
        raise ValueError("products must be one of: " + ", ".join(MODES))
    MODE = mode


def reply(status: int, content_type: str, body: bytes) -> Reply:
    return status, {"Content-Type": content_type, "Cache-Control": "no-store"}, body


def message(status: int, text: str) -> Reply:
    return reply(status, "application/json", json.dumps({"error": text}).encode("utf-8"))


def dispatch(method: str, path: str) -> Reply:
    if method not in ("GET", "HEAD"):
        return message(405, "method not allowed")
    path = path.split("?", 1)[0].split("#", 1)[0]
    if path == "/api/products":
        if MODE == "error":
            return message(500, "the product list is unavailable")
        return reply(200, "application/json", json.dumps([] if MODE == "empty" else PRODUCTS).encode("utf-8"))
    name = "index.html" if path == "/" else path.lstrip("/")
    if "\\" in name or "\x00" in name or any(part in ("", ".", "..") for part in name.split("/")):
        return message(404, "not found")
    target = os.path.join(ROOT, *name.split("/"))
    kind = TYPES.get(os.path.splitext(name)[1])
    if kind is None or not os.path.isfile(target):
        return message(404, "not found")
    with open(target, "rb") as handle:
        return reply(200, kind, handle.read())


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def serve(self) -> None:
        status, headers, content = dispatch(self.command, self.path)
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = serve


class Server(ThreadingHTTPServer):
    daemon_threads = True


class Server6(Server):
    address_family = socket.AF_INET6


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="The shop front end on a loopback address.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--products", choices=MODES, default="normal")
    args = parser.parse_args(argv)
    if args.host not in LOOPBACK:
        print("refused: the host must be a loopback address (127.0.0.1 or ::1)", file=sys.stderr)
        return 2
    set_products(args.products)
    server = (Server6 if ":" in args.host else Server)((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
