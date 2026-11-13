"""Writes a container run's input from the skill's committed assets, immediately before the run: a ZAP plan or the Semgrep rules.

Usage: python3 run_inputs.py --kind zap-baseline
       python3 run_inputs.py --kind zap-active [--openapi PATH]
       python3 run_inputs.py --kind semgrep-rules
       python3 run_inputs.py --kind osv-ecosystems

Each kind is the name of the file it writes under ``.qa/work/security/``:

* ``zap-baseline`` writes ``.qa/work/security/zap-baseline.yaml`` from ``assets/zap-baseline.yaml``: the spider, the passive
  checks and the ``sarif-json`` report job, with the target as ``${QA_TARGET}``. That is the one plan name the guard reads as
  passive (``dast-passive``), so the copy is rewritten from the asset on every call, and an asset holding an ``activeScan`` job
  is refused.
* ``zap-active`` writes ``.qa/work/security/zap-active.yaml`` from ``assets/zap-active.yaml`` (adds the active scan, capped at ten
  minutes; ``dast-active``). ``--openapi PATH`` puts an ``openapi`` job in the spider's place: it imports the project's
  contract at ``/work/PATH`` against ``${QA_TARGET}``, so the active scan reaches every documented operation, and no crawl
  starts from a root an API does not serve (ZAP ends such a plan with a warning exit code). ``PATH`` is a regular file of the
  project, given relative to its root, with no link on the way.
* ``semgrep-rules`` writes ``.qa/work/security/semgrep-rules.yaml`` from ``assets/semgrep-rules/`` (every ``*.yaml`` there,
  their rule lists joined).
* ``osv-ecosystems`` writes ``.qa/work/security/osv-ecosystems.json``: the OSV advisory databases the project needs, one per
  ecosystem its manifests and lockfiles name (``requirements.txt``, ``poetry.lock``, ``Pipfile.lock``, ``uv.lock``: PyPI;
  ``package-lock.json``, ``yarn.lock``, ``pnpm-lock.yaml``: npm; ``go.sum``, ``go.mod``: Go; ``pom.xml``: Maven; ``Cargo.lock``:
  crates.io; ``Gemfile.lock``: RubyGems; ``composer.lock``: Packagist), found by a walk of the project that skips dependency and
  tool directories and links. The summary lists the ecosystems and one install line for each, to pass as the OSV leg's
  ``--install`` in place of its example line. It reads the project and writes nothing outside that file.

The run sets ``QA_TARGET=http://127.0.0.1:<port>`` on its command line; nothing here names a host. Writes go only under
``.qa/work/security/``, through no link. Prints one JSON summary with the written path and its sha256 (the kernel records the
sha256 of every ``--input`` too). Exit 0 on success, 2 on bad arguments or a refused path. Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import List, Sequence

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent / "assets"
OUTPUT = {"zap-baseline": ".qa/work/security/zap-baseline.yaml", "zap-active": ".qa/work/security/zap-active.yaml",
          "semgrep-rules": ".qa/work/security/semgrep-rules.yaml", "osv-ecosystems": ".qa/work/security/osv-ecosystems.json"}
OSV_FILES = {"requirements.txt": "PyPI", "poetry.lock": "PyPI", "Pipfile.lock": "PyPI", "uv.lock": "PyPI",
             "package-lock.json": "npm", "yarn.lock": "npm", "pnpm-lock.yaml": "npm", "go.sum": "Go", "go.mod": "Go",
             "pom.xml": "Maven", "Cargo.lock": "crates.io", "Gemfile.lock": "RubyGems", "composer.lock": "Packagist"}
OSV_SKIP_DIRS = {".git", ".qa", ".qa-input", ".claude", "node_modules", "__pycache__", "vendor", "site-packages", "target", "dist", "build"}
OSV_HOST = "https://osv-vulnerabilities.storage.googleapis.com"
SAFE_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")
SPIDER = "  - type: spider\n    parameters:\n      context: target\n      maxDuration: 2\n"


def _writer():
    """``unicode_scan.write_new`` and ``Refused``, loaded from beside this file (an isolated interpreter does not put this
    directory on the import path)."""
    spec = importlib.util.spec_from_file_location("qa_security_unicode_scan", str(HERE / "unicode_scan.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def project_file(root: Path, text: str, refused) -> str:
    """``text`` when it names a regular file of the project with no link on the way; Refused otherwise."""
    parts = PurePosixPath(text).parts
    if not SAFE_PATH.fullmatch(text) or ".." in parts or "." in parts or parts[0] in (".qa", ".git", ".claude"):
        raise refused("--openapi must be a plain relative path to a file of the project")
    here = root
    for part in parts:
        here = here / part
        try:
            mode = os.lstat(str(here)).st_mode
        except OSError:
            raise refused("--openapi names no file of the project") from None
        if stat.S_ISLNK(mode):
            raise refused("--openapi passes through a link")
    if not stat.S_ISREG(mode):
        raise refused("--openapi is not a regular file")
    return text


def openapi_job(path: str) -> str:
    return ("  - type: openapi\n    parameters:\n      apiFile: /work/%s\n      targetUrl: ${QA_TARGET}\n      context: target\n" % path)


def rules_text(refused) -> bytes:
    sources = sorted((ASSETS / "semgrep-rules").glob("*.yaml"))
    if not sources:
        raise refused("no rules in assets/semgrep-rules/")
    bodies: List[str] = ["# Written by run_inputs.py --kind semgrep-rules from assets/semgrep-rules/ (Apache-2.0); do not edit.\nrules:\n"]
    for source in sources:
        text = source.read_text(encoding="utf-8")
        head, marker, body = text.partition("\nrules:\n")
        if not marker:
            raise refused("%s has no top-level rules list" % source.name)
        bodies.append(body if body.endswith("\n") else body + "\n")
    return "".join(bodies).encode("utf-8")


def osv_ecosystems(root: Path) -> List[str]:
    """The OSV ecosystems the manifests and lockfiles under ``root`` name, in the order PyPI, npm, Go, Maven, crates.io, RubyGems,
    Packagist; no directory of dependencies or tooling (``OSV_SKIP_DIRS``, ``.venv*``) and no link is followed."""
    found = set()
    for here, directories, names in os.walk(str(root)):
        directories[:] = sorted(d for d in directories if d not in OSV_SKIP_DIRS and not d.startswith(".venv")
                                and not os.path.islink(os.path.join(here, d)))
        for name in names:
            if name in OSV_FILES and not os.path.islink(os.path.join(here, name)):
                found.add(OSV_FILES[name])
    order = ["PyPI", "npm", "Go", "Maven", "crates.io", "RubyGems", "Packagist"]
    return [e for e in order if e in found]


def osv_install_line(ecosystem: str) -> str:
    return ("mkdir -p /work/osv-db/osv-scalibr/{e} && wget -q -O /work/osv-db/osv-scalibr/{e}/all.zip {h}/{e}/all.zip"
            .format(e=ecosystem, h=OSV_HOST))


def build(kind: str, openapi: str, root: Path, refused) -> bytes:
    if kind == "osv-ecosystems":
        if openapi:
            raise refused("--openapi goes with --kind zap-active only")
        found = osv_ecosystems(root)
        return (json.dumps({"ecosystems": found, "install": [osv_install_line(e) for e in found]}, indent=2, sort_keys=True)
                + "\n").encode("utf-8")
    if kind == "semgrep-rules":
        if openapi:
            raise refused("--openapi goes with --kind zap-active only")
        return rules_text(refused)
    text = (ASSETS / ("%s.yaml" % kind)).read_text(encoding="utf-8")
    if kind == "zap-baseline":
        if openapi:
            raise refused("--openapi goes with --kind zap-active only: an imported contract is scanned actively")
        if re.search(r"(?m)^\s*-\s*type:\s*activeScan\b", text):
            raise refused("the baseline asset holds an activeScan job")
        return text.encode("utf-8")
    if not re.search(r"(?m)^\s*-\s*type:\s*activeScan\b", text) or "maxScanDurationInMins: 10" not in text or text.count(SPIDER) != 1:
        raise refused("the active asset lost its capped activeScan job or its spider job")
    if openapi:
        text = text.replace(SPIDER, openapi_job(project_file(root, openapi, refused)))
    return text.encode("utf-8")


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kind", required=True, choices=sorted(OUTPUT))
    parser.add_argument("--openapi", default="")
    args = parser.parse_args(argv)
    writer = _writer()
    root = Path.cwd().resolve()
    try:
        data = build(args.kind, args.openapi, root, writer.Refused)
        written = writer.write_new(OUTPUT[args.kind], data, root)
    except (writer.Refused, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, writer.Refused) else type(exc).__name__}, sort_keys=True))
        return 2
    summary = {"ok": True, "kind": args.kind, "written": written, "sha256": sha256(data),
               "input": "/work/.qa-input/" + written[len(".qa/work/"):]}
    if args.kind == "osv-ecosystems":
        listed = json.loads(data.decode("utf-8"))
        summary["ecosystems"], summary["install"] = listed["ecosystems"], listed["install"]
        del summary["input"]
    elif args.kind != "semgrep-rules":
        summary["target"] = "QA_TARGET=http://127.0.0.1:<port> on the run's command line"
        summary["openapi"] = args.openapi or None
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
