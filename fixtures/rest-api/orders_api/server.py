"""Serves ``dispatch`` over HTTP on a loopback address: ``python -m orders_api.server --host 127.0.0.1 --port 8080``.

The collection can start with orders already in it, for runs that need a large one: ``--preload N`` or ORDERS_API_PRELOAD=N.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orders_api import app  # noqa: E402

LOOPBACK = ("127.0.0.1", "::1")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def serve(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        status, headers, content = app.dispatch(self.command, self.path, dict(self.headers.items()), body)
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    do_GET = do_POST = do_PUT = do_DELETE = serve


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128


class Server6(Server):
    address_family = socket.AF_INET6


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="The orders API on a loopback address.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--preload", type=int, default=int(os.environ.get("ORDERS_API_PRELOAD", "0")))
    args = parser.parse_args(argv)
    if args.host not in LOOPBACK:
        print("refused: the host must be a loopback address (127.0.0.1 or ::1)", file=sys.stderr)
        return 2
    app.reset(preload=max(args.preload, 0))
    server = (Server6 if ":" in args.host else Server)((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
