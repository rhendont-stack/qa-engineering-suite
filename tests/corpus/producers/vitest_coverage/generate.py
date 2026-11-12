"""Producer for the Vitest coverage corpus entries (V8 provider: LCOV and Cobertura XML).

Usage: python3 generate.py [--write-lock]     (on a host with Docker)

Runs `vitest run --coverage` (vitest and @vitest/coverage-v8 3.2.7) on a small
sample project inside containers of node:lts-slim, pinned by index digest. The
project is copied into a fresh temporary directory that is mounted as the only
volume. `npm ci --ignore-scripts` installs the locked packages in a first
container with a network; the tests run in a second one with none. Copies
lcov.info and cobertura-coverage.xml into tests/corpus/, normalizes them,
prints the commands it ran and Vitest's own coverage table, and deletes the
directory. `--write-lock` instead runs `npm install --package-lock-only` once
and copies package-lock.json back here, without the `deprecated` notices. Every container is named
qa-suite-corpus-* and labelled qa-suite=corpus-producer; nothing else on the
Docker host is touched.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# node:lts-slim, resolved on 2026-11-09 (shared/policies/container-images.json, id node-lts).
IMAGE = "node@sha256:5adad64f9a1fca2d51972caa65983634c8a774eb300d72b20f7f2a0f60368072"

HERE = Path(__file__).resolve().parent
CORPUS = HERE.parents[1]
NORMALIZE = HERE.parent / "normalize.py"


def docker(project: Path, name: str, network: bool, script: str):
    cmd = ["docker", "run", "--rm", "--name", "qa-suite-corpus-%s-%d" % (name, os.getpid()),
           "--label", "qa-suite=corpus-producer", "-v", "%s:/work" % project, "-w", "/work",
           "-e", "HOME=/work/home", "-e", "npm_config_update_notifier=false", "-e", "npm_config_fund=false",
           "-e", "npm_config_audit=false"]
    if not network:
        cmd += ["--network=none"]
    cmd += [IMAGE, "sh", "-c", script]
    print("$ " + " ".join(cmd[:-1]) + " '" + script + "'", flush=True)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    sys.stdout.write(proc.stdout)
    return proc


def main(argv) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="qa-vitest-coverage-"))
    try:
        project = tmp / "project"
        shutil.copytree(str(HERE), str(project), ignore=shutil.ignore_patterns("node_modules", "generate.py", "__pycache__", "coverage"))
        (project / "home").mkdir()
        if "--write-lock" in argv:
            if docker(project, "vitest-lock", True, "npm install --package-lock-only --ignore-scripts").returncode != 0:
                return 1
            # npm copies each package's `deprecated` notice into the lock, some with a maintainer's email address.
            # The notice is not read by `npm ci`, and the public repository holds no personal addresses.
            lock = json.loads((project / "package-lock.json").read_text(encoding="utf-8"))
            for entry in lock["packages"].values():
                entry.pop("deprecated", None)
            (HERE / "package-lock.json").write_text(json.dumps(lock, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print("wrote package-lock.json")
            return 0
        if docker(project, "vitest-install", True, "npm ci --ignore-scripts").returncode != 0:
            return 1
        proc = docker(project, "vitest-run", False,
                      "node --version && npx vitest run --coverage --reporter=default && npx vitest --version")
        lcov, xml = project / "coverage" / "lcov.info", project / "coverage" / "cobertura-coverage.xml"
        if proc.returncode != 0 or not lcov.is_file() or not xml.is_file():
            print("Vitest wrote no coverage report", file=sys.stderr)
            return 1
        lock = (project / "package-lock.json").read_text(encoding="utf-8")
        version = re.search(r'"node_modules/@vitest/coverage-v8": \{\s*"version": "([^"]+)"', lock).group(1)
        outputs = [(xml, CORPUS / "cobertura" / ("vitest-coverage-v8-%s-shipping.xml" % version)),
                   (lcov, CORPUS / "lcov" / ("vitest-coverage-v8-%s-shipping.info" % version))]
        for src, dst in outputs:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(src), str(dst))
        if subprocess.run([sys.executable, str(NORMALIZE)] + [str(d) for _s, d in outputs]).returncode != 0:
            return 1
        for _s, dst in outputs:
            print("wrote " + str(dst.relative_to(CORPUS)))
        print("@vitest/coverage-v8 %s, image %s" % (version, IMAGE))
        table = [l for l in proc.stdout.splitlines() if re.match(r"\s*(File|All files|shipping\.mjs)\s*\|", l)]
        print("console summary: " + " | ".join(re.sub(r"\s+", " ", l).strip() for l in table))
        return 0
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
