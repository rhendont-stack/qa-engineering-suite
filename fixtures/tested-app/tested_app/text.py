"""Text helpers."""
from __future__ import annotations

import re


def slugify(text: str) -> str:
    """Lower-case ``text`` and join its words with single hyphens."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
