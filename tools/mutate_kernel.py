"""Configures mutation testing of the kernel's core modules and prints the command that measures it.

Usage: python3 -I tools/mutate_kernel.py [--modules status,gate,ledger,canonical,compare,trace] --out DIR

Writes ``DIR/setup.cfg``, a mutmut 3.x configuration that mutates only the named modules of ``shared/kernel/qa/`` and runs
only their unit test files, and prints one JSON object: ``ok``, ``config``, ``modules``, ``image``, ``argv`` and ``command``
(``argv`` as one shell line). The command is a ``qa run --container`` of the suite's own launcher, to run from the repository
root with ``--out .``: the run's project copy holds the files git lists as tracked or as untracked and not ignored
(``qa.gitinfo.project_files``), so the untracked ``setup.cfg`` reaches the container with no write at run time, and mutmut reads
it from its working directory. Never commit it. The command installs the pinned tools in a first container step (the only one
with a network), runs mutmut and then ``skills/qa-mutation/scripts/mutmut_to_report.py`` in that one execution, and declares
the converted file as the run's ``mutation`` report. The score is therefore in that execution's evidence manifest, and it is
read from there: this tool runs nothing and gates nothing.

``report/check`` can be named, but is not in the default: the leg that included it ended ``report_invalid``
(EXE-01M75E9WGZFMNGNSTYSV67RH4N), because the kernel's byte-level redaction cut through the stored JSON report. The final
review changed how a JSON report is redacted (value by value, then serialised again); that leg has not been run again. An
unknown or repeated module is refused (exit 2) before anything is written.

An existing ``DIR/setup.cfg`` that differs from the one this tool would write is refused (exit 2, ``config_exists``) unless
``--force`` is given, so a tracked or hand-written file is never overwritten by accident. With ``--out .`` the file sits at the
repository root, untracked: it must not be committed, and it should be deleted once the run is recorded.
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
DEFAULT_MODULES = ("status", "gate", "ledger", "canonical", "compare", "trace")  # report/check: see the module docstring
# python:3.12-slim (Python 3.12.16), the index digest the mutmut corpus capture was produced in (python:3-slim is 3.15, for
# which pyyaml 6.0.3 has no wheel).
IMAGE = "python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
LAUNCHER = "skills/qa-mutation/scripts/qa.py"
CONVERTER = "skills/qa-mutation/scripts/mutmut_to_report.py"
REPORT = "{evidence}/kernel-mutation.json"
TIMEOUT_SECONDS = 4 * 3600
# The kernel's own test tools, then mutmut's. requirements-mutation.txt holds one macOS-arm64 (cp312) wheel hash per package,
# which the Linux container cannot install with --require-hashes; the corpus producer's file pins the same versions (a test
# checks it) with the hash of every wheel PyPI lists.
REQUIREMENTS = ("requirements-dev.txt", "tests/corpus/producers/mutmut_mutation/requirements.txt")
# What the tests read next to the kernel: mutmut runs them in ``mutants/``, a copy of the paths it is told to take.
# ``qa.resource_dir`` and ``integrity.kernel_integrity`` read ``src/qa`` as a source checkout when ``<kind>`` sits next to
# ``src``, at the project root (``src/<kind>`` would make it look vendored, with its manifest missing).
RESOURCES = ("schemas", "policies", "templates")
ALSO_COPY = (("pytest.ini", "fixtures", "shared/kernel", "shared/launcher", "shared/policies", "shared/schemas",
              "shared/templates") + RESOURCES)
# mutmut names a mutant after its file's path with a leading ``src.`` removed, and the tests import the kernel as ``qa``
# (pytest.ini puts shared/kernel on the path), so the run mutates the kernel through ``src/qa``, a link made in the project
# copy. The unmutated kernel stays in ``mutants/shared/kernel`` for the tests that start it in a subprocess.
LINK_SOURCE, LINK_TARGET = "src/qa/", "../shared/kernel/qa"
# The ini's own pythonpath would put the unmutated kernel before mutmut's copy.
PYTEST_ARGS = ("-o", "pythonpath=tests")
VENV, HOME = "/work/venv", "/work/home"
# The converter takes ``mutants/src`` only: ``mutants/`` also holds the corpus captures of ``tests/``, which are not this run's.
CONVERTED_FROM = "--root src --mutants mutants/src"


class Refused(Exception):
    def __init__(self, error: str, **details: object) -> None:
        super().__init__(error)
        self.error, self.details = error, details


def parse_modules(text: str) -> List[str]:
    names = text.split(",")
    unknown = [name for name in names if name not in MODULES]
    if unknown:
        raise Refused("unknown_module", modules=unknown, known=list(MODULES))
    repeated = list(dict.fromkeys(name for name in names if names.count(name) > 1))
    if repeated:
        raise Refused("duplicate_module", modules=repeated)
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


def run_script() -> str:
    """What runs in the container after the install: links that give the kernel its layout, mutmut, then the converter. The
    configuration is not written here: it is the untracked ``setup.cfg`` the project copy already holds."""
    return " && ".join([
        "export PATH=%s/bin:$PATH HOME=%s PYTHONDONTWRITEBYTECODE=1" % (VENV, HOME),
        "mkdir -p %s src" % HOME,
        "ln -s %s %s" % (LINK_TARGET, LINK_SOURCE.rstrip("/")),
        " && ".join("ln -s shared/%s %s" % (kind, kind) for kind in RESOURCES),
        "python -m mutmut run",
        "python3 %s %s --out %s" % (CONVERTER, CONVERTED_FROM, REPORT)])


def command() -> List[str]:
    return ["python3", LAUNCHER, "--role", "verifier", "--skill", "qa-mutation", "run", "--container", IMAGE,
            "--install", install_command(), "--timeout", str(TIMEOUT_SECONDS), "--report", "mutation:" + REPORT,
            "--", "sh", "-c", run_script()]


UNTRACKED_NOTE = ("setup.cfg is written for this run only: at the repository root (--out .) it is untracked, must not be "
                  "committed, and should be deleted once the run is recorded")


def _refuse_existing(target: Path, data: bytes, force: bool) -> None:
    """An existing file that differs, or anything there that is not a regular file, is kept unless ``force``."""
    if not (target.exists() or target.is_symlink()):
        return
    same = target.is_file() and not target.is_symlink() and target.read_bytes() == data
    if not same and not force:
        raise Refused("config_exists", config=str(target),
                      detail="a different setup.cfg is already there; it is never overwritten without --force")
    if not same and (target.is_symlink() or not target.is_file()):
        raise Refused("config_not_a_file", config=str(target), detail="setup.cfg there is a link or not a regular file")


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="mutate_kernel.py", description=__doc__.split("\n\n")[0], epilog=UNTRACKED_NOTE + ".")
    parser.add_argument("--modules", default=",".join(DEFAULT_MODULES), metavar="LIST",
                        help="comma-separated; default: %s. report/check can be named, but the leg that included it ended "
                             "report_invalid (EXE-01M75E9WGZFMNGNSTYSV67RH4N) through a redaction defect the final review "
                             "fixed; that leg has not been run again"
                             % ",".join(DEFAULT_MODULES))
    parser.add_argument("--out", required=True, metavar="DIR",
                        help="where setup.cfg is written; the repository root (.) for the run to pick it up")
    parser.add_argument("--force", action="store_true",
                        help="overwrite a setup.cfg that is already there and differs")
    args = parser.parse_args(argv)
    out = Path(args.out)
    try:
        modules = parse_modules(args.modules)
        data = ("\n".join(config_lines(modules)) + "\n").encode("utf-8")
        _refuse_existing(out / "setup.cfg", data, args.force)
    except Refused as refused:
        print(json.dumps(dict(ok=False, error=refused.error, **refused.details), sort_keys=True))
        return 2
    out.mkdir(parents=True, exist_ok=True)
    (out / "setup.cfg").write_bytes(data)
    words = command()
    print(json.dumps(dict(ok=True, config=str(out / "setup.cfg"), modules=modules, image=IMAGE, argv=words,
                          command=" ".join(shlex.quote(word) for word in words), note=UNTRACKED_NOTE), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
