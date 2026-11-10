"""Producer for the coverage.py corpus entries (Cobertura XML and LCOV).

Usage: python3 generate.py     (on a host with Docker; see below)

Runs coverage.py 7.10.7 with pytest 8.4.2 on a small sample project inside
containers of python:3.9-slim, pinned by index digest. The project is copied
into a fresh temporary directory that is mounted as the only volume; the
packages come from requirements.txt with --require-hashes. Packages install in
a first container with a network; the tests run in a second one with none.
Copies coverage.xml and coverage.lcov into tests/corpus/, normalizes them,
prints the commands it ran and coverage.py's own `report` table, and deletes the
directory. Every container is named qa-suite-corpus-* and labelled
qa-suite=corpus-producer; nothing else on the Docker host is touched.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# python:3.9-slim, resolved on 2026-11-09 (shared/policies/container-images.json, id python-3.9).
IMAGE = "python@sha256:2d97f6910b16bd338d3060f261f53f144965f755599aab1acda1e13cf1731b1b"
COVERAGE, PYTEST = "7.10.7", "8.4.2"

HERE = Path(__file__).resolve().parent
CORPUS = HERE.parents[1]
NORMALIZE = HERE.parent / "normalize.py"


def docker(project: Path, name: str, network: bool, script: str):
    cmd = ["docker", "run", "--rm", "--name", "qa-suite-corpus-%s-%d" % (name, os.getpid()),
           "--label", "qa-suite=corpus-producer", "-v", "%s:/work" % project, "-w", "/work",
           "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONPATH=/work/site"]
    if not network:
        cmd += ["--network=none"]
    cmd += [IMAGE, "sh", "-c", script]
    print("$ " + " ".join(cmd[:-1]) + " '" + script + "'", flush=True)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    sys.stdout.write(proc.stdout)
    return proc


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="qa-coverage-py-"))
    try:
        project = tmp / "project"
        shutil.copytree(str(HERE), str(project), ignore=shutil.ignore_patterns("generate.py", "__pycache__"))
        install = ("pip install --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: "
                   "--target /work/site -r requirements.txt")
        if docker(project, "coverage-install", True, install).returncode != 0:
            return 1
        run = ("python -m coverage run --branch --source=. --omit='test_*.py,site/*' -m pytest -q -p no:cacheprovider"
               " && python -m coverage xml -o coverage.xml && python -m coverage lcov -o coverage.lcov"
               " && python -m coverage --version | head -1 && python -m coverage report")
        proc = docker(project, "coverage-run", False, run)
        if proc.returncode != 0 or not (project / "coverage.xml").is_file() or not (project / "coverage.lcov").is_file():
            print("coverage.py wrote no report", file=sys.stderr)
            return 1
        version = re.search(r"[Cc]overage\.py, version (\S+)", proc.stdout).group(1)
        outputs = [(project / "coverage.xml", CORPUS / "cobertura" / ("coveragepy-%s-shipping.xml" % version)),
                   (project / "coverage.lcov", CORPUS / "lcov" / ("coveragepy-%s-shipping.info" % version))]
        for src, dst in outputs:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(src), str(dst))
        if subprocess.run([sys.executable, str(NORMALIZE)] + [str(d) for _s, d in outputs]).returncode != 0:
            return 1
        for _s, dst in outputs:
            print("wrote " + str(dst.relative_to(CORPUS)))
        print("coverage.py %s, pytest %s, image %s" % (version, PYTEST, IMAGE))
        table = [l for l in proc.stdout.splitlines() if re.match(r"(Name|TOTAL|shipping\.py)\s", l)]
        print("console summary: " + " | ".join(" ".join(l.split()) for l in table))
        return 0
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
