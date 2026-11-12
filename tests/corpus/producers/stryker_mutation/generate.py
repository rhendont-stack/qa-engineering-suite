"""Producer for the StrykerJS mutation-testing corpus entry (mutation-testing-report-schema JSON).

Usage: python3 generate.py [--write-lock]     (on a host with Docker)

Runs StrykerJS 10.1.1 (@stryker-mutator/core and @stryker-mutator/vitest-runner, with vitest 3.2.7) on a tiny sample
project inside containers of node:lts-slim, pinned by index digest. One source file is tested and holds one surviving
mutant (`weightKg > 10` becomes `>= 10`, which changes nothing at exactly 10 kg); the other is not tested, so its
mutants have no coverage. The project is copied into a fresh temporary directory that is mounted as the only volume.
`npm ci --ignore-scripts` installs the locked packages in a first container with a network; Stryker runs in a second
one with none. Copies reports/mutation/mutation.json into tests/corpus/, normalizes it, prints the commands it ran and
Stryker's own clear-text summary table, and deletes the directory. `--write-lock` instead runs
`npm install --package-lock-only` once and copies package-lock.json back here, without the `deprecated` notices.
Every container is named qa-suite-corpus-* and labelled qa-suite=corpus-producer; nothing else on the Docker host is
touched.
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

PS_SETUP = ("apt-get update -qq && apt-get install -y -qq --no-install-recommends procps >/dev/null && mkdir -p ps/bin ps/lib "
            "&& cp /usr/bin/ps ps/bin/ && ldd /usr/bin/ps | awk '/=> \\//{print $3}' | grep -v -e libc.so -e libm.so | xargs -r -I{} cp {} ps/lib/")

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
    tmp = Path(tempfile.mkdtemp(prefix="qa-stryker-mutation-"))
    try:
        project = tmp / "project"
        shutil.copytree(str(HERE), str(project), ignore=shutil.ignore_patterns("node_modules", "generate.py", "__pycache__", "reports", ".stryker-tmp"))
        (project / "home").mkdir()
        if "--write-lock" in argv:
            if docker(project, "stryker-lock", True, "npm install --package-lock-only --ignore-scripts").returncode != 0:
                return 1
            # npm copies each package's `deprecated` notice into the lock, some with a maintainer's email address.
            # The notice is not read by `npm ci`, and the public repository holds no personal addresses.
            lock = json.loads((project / "package-lock.json").read_text(encoding="utf-8"))
            for entry in lock["packages"].values():
                entry.pop("deprecated", None)
            (HERE / "package-lock.json").write_text(json.dumps(lock, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print("wrote package-lock.json")
            return 0
        # Stryker stops its test-runner processes with `tree-kill`, which runs `ps`; node:lts-slim has none. The
        # install container (the only one with a network) therefore also installs procps and copies `ps` and the
        # libraries it needs into the project, where the second container finds them through PATH and LD_LIBRARY_PATH.
        if docker(project, "stryker-install", True, "npm ci --ignore-scripts && " + PS_SETUP).returncode != 0:
            return 1
        proc = docker(project, "stryker-run", False,
                      "export PATH=/work/ps/bin:$PATH LD_LIBRARY_PATH=/work/ps/lib && node --version && npx stryker run && npx stryker --version")
        report = project / "reports" / "mutation" / "mutation.json"
        if proc.returncode != 0 or not report.is_file():
            print("Stryker wrote no mutation report", file=sys.stderr)
            return 1
        lock = (project / "package-lock.json").read_text(encoding="utf-8")
        version = re.search(r'"node_modules/@stryker-mutator/core": \{\s*"version": "([^"]+)"', lock).group(1)
        dst = CORPUS / "mutation" / ("stryker-%s-shipping.json" % version)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(report), str(dst))
        if subprocess.run([sys.executable, str(NORMALIZE), str(dst)]).returncode != 0:
            return 1
        print("wrote " + str(dst.relative_to(CORPUS)))
        print("@stryker-mutator/core %s, image %s" % (version, IMAGE))
        table = [l for l in proc.stdout.splitlines() if re.match(r"\s*(File|All files|shipping\.mjs|label\.mjs)\s*\|", l)]
        print("console summary: " + " | ".join(re.sub(r"\s+", " ", l).strip() for l in table))
        return 0
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
