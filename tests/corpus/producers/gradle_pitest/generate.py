"""Producer for the PIT corpus capture (PIT's own mutations.xml, which qa-mutation's converter turns into a mutation report).

Usage: python3 generate.py [--write-verification-metadata]     (on a host with Docker)

Runs `gradle test pitest` (Gradle 9.5.0, the info.solidsoft.pitest plugin 1.19.0, PIT 1.30.2 with its JUnit 5 plugin 1.2.3,
JUnit 5.14.4) on a small sample project inside containers of gradle:9.5.0-jdk17 (Eclipse Temurin 17), pinned by index
digest. `corpus/Shipping.java` is tested and holds a surviving mutant at its weight boundary; `corpus/Label.java` has no
test, so its mutants have no coverage. The project is copied into a fresh temporary directory that is mounted as the only
volume, with GRADLE_USER_HOME inside it. A first container with a network resolves the dependencies, which Gradle checks
against gradle/verification-metadata.xml; the run whose report is kept is a second one with no network and --offline.
Copies build/reports/pitest/mutations.xml and PIT's console output (its statistics block: mutations generated and
killed, per mutator, per status), normalized, into `captured/` here, and deletes the directory.
`--write-verification-metadata` instead lets Gradle write the checksums once and copies the file back here. Every container
is named qa-suite-corpus-* and labelled qa-suite=corpus-producer; nothing else on the Docker host is touched.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# gradle:9.5.0-jdk17, resolved on 2026-11-10 with `docker buildx imagetools inspect` (the image of the JaCoCo producer).
IMAGE = "gradle@sha256:dfb52c9c5247cf6129770cd60597bbc1e67cdcfb7fd3691ed450d1c60737ea9e"
PIT = "1.30.2"

HERE = Path(__file__).resolve().parent
NORMALIZE = HERE.parent / "normalize.py"
CAPTURED = HERE / "captured"


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


def statistics(stdout: str) -> str:
    """PIT's statistics block as it printed it, from the "- Mutators" heading to the end of "- Statistics"."""
    start = stdout.find("- Mutators")
    end = stdout.find(">> Ran ", start)
    if start < 0 or end < 0:
        return ""
    end = stdout.find("\n", end)
    return stdout[start:end if end >= 0 else len(stdout)].rstrip() + "\n"


def main(argv) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="qa-gradle-pitest-"))
    try:
        project = tmp / "project"
        shutil.copytree(str(HERE), str(project), ignore=shutil.ignore_patterns("build", ".gradle", "generate.py", "__pycache__", "captured"))
        (project / "home").mkdir()
        gradle = "gradle --no-daemon --console=plain"
        if "--write-verification-metadata" in argv:
            metadata = project / "gradle" / "verification-metadata.xml"
            if metadata.exists():
                metadata.unlink()
            script = "%s --write-verification-metadata sha256 test pitest" % gradle
            if docker(project, "pitest-metadata", True, script).returncode != 0:
                return 1
            (HERE / "gradle").mkdir(exist_ok=True)
            shutil.copyfile(str(metadata), str(HERE / "gradle" / "verification-metadata.xml"))
            print("wrote gradle/verification-metadata.xml")
            return 0
        if docker(project, "pitest-resolve", True, "%s test pitest" % gradle).returncode != 0:
            return 1
        shutil.rmtree(str(project / "build"))
        proc = docker(project, "pitest-run", False, "%s --offline test pitest && gradle --version | grep -e '^Gradle' -e 'JVM:'" % gradle)
        xml = project / "build" / "reports" / "pitest" / "mutations.xml"
        block = statistics(proc.stdout)
        if proc.returncode != 0 or not xml.is_file() or not block:
            print("PIT wrote no report or no statistics", file=sys.stderr)
            return 1
        if CAPTURED.exists():
            shutil.rmtree(str(CAPTURED))
        CAPTURED.mkdir()
        written = [CAPTURED / "mutations.xml", CAPTURED / "console.txt"]
        shutil.copyfile(str(xml), str(written[0]))
        written[1].write_text(block, encoding="utf-8")
        if subprocess.run([sys.executable, str(NORMALIZE)] + [str(p) for p in written]).returncode != 0:
            return 1
        for path in written:
            print("wrote " + str(path.relative_to(HERE)))
        gradle_v = re.search(r"^Gradle (\S+)", proc.stdout, re.M)
        jvm = re.search(r"JVM:\s+(.+)$", proc.stdout, re.M)
        print("gradle %s, PIT %s, JVM %s, image %s" % (gradle_v.group(1) if gradle_v else "?", PIT,
                                                    jvm.group(1).strip() if jvm else "?", IMAGE))
        return 0
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
