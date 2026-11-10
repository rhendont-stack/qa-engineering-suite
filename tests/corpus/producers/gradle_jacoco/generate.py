"""Producer for the Gradle JaCoCo corpus entry (JaCoCo XML).

Usage: python3 generate.py [--write-verification-metadata]     (on a host with Docker)

Runs `gradle test jacocoTestReport` (Gradle 9.5.0, JaCoCo 0.8.13, JUnit 5.14.4)
on a small sample project inside containers of gradle:9.5.0-jdk17, pinned by
index digest. The project is copied into a fresh temporary directory that is
mounted as the only volume, with GRADLE_USER_HOME inside it. A first container
with a network resolves the dependencies, which Gradle checks against
gradle/verification-metadata.xml; the run whose report is kept is a second one
with no network and --offline. Copies jacocoTestReport.xml into
tests/corpus/, normalizes it, prints the commands it ran and the JaCoCo totals
from the CSV report the same task wrote, and deletes the directory.
`--write-verification-metadata` instead lets Gradle write the checksums once and
copies the file back here. Every container is named qa-suite-corpus-* and
labelled qa-suite=corpus-producer; nothing else on the Docker host is touched.
"""
from __future__ import annotations

import csv
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# gradle:9.5.0-jdk17, resolved on 2026-11-10 with `docker buildx imagetools inspect`.
IMAGE = "gradle@sha256:dfb52c9c5247cf6129770cd60597bbc1e67cdcfb7fd3691ed450d1c60737ea9e"
JACOCO = "0.8.13"

HERE = Path(__file__).resolve().parent
CORPUS = HERE.parents[1]
NORMALIZE = HERE.parent / "normalize.py"


def docker(project: Path, name: str, network: bool, script: str):
    cmd = ["docker", "run", "--rm", "--name", "qa-suite-corpus-%s-%d" % (name, os.getpid()),
           "--label", "qa-suite=corpus-producer", "-v", "%s:/work" % project, "-w", "/work",
           "-e", "GRADLE_USER_HOME=/work/gradle-home", "-e", "HOME=/work/home"]
    if not network:
        cmd += ["--network=none"]
    cmd += [IMAGE, "sh", "-c", script]
    print("$ " + " ".join(cmd[:-1]) + " '" + script + "'", flush=True)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    sys.stdout.write(proc.stdout)
    return proc


def main(argv) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="qa-gradle-jacoco-"))
    try:
        project = tmp / "project"
        shutil.copytree(str(HERE), str(project), ignore=shutil.ignore_patterns("build", ".gradle", "generate.py", "__pycache__"))
        (project / "home").mkdir()
        gradle = "gradle --no-daemon --console=plain"
        if "--write-verification-metadata" in argv:
            (project / "gradle" / "verification-metadata.xml").unlink()
            script = "%s --write-verification-metadata sha256 test jacocoTestReport" % gradle
            if docker(project, "jacoco-metadata", True, script).returncode != 0:
                return 1
            shutil.copyfile(str(project / "gradle" / "verification-metadata.xml"), str(HERE / "gradle" / "verification-metadata.xml"))
            print("wrote gradle/verification-metadata.xml")
            return 0
        if docker(project, "jacoco-resolve", True, "%s test jacocoTestReport" % gradle).returncode != 0:
            return 1
        shutil.rmtree(str(project / "build"))
        proc = docker(project, "jacoco-run", False, "%s --offline test jacocoTestReport && gradle --version | grep '^Gradle'" % gradle)
        base = project / "build" / "reports" / "jacoco" / "test"
        xml, table = base / "jacocoTestReport.xml", base / "jacocoTestReport.csv"
        if proc.returncode != 0 or not xml.is_file() or not table.is_file():
            print("JaCoCo wrote no report", file=sys.stderr)
            return 1
        gradle_v = re.search(r"^Gradle (\S+)", proc.stdout, re.M).group(1)
        dst = CORPUS / "jacoco" / ("gradle-%s-jacoco-%s-shipping.xml" % (gradle_v, JACOCO))
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(xml), str(dst))
        if subprocess.run([sys.executable, str(NORMALIZE), str(dst)]).returncode != 0:
            return 1
        print("wrote " + str(dst.relative_to(CORPUS)))
        print("gradle %s, jacoco %s, image %s" % (gradle_v, JACOCO, IMAGE))
        rows = list(csv.DictReader(table.read_text(encoding="utf-8").splitlines()))
        totals = {key: sum(int(r[key]) for r in rows) for key in ("LINE_MISSED", "LINE_COVERED", "BRANCH_MISSED", "BRANCH_COVERED")}
        print("console summary (jacoco CSV, the tool's own report): " + " ".join("%s=%d" % kv for kv in totals.items()) + " files=%d" % len(rows))
        return 0
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
