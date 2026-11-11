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
DEFAULT = "status,gate,ledger,canonical,compare,trace"


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
    assert cfg["only_mutate"] == ["src/qa/status.py", "src/qa/report/check.py"]
    assert cfg["pytest_add_cli_args_test_selection"] == ["tests/unit/test_status.py", "tests/unit/test_report.py"]
    assert cfg["source_paths"] == ["src/qa/"]
    for name in ("gate", "ledger", "canonical", "compare", "trace"):
        assert name not in " ".join(sum(cfg.values(), [])).replace("test_report", "")
    # what the config names exists, so a renamed module or test is noticed here and not after hours of mutation testing
    for path in cfg["only_mutate"] + cfg["pytest_add_cli_args_test_selection"]:
        assert (REPO / path.replace("src/qa/", "shared/kernel/qa/")).is_file(), path


def test_default_is_the_six_measurable_modules_and_report_check_stays_selectable(tmp_path):
    code, summary, _ = mutate(tmp_path)
    assert code == 0 and summary["modules"] == DEFAULT.split(",") and "report/check" not in summary["modules"]
    code, summary, _ = mutate(tmp_path / "all", "--modules", ALL)
    assert code == 0 and summary["modules"] == ALL.split(",")
    assert "report_invalid" in subprocess.run([sys.executable, "-I", str(TOOL), "--help"], capture_output=True, text=True).stdout


def test_every_listed_module_has_its_own_source_and_test(tmp_path):
    code, summary, _ = mutate(tmp_path, "--modules", ALL)
    assert code == 0 and summary["modules"] == ALL.split(",")
    cfg = config_of(tmp_path)
    assert len(cfg["only_mutate"]) == 7 and len(set(cfg["only_mutate"])) == 7
    assert all((REPO / path.replace("src/qa/", "shared/kernel/qa/")).is_file()
               for path in cfg["only_mutate"] + cfg["pytest_add_cli_args_test_selection"])


def test_unknown_module_is_refused(tmp_path):
    out = tmp_path / "cfg"
    for bad in ("status,nonesuch", "../status", "report", ""):
        code, summary, done = mutate(out, "--modules", bad)
        assert code == 2 and summary["ok"] is False and summary["error"] == "unknown_module", (bad, done.stdout, done.stderr)
        assert not out.exists(), "a refused request writes nothing"
    assert mutate(out, "--modules", "status,nonesuch")[1]["modules"] == ["nonesuch"]


def test_duplicate_module_is_refused_once_per_name(tmp_path):
    out = tmp_path / "cfg"
    code, summary, _ = mutate(out, "--modules", "status,gate,status,gate,status")
    assert code == 2 and summary["error"] == "duplicate_module" and summary["modules"] == ["status", "gate"]
    assert not out.exists()


def test_printed_command_pins_the_image_by_digest(tmp_path):
    code, summary, _ = mutate(tmp_path, "--modules", ALL)
    assert code == 0
    command = summary["command"]
    argv = shlex.split(command)
    assert argv == summary["argv"]
    image = argv[argv.index("--container") + 1]
    assert re.fullmatch(r"python@sha256:[0-9a-f]{64}", image) and summary["image"] == image
    assert "--install" in argv and "/bin/pip install " in argv[argv.index("--install") + 1]
    assert "--require-hashes" in argv[argv.index("--install") + 1]
    # one run does both: mutmut, then the converter, whose output is the declared report of that same execution
    assert argv[argv.index("--report") + 1] == "mutation:{evidence}/kernel-mutation.json"
    assert argv[argv.index("--timeout") + 1] == "14400"
    tail = argv[argv.index("--") + 1:]
    assert tail[:2] == ["sh", "-c"] and len(tail) == 3
    assert "mutmut run" in tail[2] and "mutmut_to_report.py" in tail[2] and "{evidence}/kernel-mutation.json" in tail[2]
    assert argv[:2] == ["python3", "skills/qa-mutation/scripts/qa.py"]
    assert argv[argv.index("--role") + 1] == "verifier" and argv.index("run") == argv.index("--skill") + 2
    # everything the command names exists, so a rename is noticed here and not on the remote engine
    named = [argv[1], "skills/qa-mutation/scripts/mutmut_to_report.py"] + re.findall(r"-r (\S+)", argv[argv.index("--install") + 1])
    assert len(named) == 4 and all((REPO / path).is_file() for path in named), named
    # the run writes no configuration: the one it reads is the untracked setup.cfg of the project copy
    assert "setup.cfg" not in tail[2] and "tee" not in tail[2] and ">" not in tail[2].replace("{evidence}", "")
    assert (tmp_path / "setup.cfg").is_file()


def pins(path):
    return dict(re.findall(r"^([A-Za-z0-9_.-]+)==(\S+)", (REPO / path).read_text(), re.M))


def test_corpus_requirements_pin_the_same_versions_as_requirements_mutation():
    """The kernel leg installs the corpus producer's file because requirements-mutation.txt holds macOS-arm64 hashes only."""
    mutation = pins("requirements-mutation.txt")
    assert len(mutation) >= 10 and mutation["mutmut"] and mutation["pytest"] and mutation["coverage"]
    corpus = pins("tests/corpus/producers/mutmut_mutation/requirements.txt")
    assert {name.lower().replace("_", "-"): v for name, v in corpus.items()} == \
        {name.lower().replace("_", "-"): v for name, v in mutation.items()}


def test_pytest_arguments_are_one_per_line(tmp_path):
    """mutmut passes each line of a list option as one argument: ``-o pythonpath=tests`` on one line would be a single bogus one."""
    mutate(tmp_path)
    assert config_of(tmp_path)["pytest_add_cli_args"] == ["-o", "pythonpath=tests"]


def test_converter_reads_only_the_mutated_kernel(tmp_path):
    """``mutants/`` holds a copy of ``tests/``, with the corpus captures' own .meta files: the converter must not scan them."""
    script = mutate(tmp_path)[1]["argv"][-1]
    assert "mutmut_to_report.py --root src --mutants mutants/src --out {evidence}/kernel-mutation.json" in script
