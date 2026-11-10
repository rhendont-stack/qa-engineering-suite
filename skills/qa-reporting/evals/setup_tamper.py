"""Scenario tampered log: a clean targeted run, then one of its recorded logs is edited afterwards."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _prepare as prep  # noqa: E402

prep.require_clean_start()
prep.trusted_workspace()
execution = prep.run_suite("tests/test_text.py", scope="targeted")
prep.tamper(execution)
