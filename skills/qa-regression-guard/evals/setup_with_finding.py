"""Scenario setup "F7 at the fix, the defect recorded": the trusted project plus the one finding the fix is about.

Run by tools/run_skill_eval.py in the working copy; see ``_setup.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _setup  # noqa: E402

_setup.prepare(finding=True)
