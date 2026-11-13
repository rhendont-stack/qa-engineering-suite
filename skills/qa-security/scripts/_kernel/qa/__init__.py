from __future__ import annotations

from pathlib import Path

from .errors import QaError

KERNEL_VERSION = "0.1.0-dev"
SCHEMA_VERSION = "1"


def resource_dir(kind: str) -> Path:
    """Return the shared resource directory ``kind``.

    Vendored layout: ``_kernel/qa`` next to ``_kernel/<kind>``.
    Source layout: ``shared/kernel/qa`` with ``shared/<kind>``.
    """
    package_parent = Path(__file__).resolve().parent.parent
    for candidate in (package_parent / kind, package_parent.parent / kind):
        if candidate.is_dir():
            return candidate
    raise QaError("resource_missing", "resource directory not found", kind=kind)
