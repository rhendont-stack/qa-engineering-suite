"""Scenario F4: a full run with a real failure and a flaky test, recorded by the kernel. No gate evaluation, no report."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _prepare as prep  # noqa: E402

prep.require_clean_start()
prep.trusted_workspace()
first = prep.run_suite()
prep.rerun(first, "tests/test_sync.py::test_sync_settles", 3)
