"""Producer for the Gradle + JUnit 5 corpus entry.

Usage: /usr/bin/python3 generate.py

Copies this directory into a fresh temporary directory, runs `gradle test` there
on the host JDK 17 with GRADLE_USER_HOME inside it (dependencies are checked
against gradle/verification-metadata.xml), copies the JUnit XML report into
tests/corpus/junit/, normalizes it, prints the commands it ran and Gradle's own
summary line, and deletes the directory.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORPUS = HERE.parents[1]
NORMALIZE = HERE.parent / "normalize.py"


def run(cmd, cwd, env):
    print("$ " + " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    sys.stdout.write(proc.stdout)
    return proc


def main() -> int:
    jdk17 = subprocess.run(["/usr/libexec/java_home", "-v", "17"], stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout.strip()
    tmp = Path(tempfile.mkdtemp(prefix="qa-gradle-junit5-"))
    try:
        project = tmp / "project"
        shutil.copytree(str(HERE), str(project), ignore=shutil.ignore_patterns("build", ".gradle", "generate.py", "__pycache__"))
        # Homebrew's gradle script would otherwise default JAVA_HOME to its own openjdk 25.
        env = dict(os.environ, JAVA_HOME=jdk17, GRADLE_USER_HOME=str(tmp / "gradle-home"))
        version_out = run(["gradle", "--version", "--no-daemon"], project, env).stdout
        gradle_v = re.search(r"^Gradle (\S+)", version_out, re.M).group(1)
        junit_v = re.search(r'junit-bom:([\d.]+)"', (project / "build.gradle.kts").read_text(encoding="utf-8")).group(1)
        proc = run(["gradle", "--no-daemon", "test"], project, env)
        report = project / "build" / "test-results" / "test" / "TEST-corpus.MixedTest.xml"
        if not report.is_file():
            print("Gradle wrote no report", file=sys.stderr)
            return 1
        dst = CORPUS / "junit" / ("gradle-%s-junit-%s-mixed.xml" % (gradle_v, junit_v))
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(report), str(dst))
        if subprocess.run([sys.executable, str(NORMALIZE), str(dst)]).returncode != 0:
            return 1
        print("wrote " + str(dst.relative_to(CORPUS)))
        print("console summary: " + " | ".join(l.strip() for l in proc.stdout.splitlines() if "tests completed" in l))
        return 0
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
