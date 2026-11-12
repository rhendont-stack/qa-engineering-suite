"""Producer for the mutmut corpus capture (mutmut's own output, which qa-mutation's converter turns into a mutation report).

Usage: python3 generate.py     (on a host with Docker)

Runs mutmut 3.8.1 (with pytest 8.4.2 and coverage 7.17.2, every package from requirements.txt with --require-hashes) on a
small sample package inside containers of python:3.12-slim, pinned by index digest. `shipping/cost.py` is tested and holds a
surviving mutant at its weight boundary; `shipping/label.py` has no test, so its mutants have none. The project is copied
into a fresh temporary directory that is mounted as the only volume. Packages install in a first container with a network;
mutmut runs in a second one with none: `mutmut run`, then `mutmut results --all true`, `mutmut export-cicd-stats` and
`mutmut show` for every mutant. Copies what the converter reads (each `mutants/<path>.meta` and the mutated `mutants/<path>`)
and mutmut's console output, normalized, into `captured/` here, and deletes the directory. The console output is the tool's
own account of each mutant's status, which the corpus tests compare the converted report against. Every container is named
qa-suite-corpus-* and labelled qa-suite=corpus-producer; nothing else on the Docker host is touched.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# python:3.12-slim, resolved on 2026-11-10 with `docker buildx imagetools inspect` (the interpreter requirements-mutation.txt
# is resolved for; python:3-slim is 3.15, for which pyyaml 6.0.3 has no wheel).
IMAGE = "python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
MUTMUT = "3.8.1"

HERE = Path(__file__).resolve().parent
NORMALIZE = HERE.parent / "normalize.py"
CAPTURED = HERE / "captured"
SOURCES = ("shipping/__init__.py", "shipping/cost.py", "shipping/label.py")

RUN = ("python --version && python -m mutmut run; echo \"mutmut run exit $?\"; "
       "echo '=====RESULTS====='; python -m mutmut results --all true; "
       "echo '=====CICD====='; python -m mutmut export-cicd-stats && cat mutants/mutmut-cicd-stats.json; echo; "
       "echo '=====SHOW====='; for name in $(python -m mutmut results --all true | sed -n 's/^ *\\([^:]*\\):.*/\\1/p'); do "
       "python -m mutmut show \"$name\"; done; echo '=====END====='")


def docker(project: Path, name: str, network: bool, script: str):
    cmd = ["docker", "run", "--rm", "--name", "qa-suite-corpus-%s-%d" % (name, os.getpid()),
           "--label", "qa-suite=corpus-producer", "-v", "%s:/work" % project, "-w", "/work",
           "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONPATH=/work/site", "-e", "HOME=/work/home"]
    if not network:
        cmd += ["--network=none"]
    cmd += [IMAGE, "sh", "-c", script]
    print("$ " + " ".join(cmd[:-1]) + " '" + script + "'", flush=True)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    sys.stdout.write(proc.stdout)
    return proc


SPINNER = re.compile("^[\u2800-\u28ff] ")


def clean_console(text: str) -> str:
    """mutmut's console without its spinner frames; of the progress lines only the last, which is the final count."""
    lines = text.splitlines()
    progress = [l for l in lines if SPINNER.match(l) and len(l.split()) > 1 and "/" in l.split()[1]]
    kept, last = [], progress[-1] if progress else None
    for line in lines:
        if SPINNER.match(line):
            if line is last:
                kept.append(SPINNER.sub("", line))
            continue
        kept.append(line)
    return "\n".join(kept) + "\n"


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="qa-mutmut-mutation-"))
    try:
        project = tmp / "project"
        shutil.copytree(str(HERE), str(project), ignore=shutil.ignore_patterns("generate.py", "__pycache__", "captured", "mutants"))
        (project / "home").mkdir()
        install = ("pip install --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: "
                   "--target /work/site -r requirements.txt")
        if docker(project, "mutmut-install", True, install).returncode != 0:
            return 1
        proc = docker(project, "mutmut-run", False, RUN)
        metas = sorted((project / "mutants").rglob("*.py.meta")) if (project / "mutants").is_dir() else []
        if proc.returncode != 0 or not metas or "=====END=====" not in proc.stdout:
            print("mutmut wrote no mutation data", file=sys.stderr)
            return 1
        if CAPTURED.exists():
            shutil.rmtree(str(CAPTURED))
        written = []
        for source in SOURCES:
            for suffix in ("", ".meta"):
                src = project / "mutants" / (source + suffix)
                if src.is_file():
                    dst = CAPTURED / "mutants" / (source + suffix)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(str(src), str(dst))
                    written.append(dst)
        console = CAPTURED / "console.txt"
        text = proc.stdout.replace("\r", "\n")
        console.write_text(clean_console(text[text.index("Python "):]), encoding="utf-8")
        written.append(console)
        if subprocess.run([sys.executable, str(NORMALIZE)] + [str(p) for p in written]).returncode != 0:
            return 1
        for path in written:
            print("wrote " + str(path.relative_to(HERE)))
        version = re.search(r"^Python (\S+)", text, re.M)
        print("mutmut %s, %s, image %s" % (MUTMUT, version.group(0) if version else "Python ?", IMAGE))
        return 0
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
