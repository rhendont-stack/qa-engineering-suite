from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys

from helpers import LAUNCHER, REPO


def _load_launcher():
    spec = importlib.util.spec_from_file_location("qa_launcher", str(LAUNCHER))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launcher = _load_launcher()


# --------------------------------------------------------------------------- the brief's tests

def test_cwd_json_py_does_not_shadow_stdlib(tmp_path):
    (tmp_path / "json.py").write_text("raise SystemExit('shadowed')")
    r = subprocess.run([sys.executable, str(LAUNCHER), "--workspace", str(tmp_path), "init"], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0 and json.loads(r.stdout)["ok"]
def test_check_version_message():
    assert launcher.check_version((3, 8, 10)) == "qa needs Python 3.9 or newer; found 3.8"
    assert launcher.check_version((3, 9, 0)) is None
def test_launcher_writes_no_bytecode(tmp_path):
    copy = tmp_path / "shared"; shutil.copytree(REPO / "shared", copy, ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "w").mkdir()
    env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
    subprocess.run([sys.executable, str(copy / "launcher" / "qa.py"), "--workspace", str(tmp_path / "w"), "init"], check=True, capture_output=True, env=env)
    assert not list(copy.rglob("__pycache__"))
def test_launcher_parses_on_old_interpreters():
    tree = ast.parse(LAUNCHER.read_text(encoding="utf-8"), feature_version=(3, 4))
    newer = [n for n in ast.walk(tree) if isinstance(n, (ast.JoinedStr, ast.AnnAssign, ast.NamedExpr))
             or (isinstance(n, ast.ImportFrom) and n.module == "__future__") or (isinstance(n, ast.arg) and n.annotation)
             or (isinstance(n, ast.FunctionDef) and n.returns)]
    assert newer == []


# --------------------------------------------------------------------------- where the kernel is found (T18-R15)

def _stub_kernel(root):
    """A kernel whose ``main`` prints what the launcher left at ``sys.path[0]``."""
    (root / "qa").mkdir(parents=True)
    (root / "qa" / "__init__.py").write_text("")
    (root / "qa" / "cli.py").write_text("import json, sys\n\n\ndef main():\n    print(json.dumps(sys.path[0]))\n    return 0\n")


def _launch_copy(directory):
    directory.mkdir(parents=True, exist_ok=True)
    copy = directory / "qa.py"
    shutil.copy(str(LAUNCHER), str(copy))
    done = subprocess.run([sys.executable, str(copy)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_kernel_comes_first_on_the_path(tmp_path):  # the launcher is itself named qa.py and would shadow the package
    _stub_kernel(tmp_path / "shared" / "kernel")
    assert _launch_copy(tmp_path / "shared" / "launcher") == os.path.realpath(str(tmp_path / "shared" / "kernel"))


def test_a_kernel_installed_beside_the_launcher_wins(tmp_path):
    _stub_kernel(tmp_path / "shared" / "kernel")
    _stub_kernel(tmp_path / "shared" / "launcher" / "_kernel")
    assert _launch_copy(tmp_path / "shared" / "launcher") == os.path.realpath(str(tmp_path / "shared" / "launcher" / "_kernel"))
