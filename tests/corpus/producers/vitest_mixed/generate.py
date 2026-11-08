"""Producer for the Vitest JUnit and CTRF corpus entries.

Usage: /usr/bin/python3 generate.py

Copies this directory into a fresh temporary directory, installs the locked
packages there (npm cache inside it too), runs Vitest once with the JUnit and
CTRF reporters, copies both reports into tests/corpus/, normalizes them, prints
the commands it ran and Vitest's own summary lines, and deletes the directory.
"""
from __future__ import annotations

import json
import os
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
    tmp = Path(tempfile.mkdtemp(prefix="qa-vitest-mixed-"))
    try:
        project = tmp / "project"
        shutil.copytree(str(HERE), str(project), ignore=shutil.ignore_patterns("node_modules", "generate.py", "__pycache__"))
        env = dict(os.environ, npm_config_cache=str(tmp / "npm-cache"), npm_config_update_notifier="false",
                   npm_config_fund="false", npm_config_audit="false")
        if run(["npm", "ci", "--ignore-scripts"], project, env).returncode != 0:
            return 1
        # Vitest exits 1 when a test fails, which is the point of the sample.
        proc = run(["npx", "vitest", "run", "--reporter=default", "--reporter=junit", "--reporter=vitest-ctrf-json-reporter",
                    "--outputFile.junit=junit.xml"], project, env)
        junit, ctrf = project / "junit.xml", project / "vitest-ctrf" / "report.json"
        if not junit.is_file() or not ctrf.is_file():
            print("Vitest wrote no report", file=sys.stderr)
            return 1
        lock = json.loads((project / "package-lock.json").read_text(encoding="utf-8"))["packages"]
        vitest_v = lock["node_modules/vitest"]["version"]
        reporter_v = lock["node_modules/vitest-ctrf-json-reporter"]["version"]
        outputs = [(junit, CORPUS / "junit" / ("vitest-%s-mixed.xml" % vitest_v)),
                   (ctrf, CORPUS / "ctrf" / ("vitest-%s-ctrf-%s-mixed.json" % (vitest_v, reporter_v)))]
        for src, dst in outputs:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(src), str(dst))
        if subprocess.run([sys.executable, str(NORMALIZE)] + [str(d) for _s, d in outputs]).returncode != 0:
            return 1
        for _s, dst in outputs:
            print("wrote " + str(dst.relative_to(CORPUS)))
        print("console summary: " + " | ".join(l.strip() for l in proc.stdout.splitlines() if l.strip().startswith(("Test Files", "Tests"))))
        return 0
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
