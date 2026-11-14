"""Writes a browser run's inputs from the skill's committed assets into ``.qa/work/frontend/``, immediately before the run.

Usage: python3 run_inputs.py --kind runner
       python3 run_inputs.py --kind checks [--page PATH ...] [--state-route PATH ...] [--loading SEL] [--list SEL]
                             [--empty SEL] [--error SEL]
       python3 run_inputs.py --kind axe [--page PATH ...]
       python3 run_inputs.py --kind lighthouse

Each kind writes every file its leg passes as ``--input`` (``references/tools.md``):

* ``runner`` and ``lighthouse``: ``package.json`` and ``package-lock.json`` from ``assets/runner/``, the pinned runner
  (``@playwright/test``, ``@axe-core/playwright``, ``axe-core``, ``lighthouse``) that the run's install step puts in place
  with ``npm ci --ignore-scripts --no-audit --no-fund``. Lighthouse takes its URL on its own command line.
* ``checks``: the runner, ``playwright.config.mjs``, ``checks.spec.mjs`` and ``pages.json``.
* ``axe``: the runner, ``playwright.config.mjs``, ``axe.spec.mjs`` and ``pages.json``.

``pages.json`` lists the pages to check, as paths on the run's base URL (``--page``, repeated; ``/`` when none is given),
and, for the state checks, the data requests to route (``--state-route``, a path such as ``/api/products``) with the
selectors of the loading indicator, the list the data fills, the empty-state message and the error message. A path is
a plain absolute path: no scheme, no host, no ``//`` at its start and no backslash, so no page outside the base URL can
be named; the specs refuse one again at run time. The base URL itself is never written here: the run sets
``QA_BASE_URL=http://127.0.0.1:<port>`` on its command line, and the config refuses any other.

Every file is rewritten from the assets on each call, under ``.qa/work/frontend/`` only, through no link (a fresh temporary
file by exclusive create, then a rename onto the name). Prints one JSON summary with each written path, its sha256 and its
path inside the run. Exit 0 on success, 2 on bad arguments or a refused path. Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent / "assets"
WORK = ".qa/work/frontend"
INSIDE = "/work/.qa-input/frontend"
RUNNER = {"package.json": ASSETS / "runner" / "package.json", "package-lock.json": ASSETS / "runner" / "package-lock.json"}
KINDS = {
    "runner": dict(RUNNER),
    "lighthouse": dict(RUNNER),
    "checks": dict(RUNNER, **{"playwright.config.mjs": ASSETS / "playwright.config.mjs", "checks.spec.mjs": ASSETS / "checks.spec.mjs"}),
    "axe": dict(RUNNER, **{"playwright.config.mjs": ASSETS / "playwright.config.mjs", "axe.spec.mjs": ASSETS / "axe.spec.mjs"}),
}
INSTALL = "cd %s && npm ci --ignore-scripts --no-audit --no-fund" % INSIDE
PAGE = re.compile(r"/(?!/)[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,300}(?:\?[A-Za-z0-9._~!$&'()*+,;=:@%/?-]{0,300})?")
SELECTOR = re.compile(r"[^\x00-\x1f\x7f]{1,200}")


def _writer():
    """``static_ui_scan.write_new`` and ``Refused``, loaded from beside this file (an isolated interpreter does not put this
    directory on the import path)."""
    spec = importlib.util.spec_from_file_location("qa_frontend_static_ui_scan", str(HERE / "static_ui_scan.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def plain_path(text: str, what: str, refused) -> str:
    if not PAGE.fullmatch(text) or "\\" in text or "/../" in text + "/" or "/./" in text + "/":
        raise refused("%s must be a plain absolute path on the base URL (such as / or /about.html), with no scheme or host" % what)
    return text


def selector(text: Optional[str], what: str, refused) -> Optional[str]:
    if text is None:
        return None
    if not SELECTOR.fullmatch(text):
        raise refused("%s must be a CSS selector of 1 to 200 printable characters" % what)
    return text


def pages_json(args: argparse.Namespace, refused) -> bytes:
    pages = [plain_path(p, "--page", refused) for p in (args.page or ["/"])]
    if len(set(pages)) != len(pages):
        raise refused("--page names a page twice")
    routes = [plain_path(r, "--state-route", refused) for r in (args.state_route or [])]
    states = {"routes": routes, "loading": selector(args.loading, "--loading", refused),
              "list": selector(args.list, "--list", refused), "empty": selector(args.empty, "--empty", refused),
              "error": selector(args.error, "--error", refused)}
    if not routes and any(states[k] for k in ("loading", "list", "empty", "error")):
        raise refused("the state selectors go with --state-route, the data request the state checks route")
    document = {"pages": pages, "states": states}
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def build(args: argparse.Namespace, refused) -> Dict[str, bytes]:
    page_options = args.page or args.state_route or any(v is not None for v in (args.loading, args.list, args.empty, args.error))
    if args.kind in ("runner", "lighthouse") and page_options:
        raise refused("--kind %s takes no page option: the URL goes on the run's command line" % args.kind)
    if args.kind == "axe" and (args.state_route or any(v is not None for v in (args.loading, args.list, args.empty, args.error))):
        raise refused("the state options go with --kind checks")
    files = {name: source.read_bytes() for name, source in KINDS[args.kind].items()}
    if args.kind in ("checks", "axe"):
        files["pages.json"] = pages_json(args, refused)
    return files


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kind", required=True, choices=sorted(KINDS))
    parser.add_argument("--page", action="append")
    parser.add_argument("--state-route", action="append")
    parser.add_argument("--loading")
    parser.add_argument("--list")
    parser.add_argument("--empty")
    parser.add_argument("--error")
    args = parser.parse_args(argv)
    writer = _writer()
    root = Path.cwd().resolve()
    written: List[Dict[str, str]] = []
    try:
        files = build(args, writer.Refused)
        for name in sorted(files):
            path = writer.write_new("%s/%s" % (WORK, name), files[name], root)
            written.append({"path": path, "sha256": sha256(files[name]), "input": "%s/%s" % (INSIDE, name)})
    except (writer.Refused, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, writer.Refused) else type(exc).__name__,
                          "written": written}, sort_keys=True))
        return 2
    summary = {"ok": True, "kind": args.kind, "written": written, "inputs": [w["path"] for w in written], "install": INSTALL,
               "base_url": "QA_BASE_URL=http://127.0.0.1:<port> on the run's command line"}
    if args.kind in ("checks", "axe"):
        summary["pages"] = json.loads(files["pages.json"].decode("utf-8"))
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
