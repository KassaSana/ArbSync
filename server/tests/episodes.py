"""Shared builder for `OpportunityEpisode` values in tests.

Most persistence and API tests only care about when an episode started and
what it was worth, so this fills the peak fields from the open values and
leaves the episode open unless a close is requested.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

from arb.types import EpisodeCloseReason, OpportunityEpisode


def make_episode(**overrides: Any) -> OpportunityEpisode:
    fields: dict[str, Any] = dict(
        start_ns=1,
        pair="BTC-USD",
        buy_exchange="gemini",
        sell_exchange="coinbase",
        buy_price=Decimal("100"),
        sell_price=Decimal("103"),
        spread_pct=Decimal("3"),
        max_size=Decimal("1"),
        theoretical_profit=Decimal("3"),
    )
    fields.update(overrides)
    fields.setdefault("quote_asset", str(fields["pair"]).rsplit("-", maxsplit=1)[1])
    fields.setdefault("peak_spread_pct", fields["spread_pct"])
    fields.setdefault("peak_size", fields["max_size"])
    fields.setdefault("peak_profit", fields["theoretical_profit"])
    return OpportunityEpisode(**fields)


def close_episode(
    episode: OpportunityEpisode,
    *,
    duration_ns: int,
    reason: EpisodeCloseReason = "spread_closed",
    peak_spread_pct: Decimal | None = None,
    peak_size: Decimal | None = None,
    peak_profit: Decimal | None = None,
    close_spread_pct: Decimal | None = Decimal("0"),
) -> OpportunityEpisode:
    """The close event for `episode`, optionally with a higher peak than it opened at."""
    return replace(
        episode,
        end_ns=episode.start_ns + duration_ns,
        close_reason=reason,
        close_spread_pct=close_spread_pct,
        peak_spread_pct=episode.peak_spread_pct if peak_spread_pct is None else peak_spread_pct,
        peak_size=episode.peak_size if peak_size is None else peak_size,
        peak_profit=episode.peak_profit if peak_profit is None else peak_profit,
    )
