"""Scenario F9: the project's zero-tests runner exits 0 and its report holds no tests; the kernel records that run."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _prepare as prep  # noqa: E402

prep.require_clean_start()
prep.trusted_workspace()
prep.kernel("run", "--scope", "full", "--report", "junit:report.xml", "--", sys.executable, "runners/zero_tests.py", "report.xml")
