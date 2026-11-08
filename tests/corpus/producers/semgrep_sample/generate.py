"""Producer for the Semgrep SARIF corpus entry.

Usage: /usr/bin/python3 generate.py

Writes a Python file with three flagged calls into a fresh temporary directory
at run time (no flagged code is committed), scans it with rules.yml, copies the
SARIF report into tests/corpus/sarif/, normalizes it, prints the commands it ran
and Semgrep's own findings count, and deletes the directory.
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
ROOT = HERE.parents[3]
CORPUS = HERE.parents[1]
NORMALIZE = HERE.parent / "normalize.py"
SEMGREP = ROOT / ".venv-semgrep" / "bin" / "semgrep"

SAMPLE = '''import subprocess


def run(expression, command):
    value = eval(expression)
    subprocess.run(command, shell=True)
    subprocess.call(command + " --verbose", shell=True)
    return value
'''


def run(cmd, env):
    print("$ " + " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    sys.stdout.write(proc.stdout)
    return proc


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="qa-semgrep-sample-"))
    try:
        scan = tmp / "scan"
        scan.mkdir()
        (scan / "sample.py").write_text(SAMPLE, encoding="utf-8")
        env = dict(os.environ, SEMGREP_ENABLE_VERSION_CHECK="0", SEMGREP_SEND_METRICS="off",
                   HOME=str(tmp / "home"), XDG_CACHE_HOME=str(tmp / "cache"))
        (tmp / "home").mkdir()
        version = run([str(SEMGREP), "--version"], env).stdout.strip().splitlines()[-1]
        out = tmp / "semgrep.sarif"
        proc = run([str(SEMGREP), "scan", "--config", str(HERE / "rules.yml"), "--metrics=off", "--disable-version-check",
                    "--sarif", "--output", str(out), str(scan)], env)
        if not out.is_file():
            print("Semgrep wrote no report", file=sys.stderr)
            return 1
        dst = CORPUS / "sarif" / ("semgrep-%s.sarif" % version)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(out), str(dst))
        if subprocess.run([sys.executable, str(NORMALIZE), str(dst)]).returncode != 0:
            return 1
        print("wrote " + str(dst.relative_to(CORPUS)))
        print("console summary: " + " | ".join(l.strip() for l in proc.stdout.splitlines() if re.search(r"\d+ (code )?findings?|Ran \d+ rules", l)))
        return 0
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
