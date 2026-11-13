"""Applies the ``NNNN_<name>.up.sql`` and ``NNNN_<name>.down.sql`` files of a directory to an open connection."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import List

NAME = re.compile(r"\d{4}_[a-z_]+\.(up|down)\.sql")


def migration_files(directory: Path, kind: str) -> List[Path]:
    """The ``kind`` (``up`` or ``down``) files of ``directory`` in the order they are applied: oldest first for ``up``, newest first for ``down``."""
    if kind not in ("up", "down"):
        raise ValueError("kind must be 'up' or 'down'")
    found = sorted(path for path in Path(directory).iterdir() if NAME.fullmatch(path.name) and path.name.endswith("." + kind + ".sql"))
    return found if kind == "up" else found[::-1]


def apply(connection: sqlite3.Connection, directory: Path, kind: str) -> List[str]:
    """Runs every ``kind`` file of ``directory`` that exists and returns their names."""
    names = []
    for path in migration_files(directory, kind):
        connection.executescript(path.read_text(encoding="utf-8"))
        names.append(path.name)
    return names
