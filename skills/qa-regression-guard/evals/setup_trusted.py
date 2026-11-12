"""Scenario setup "F7, the project trusted": ``init`` and the user's approval to run the tests, nothing else.

Run by tools/run_skill_eval.py in the working copy; see ``_setup.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _setup  # noqa: E402

_setup.prepare(finding=False)
