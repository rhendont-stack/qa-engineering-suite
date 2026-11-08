from __future__ import annotations

import subprocess
import sys

from helpers import KERNEL


def test_kernel_imports_with_stdlib_only():
    code = ("import sys, pkgutil, importlib; sys.path.insert(0, sys.argv[1]); import qa; "
            "[importlib.import_module(m.name) for m in pkgutil.walk_packages(qa.__path__, 'qa.')]")
    r = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(KERNEL)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
