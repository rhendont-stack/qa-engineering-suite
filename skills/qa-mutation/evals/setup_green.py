"""Scenario setup "F4 with its genuine pricing failure repaired, the project trusted": see ``_setup.py``.

Run by tools/run_skill_eval.py in the working copy.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _setup  # noqa: E402

_setup.prepare(repair=True)
