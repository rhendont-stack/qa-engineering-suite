"""tools/mutate_kernel.py: the mutmut configuration for the kernel's core modules, and the one command line that runs the leg.

The tool runs nothing itself. It writes ``setup.cfg`` for mutmut 3.x into ``--out`` and prints the ``qa run --container``
command; the measured score comes only from the recorded run. These tests check what the tool writes and prints, on the real
repository, as the tool is run (``python -I``).
"""
from __future__ import annotations

import configparser
import json
import re
import shlex
import subprocess
import sys

from helpers import REPO

TOOL = REPO / "tools" / "mutate_kernel.py"
ALL = "status,gate,ledger,canonical,compare,report/check,trace"


def mutate(out, *args):
    done = subprocess.run([sys.executable, "-I", str(TOOL), "--out", str(out)] + list(args), capture_output=True, text=True,
                          timeout=60, cwd=str(REPO))
    return done.returncode, (json.loads(done.stdout) if done.stdout.strip() else None), done


def config_of(out):
    parser = configparser.ConfigParser()
    parser.read(str(out / "setup.cfg"))
    section = parser["mutmut"]
    return {key: [line for line in section[key].split("\n") if line] for key in section}


def test_config_names_only_the_listed_modules(tmp_path):
    code, summary, _ = mutate(tmp_path, "--modules", "status,report/check")
    assert code == 0 and summary["ok"] is True and summary["modules"] == ["status", "report/check"]
    cfg = config_of(tmp_path)
    assert cfg["only_mutate"] == ["shared/kernel/qa/status.py", "shared/kernel/qa/report/check.py"]
    assert cfg["pytest_add_cli_args_test_selection"] == ["tests/unit/test_status.py", "tests/unit/test_report.py"]
    assert cfg["source_paths"] == ["shared/kernel/qa/"]
    for name in ("gate", "ledger", "canonical", "compare", "trace"):
        assert name not in " ".join(sum(cfg.values(), [])).replace("test_report", "")
    # what the config names exists, so a renamed module or test is noticed here and not after hours of mutation testing
    for path in cfg["only_mutate"] + cfg["pytest_add_cli_args_test_selection"]:
        assert (REPO / path).is_file(), path


def test_every_default_module_has_its_own_source_and_test(tmp_path):
    code, summary, _ = mutate(tmp_path, "--modules", ALL)
    assert code == 0 and summary["modules"] == ALL.split(",")
    cfg = config_of(tmp_path)
    assert len(cfg["only_mutate"]) == 7 and len(set(cfg["only_mutate"])) == 7
    assert all((REPO / path).is_file() for path in cfg["only_mutate"] + cfg["pytest_add_cli_args_test_selection"])


def test_unknown_module_is_refused(tmp_path):
    out = tmp_path / "cfg"
    for bad in ("status,nonesuch", "../status", "report", "status,status", ""):
        code, summary, done = mutate(out, "--modules", bad)
        assert code == 2 and summary["ok"] is False and summary["error"] == "unknown_module", (bad, done.stdout, done.stderr)
        assert not out.exists(), "a refused request writes nothing"
    assert "nonesuch" in mutate(out, "--modules", "status,nonesuch")[1]["modules"]


def test_printed_command_pins_the_image_by_digest(tmp_path):
    code, summary, _ = mutate(tmp_path, "--modules", ALL)
    assert code == 0
    command = summary["command"]
    argv = shlex.split(command)
    assert argv == summary["argv"]
    image = argv[argv.index("--container") + 1]
    assert re.fullmatch(r"python@sha256:[0-9a-f]{64}", image) and summary["image"] == image
    assert "--install" in argv and argv[argv.index("--install") + 1].startswith("pip install ")
    assert "--require-hashes" in argv[argv.index("--install") + 1]
    # one run does both: mutmut, then the converter, whose output is the declared report of that same execution
    assert argv[argv.index("--report") + 1] == "mutation:{evidence}/kernel-mutation.json"
    assert argv[argv.index("--timeout") + 1] == "14400"
    tail = argv[argv.index("--") + 1:]
    assert tail[:2] == ["sh", "-c"] and len(tail) == 3
    assert "mutmut run" in tail[2] and "mutmut_to_report.py" in tail[2] and "{evidence}/kernel-mutation.json" in tail[2]
    assert "--role" in argv and "run" in argv
    # the configuration the run uses is the one written to --out
    for line in (tmp_path / "setup.cfg").read_text().splitlines():
        assert shlex.quote(line) in tail[2]


def test_setup_cfg_is_written_without_a_shell_redirect(tmp_path):
    """The guard denies a ``>`` onto a file outside ``.qa/work/`` before it can tell the file is in a container copy."""
    tail = mutate(tmp_path)[1]["argv"][-1]
    assert "tee setup.cfg" in tail and "> setup.cfg" not in tail and ">" not in tail.replace("{evidence}", "")
