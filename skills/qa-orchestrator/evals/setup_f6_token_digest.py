"""Setup for the milestone 4a F6 exit case (``m4a-f6-exit.json``): runs qa-security's ``token_digest.py`` unchanged.

The harness only runs a setup script that lives in the case's own skill ``evals/`` directory, and this case's skill is
qa-orchestrator. This file loads ``skills/qa-security/evals/token_digest.py`` and calls its ``main``, so the run's ``setup.txt``
holds the same digests of the generated lab's signing key that the qa-security cases record, never the key itself. It reads
``lab/settings.py`` only and writes nothing.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "qa-security" / "evals" / "token_digest.py"


def main() -> int:
    spec = importlib.util.spec_from_file_location("qa_security_token_digest_for_m4a_exit", str(SOURCE))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return int(module.main())


if __name__ == "__main__":
    sys.exit(main())
