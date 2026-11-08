"""Producer for the Maven Surefire rerun corpus entry.

Usage: /usr/bin/python3 generate.py

Copies this directory into a fresh temporary directory and runs `mvn test` in
the maven:3-eclipse-temurin-21 image, pinned by index digest, with the project
and the Maven repository mounted from that directory. Copies Surefire's XML
report into tests/corpus/junit/, normalizes it, prints the commands it ran and
Surefire's own result line, and deletes the directory.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# maven:3-eclipse-temurin-21 on 2026-11-07 (resolved with `docker buildx imagetools inspect`).
IMAGE = "maven@sha256:9673eef9ed503146591667ffb92eaaa22f1b8899ed60d9df0df4da3f3a1214a6"

HERE = Path(__file__).resolve().parent
CORPUS = HERE.parents[1]
NORMALIZE = HERE.parent / "normalize.py"


def run(cmd):
    print("$ " + " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    sys.stdout.write(proc.stdout)
    return proc


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="qa-surefire-rerun-"))
    try:
        project, m2 = tmp / "project", tmp / "m2"
        shutil.copytree(str(HERE), str(project), ignore=shutil.ignore_patterns("target", "generate.py", "__pycache__"))
        m2.mkdir()
        docker = ["docker", "run", "--rm", "-v", "%s:/work" % project, "-v", "%s:/root/.m2" % m2, "-w", "/work", IMAGE]
        maven_v = re.search(r"Apache Maven (\S+)", run(docker + ["mvn", "-v"]).stdout).group(1)
        surefire_v = re.search(r"maven-surefire-plugin</artifactId>\s*<version>([\d.]+)<", (project / "pom.xml").read_text(encoding="utf-8")).group(1)
        proc = run(docker + ["mvn", "-B", "test", "-Dsurefire.rerunFailingTestsCount=2", "-Dmaven.test.failure.ignore=true"])
        report = project / "target" / "surefire-reports" / "TEST-corpus.RerunTest.xml"
        if not report.is_file():
            print("Surefire wrote no report", file=sys.stderr)
            return 1
        dst = CORPUS / "junit" / ("surefire-%s-rerun.xml" % surefire_v)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(report), str(dst))
        if subprocess.run([sys.executable, str(NORMALIZE), str(dst)]).returncode != 0:
            return 1
        print("wrote " + str(dst.relative_to(CORPUS)))
        print("maven %s, image %s" % (maven_v, IMAGE))
        print("console summary: " + " | ".join(l.strip() for l in proc.stdout.splitlines() if re.match(r"\[(INFO|WARNING|ERROR)\] Tests run:.*Flakes", l)))
        return 0
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
