from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KERNEL = REPO / "shared" / "kernel"
CORPUS = REPO / "tests" / "corpus"
LAUNCHER = REPO / "shared" / "launcher" / "qa.py"
