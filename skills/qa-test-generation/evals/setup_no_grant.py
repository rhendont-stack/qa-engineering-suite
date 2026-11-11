"""Scenario setup "F5 with TCs and no grant": the records of ``_records.py`` and nothing that lets a skill write the project.

Run by tools/run_skill_eval.py in the working copy; see ``_setup.py``.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _setup  # noqa: E402

_setup.prepare(grant=False)
