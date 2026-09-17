from __future__ import annotations

import asyncio
from pathlib import Path

from arb.replay import replay_file

CAPTURED_DIR = Path("server/tests/fixtures/captured")

# ARB-030: committed captures stay small enough to review; longer recordings
# stay outside Git. This is "roughly" 5 MB because gzip ratios vary with
# market activity, not because the limit is soft.
MAX_CAPTURE_FIXTURE_BYTES = 5_000_000


def _captured_files() -> list[Path]:
    return sorted(CAPTURED_DIR.glob("*.jsonl*"))


def test_captured_fixtures_stay_small() -> None:
    files = _captured_files()
    assert files, "ARB-030 requires a committed capture under server/tests/fixtures/captured/"
    for path in files:
        assert path.stat().st_size <= MAX_CAPTURE_FIXTURE_BYTES, (
            f"{path.name} is {path.stat().st_size} bytes; "
            "record a shorter window instead of growing the cap"
        )


def test_committed_capture_replays_deterministically() -> None:
    for path in _captured_files():
        first = asyncio.run(replay_file(path))
        second = asyncio.run(replay_file(path))

        assert first.digest == second.digest
        assert first.transitions, f"{path.name} replays to no transitions"
        venues = {transition.exchange for transition in first.transitions}
        assert venues == {"gemini", "coinbase", "binance"}, (
            f"{path.name} covers {sorted(venues)}, not all three venues"
        )
        assert first.snapshots_consumed > 0, f"{path.name} exercises no REST snapshots"
