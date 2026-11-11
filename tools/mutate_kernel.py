"""Configures mutation testing of the kernel's core modules and prints the command that measures it.

Usage: python3 -I tools/mutate_kernel.py [--modules status,gate,ledger,canonical,compare,report/check,trace] --out DIR

Writes ``DIR/setup.cfg``, a mutmut 3.x configuration that mutates only the named modules of ``shared/kernel/qa/`` and runs
only their unit test files, and prints one JSON object: ``ok``, ``config``, ``modules``, ``image``, ``argv`` and ``command``
(``argv`` as one shell line). The command is a ``qa run --container`` of the suite's own launcher, to run from the repository
root: it installs the pinned tools in a first container step (the only one with a network), writes the same configuration
into the project copy, runs mutmut and then ``skills/qa-mutation/scripts/mutmut_to_report.py`` in that one execution, and
declares the converted file as the run's ``mutation`` report. The score is therefore in that execution's evidence manifest, and
it is read from there: this tool runs nothing and gates nothing. An unknown or repeated module is refused (exit 2) before
anything is written.
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Dict, List, Tuple

# module name -> (source file under the repository root, the unit test file that exercises it)
KERNEL = "shared/kernel/qa/"
MODULES: Dict[str, Tuple[str, str]] = {
    "status": (KERNEL + "status.py", "tests/unit/test_status.py"),
    "gate": (KERNEL + "gate.py", "tests/unit/test_gate.py"),
    "ledger": (KERNEL + "ledger.py", "tests/unit/test_ledger.py"),
    "canonical": (KERNEL + "canonical.py", "tests/unit/test_canonical.py"),
    "compare": (KERNEL + "compare.py", "tests/unit/test_compare.py"),
    "report/check": (KERNEL + "report/check.py", "tests/unit/test_report.py"),
    "trace": (KERNEL + "trace.py", "tests/unit/test_trace.py"),
}
# python:3.12-slim (Python 3.12.16), the index digest the mutmut corpus capture was produced in; requirements-mutation.txt is
# resolved for that interpreter (python:3-slim is 3.15, for which pyyaml 6.0.3 has no wheel).
IMAGE = "python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
LAUNCHER = "skills/qa-mutation/scripts/qa.py"
CONVERTER = "skills/qa-mutation/scripts/mutmut_to_report.py"
REPORT = "{evidence}/kernel-mutation.json"
TIMEOUT_SECONDS = 4 * 3600
# The kernel's own test tools, then mutmut's: the same pins as requirements-mutation.txt, with the hash of every wheel PyPI
# lists (that file holds the one wheel for macOS arm64, which a Linux container cannot use).
REQUIREMENTS = ("requirements-dev.txt", "tests/corpus/producers/mutmut_mutation/requirements.txt")
# What the tests read next to the kernel: mutmut runs them in ``mutants/``, a copy of the paths it is told to take.
ALSO_COPY = ("pytest.ini", "fixtures", "shared/kernel", "shared/launcher", "shared/policies", "shared/schemas", "shared/templates")
VENV, HOME = "/work/venv", "/work/home"


# mutmut names a mutant after its file's path with a leading ``src.`` removed, and the tests import the kernel as ``qa`` (pytest.ini
# puts shared/kernel on the path), so the run mutates the kernel through ``src/qa``, a link made in the project copy. The
# unmutated kernel stays in ``mutants/shared/kernel`` for the tests that start it in a subprocess.
LINK_SOURCE, LINK_TARGET = "src/qa/", "../shared/kernel/qa"
PYTEST_ARGS = ("-o", "pythonpath=tests")  # the ini's own pythonpath would put the unmutated kernel before mutmut's copy


class Refused(Exception):
    def __init__(self, error: str, **details: object) -> None:
        super().__init__(error)
        self.error, self.details = error, details


def parse_modules(text: str) -> List[str]:
    names = text.split(",")
    unknown = [name for name in names if name not in MODULES]
    if unknown or len(set(names)) != len(names):
        raise Refused("unknown_module", modules=unknown or [n for n in names if names.count(n) > 1], known=list(MODULES))
    return names


def config_lines(modules: List[str]) -> List[str]:
    """``setup.cfg`` for mutmut 3.8: the kernel directory is copied whole (the modules import each other), only the named
    files are mutated, and only their unit tests run."""
    def listed(key: str, values: List[str]) -> List[str]:
        return ["%s =" % key] + ["    " + value for value in values]

    return (["[mutmut]", "source_paths = " + LINK_SOURCE]
            + listed("only_mutate", [LINK_SOURCE + MODULES[name][0][len(KERNEL):] for name in modules])
            + listed("pytest_add_cli_args", list(PYTEST_ARGS))
            + listed("pytest_add_cli_args_test_selection", [MODULES[name][1] for name in modules])
            + listed("also_copy", list(ALSO_COPY)))


def install_command() -> str:
    # A virtual environment, not ``pip --target``: some kernel tests start ``sys.executable`` with a scrubbed environment, and
    # the mutated modules import mutmut, so it must be found without PYTHONPATH.
    return ("python -m venv %s && %s/bin/pip install --disable-pip-version-check --no-cache-dir --require-hashes "
            "--only-binary=:all: %s" % (VENV, VENV, " ".join("-r " + path for path in REQUIREMENTS)))


def run_script(lines: List[str]) -> str:
    """What runs in the container after the install: the configuration, mutmut, then the converter, in one shell."""
    write = "printf '%%s\\n' %s | tee setup.cfg" % " ".join(shlex.quote(line) for line in lines)
    return " && ".join([
        "export PATH=%s/bin:$PATH HOME=%s PYTHONDONTWRITEBYTECODE=1" % (VENV, HOME), "mkdir -p %s src" % HOME, "ln -s %s %s" % (LINK_TARGET, LINK_SOURCE.rstrip("/")), write,
        "python -m mutmut run", "python3 %s --root . --out %s" % (CONVERTER, REPORT)])


def command(lines: List[str]) -> List[str]:
    return ["python3", LAUNCHER, "--role", "verifier", "--skill", "qa-mutation", "run", "--container", IMAGE,
            "--install", install_command(), "--timeout", str(TIMEOUT_SECONDS), "--report", "mutation:" + REPORT,
            "--", "sh", "-c", run_script(lines)]


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="mutate_kernel.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--modules", default=",".join(MODULES), metavar="LIST", help="comma-separated; default: all of them")
    parser.add_argument("--out", required=True, metavar="DIR", help="where setup.cfg is written")
    args = parser.parse_args(argv)
    try:
        modules = parse_modules(args.modules)
    except Refused as refused:
        print(json.dumps(dict(ok=False, error=refused.error, **refused.details), sort_keys=True))
        return 2
    lines = config_lines(modules)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "setup.cfg").write_text("\n".join(lines) + "\n", encoding="utf-8")
    words = command(lines)
    print(json.dumps(dict(ok=True, config=str(out / "setup.cfg"), modules=modules, image=IMAGE, argv=words,
                          command=" ".join(shlex.quote(word) for word in words)), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
