"""The accessibility corpus's fixture page: a standard-library HTTP server on loopback.

Usage: python3 page_service.py PORT

It runs only as the ``--service`` of a ``qa run --container`` (network none), never on the host. ``GET /`` is a small HTML
page that holds three seeded accessibility classes and nothing else:

- an image with no text alternative (WCAG 1.1.1, rule ``image-alt``): a violation;
- text whose colour is too close to its background (WCAG 1.4.3, rule ``color-contrast``): a violation;
- text over a gradient, whose contrast a script cannot compute (WCAG 1.4.3 again): an ``incomplete`` check that needs a person.

Any other path is a 404, any other method a 405.
"""
from __future__ import annotations

import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PAGE = b"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Accessibility fixture page</title></head>
<body>
<main>
<h1>Accessibility fixture page</h1>
<img src="/logo.png" width="40" height="40">
<p style="color:#bbbbbb;background:#ffffff">Pale grey text on a white background.</p>
<p style="color:#777777;background-image:linear-gradient(#ffffff,#cccccc)">Grey text over a gradient.</p>
</main>
</body>
</html>
"""


class Page(BaseHTTPRequestHandler):
    server_version = "page"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 (the name http.server calls)
        found = self.path.split("?", 1)[0] == "/"
        body = PAGE if found else b"not found\n"
        self.send_response(200 if found else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8" if found else "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def __getattr__(self, name: str):
        """Every other method is answered 405, as HTTP asks, not 501."""
        if name.startswith("do_"):
            def not_allowed() -> None:
                self.send_response(405)
                self.send_header("Allow", "GET")
                self.send_header("Content-Length", "0")
                self.end_headers()
            return not_allowed
        raise AttributeError(name)


def main(argv: list) -> int:
    if len(argv) != 2 or not argv[1].isdigit():
        print("usage: page_service.py PORT", file=sys.stderr)
        return 2
    HTTPServer(("127.0.0.1", int(argv[1])), Page).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
