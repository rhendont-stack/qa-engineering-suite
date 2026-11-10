from __future__ import annotations

from typing import Any, Dict


class QaError(Exception):
    """Kernel error with a stable machine-readable code."""

    def __init__(self, code: str, message: str = "", **details: Any) -> None:
        super().__init__(code if not message else "%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.details = details

    def to_json(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": dict(self.details)}
