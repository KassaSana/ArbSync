from __future__ import annotations

import sqlite3
from pathlib import Path

from perf_database import persisted_episode_count


def test_persisted_episode_count_uses_current_schema_table(tmp_path: Path) -> None:
    database = tmp_path / "profile.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE opportunity_episodes (start_ns INTEGER PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO opportunity_episodes (start_ns) VALUES (?)",
            [(1,), (2,), (3,)],
        )

    assert persisted_episode_count(database) == 3
