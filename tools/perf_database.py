"""Lightweight SQLite helpers shared by performance harnesses."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def persisted_episode_count(database: Path) -> int:
    """Return the number of canonical opportunity episodes in a benchmark database."""
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT COUNT(*) FROM opportunity_episodes").fetchone()
    if row is None:  # pragma: no cover - COUNT always returns one row
        raise RuntimeError("episode count query returned no row")
    return int(row[0])
