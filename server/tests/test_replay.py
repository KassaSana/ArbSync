from __future__ import annotations

from pathlib import Path

import pytest
from replay import replay_capture

FIXTURE_DIR = Path("server/tests/fixtures/synthetic")


@pytest.mark.parametrize(
    "fixture_name",
    [
        "gemini_btcusd.jsonl",
        "coinbase_btcusd.jsonl",
        "binance_btcusdt.jsonl",
    ],
)
@pytest.mark.asyncio
async def test_synthetic_replay_has_no_crashes_and_finishes_with_book(fixture_name: str) -> None:
    result = await replay_capture(FIXTURE_DIR / fixture_name)

    assert result["crashes"] == 0
    assert result["events"] >= 3
    assert result["crossed_or_gap_events"] <= 1
    assert result["has_top_of_book"] == 1
