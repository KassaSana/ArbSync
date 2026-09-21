from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from age_skew import (
    AgeSkewBands,
    AgeSkewRecorder,
    age_skew_band_rows,
    dimension_value,
    gate_sensitivity_rows,
    route_leg_age_rows,
)
from arb.capture import read_capture
from arb.config import load_config
from arb.types import RouteAgeEvent, RouteLegAges
from episode_stats import percentile, survives_any_notional
from research import _canonical_episode_rows, analyze_capture, replay_for_research

MS = 1_000_000
FIXTURE = Path("server/tests/fixtures/captured/three_venue_150s_20260917.jsonl.gz")


def bands() -> AgeSkewBands:
    return AgeSkewBands(age_edges_ns=(100 * MS, 1_000 * MS), skew_edges_ns=(50 * MS, 500 * MS))


def ages(buy_ms: int | None, sell_ms: int | None) -> RouteLegAges:
    skew = None if buy_ms is None or sell_ms is None else abs(buy_ms - sell_ms) * MS
    return RouteLegAges(
        buy_age_ns=None if buy_ms is None else buy_ms * MS,
        sell_age_ns=None if sell_ms is None else sell_ms * MS,
        skew_ns=skew,
    )


def event(
    kind: str,
    legs: RouteLegAges,
    *,
    start_ns: int | None = 10,
    monotonic_ns: int = 0,
    spread: str | None = "0.5",
    close_reason: str | None = None,
    buy: str = "coinbase",
    sell: str = "gemini",
) -> RouteAgeEvent:
    return RouteAgeEvent(
        kind=kind,  # type: ignore[arg-type]
        pair="BTC-USD",
        buy_exchange=buy,
        sell_exchange=sell,
        start_ns=start_ns,
        monotonic_ns=monotonic_ns,
        spread_pct=None if spread is None else Decimal(spread),
        ages=legs,
        close_reason=close_reason,  # type: ignore[arg-type]
    )


def episode(
    start_ns: int,
    *,
    duration_ns: int | None = 1_000 * MS,
    peak_spread: str = "0.5",
    peak_profit: str = "1",
    net: str | None = "0.1",
    close_reason: str = "spread_closed",
    quote_asset: str = "USD",
    buy: str = "coinbase",
    sell: str = "gemini",
) -> dict[str, object]:
    return {
        "start_ns": str(start_ns),
        "pair": "BTC-USD",
        "quote_asset": quote_asset,
        "buy_exchange": buy,
        "sell_exchange": sell,
        "duration_ns": None if duration_ns is None else str(duration_ns),
        "peak_spread_pct": peak_spread,
        "peak_profit": peak_profit,
        "close_reason": close_reason,
        "pricing_ledgers": [
            {
                "notional": "100",
                "net_executable_spread_pct": net,
                "insufficient_depth": net is None,
            }
        ],
    }


def test_band_edges_are_inclusive_upper_bounds_with_an_open_tail() -> None:
    b = bands()
    assert b.band("absolute_age", 0) == 0
    assert b.band("absolute_age", 100 * MS) == 0
    assert b.band("absolute_age", 100 * MS + 1) == 1
    assert b.band("absolute_age", 1_000 * MS) == 1
    assert b.band("absolute_age", 1_000 * MS + 1) == 2
    assert b.band("absolute_age", None) is None
    assert b.band("relative_skew", 50 * MS) == 0
    assert b.band("relative_skew", 501 * MS) == 2
    assert b.bounds_ms("absolute_age", 0) == (0, 100)
    assert b.bounds_ms("absolute_age", 1) == (100, 1_000)
    assert b.bounds_ms("absolute_age", 2) == (1_000, None)


@pytest.mark.parametrize(
    "age_edges, skew_edges",
    [((), (1,)), ((1,), ()), ((0, 5), (1,)), ((5, 5), (1,)), ((5, 4), (1,)), ((1,), (-1,))],
)
def test_bands_reject_empty_nonpositive_or_unordered_edges(
    age_edges: tuple[int, ...], skew_edges: tuple[int, ...]
) -> None:
    with pytest.raises(ValueError):
        AgeSkewBands(age_edges_ns=age_edges, skew_edges_ns=skew_edges).validate()
    bands().validate()


def test_dimensions_are_the_older_leg_and_the_skew_kept_separately() -> None:
    legs = ages(30, 700)
    assert dimension_value("absolute_age", legs) == 700 * MS
    assert dimension_value("relative_skew", legs) == 670 * MS
    # Equal ages have zero skew but can still be old: the dimensions disagree.
    old_equal = ages(900, 900)
    assert dimension_value("absolute_age", old_equal) == 900 * MS
    assert dimension_value("relative_skew", old_equal) == 0
    assert dimension_value("absolute_age", ages(None, 5)) is None
    assert dimension_value("relative_skew", ages(None, 5)) is None


def test_recorder_counts_evaluations_and_follows_episode_ages() -> None:
    recorder = AgeSkewRecorder(bands())
    recorder.record(event("evaluated", ages(10, 20), start_ns=None, spread="-0.2"))
    recorder.record(event("evaluated", ages(2_000, 20), start_ns=None, spread="-0.2"))
    recorder.record(event("evaluated", ages(None, 20), start_ns=None, spread=None))
    assert recorder.evaluations["absolute_age"] == {0: 1, 2: 1, None: 1}
    assert recorder.evaluations["relative_skew"] == {0: 1, 2: 1, None: 1}

    recorder.record(event("open", ages(30, 70), monotonic_ns=5))
    # A non-peak evaluation on the open route still widens the observed skew.
    recorder.record(event("evaluated", ages(900, 100), start_ns=None, spread="0.3"))
    recorder.record(event("peak", ages(40, 60), monotonic_ns=6, spread="0.9"))
    recorder.record(
        event("close", ages(41, 61), monotonic_ns=7, spread="0.0", close_reason="spread_closed")
    )
    # An evaluation after the close belongs to no episode.
    recorder.record(event("evaluated", ages(5_000, 0), start_ns=None, spread="-1"))

    record = recorder.episodes[(10, "BTC-USD", "coinbase", "gemini")]
    assert record.open_ages == ages(30, 70)
    assert record.peak_ages == ages(40, 60)
    assert record.close_ages == ages(41, 61)
    assert record.close_reason == "spread_closed"
    assert record.max_skew_ns == 800 * MS


def test_route_leg_age_rows_join_episodes_by_key_and_never_fabricate() -> None:
    recorder = AgeSkewRecorder(bands())
    recorder.record(event("open", ages(30, 70)))
    recorder.record(event("close", ages(30, 70), close_reason="book_ineligible", spread=None))
    rows = route_leg_age_rows(
        recorder, [episode(10, close_reason="book_ineligible"), episode(99, net=None)]
    )
    assert len(rows) == 1, "an episode without recorded ages is skipped, not invented"
    row = rows[0]
    assert row["module"] == "route_leg_ages"
    assert (row["open_buy_age_ms"], row["open_sell_age_ms"], row["open_age_skew_ms"]) == (
        30,
        70,
        40,
    )
    assert row["peak_buy_age_ms"] is None
    assert (row["close_buy_age_ms"], row["close_sell_age_ms"]) == (30, 70)
    assert row["max_age_skew_ms"] == 40
    assert row["close_reason"] == "book_ineligible"
    assert row["fee_survivor"] is True
    assert row["peak_spread_pct"] == "0.5" and row["duration_ns"] == str(1_000 * MS)


def test_band_rows_group_episodes_by_open_ages_and_summarize_outcomes() -> None:
    recorder = AgeSkewRecorder(bands())
    for _ in range(4):
        recorder.record(event("evaluated", ages(10, 20), start_ns=None, spread="-0.2"))
    recorder.record(event("evaluated", ages(2_000, 20), start_ns=None, spread="-0.2"))
    # Two fresh-band episodes: one survivor, one closed by a lost leg.
    recorder.record(event("open", ages(10, 20), start_ns=1))
    recorder.record(event("close", ages(10, 20), start_ns=1, close_reason="spread_closed"))
    recorder.record(event("open", ages(10, 40), start_ns=2, buy="gemini", sell="coinbase"))
    recorder.record(
        event(
            "close",
            ages(10, 40),
            start_ns=2,
            buy="gemini",
            sell="coinbase",
            close_reason="book_ineligible",
            spread=None,
        )
    )
    # One episode whose older leg sits above the last edge, and whose skew is large.
    recorder.record(event("open", ages(3_000, 10), start_ns=3))
    recorder.record(event("close", ages(3_000, 10), start_ns=3, close_reason="spread_closed"))
    episodes = [
        episode(1, duration_ns=100 * MS, peak_spread="0.4", net="0.1"),
        episode(
            2,
            duration_ns=300 * MS,
            peak_spread="0.6",
            net="-0.1",
            close_reason="book_ineligible",
            buy="gemini",
            sell="coinbase",
        ),
        episode(3, duration_ns=None, peak_spread="2.0", net=None),
    ]
    rows = age_skew_band_rows(recorder, episodes)
    by_key = {(row["dimension"], row["band"]): row for row in rows}
    assert set(by_key) == {
        ("absolute_age", 0),
        ("absolute_age", 1),
        ("absolute_age", 2),
        ("relative_skew", 0),
        ("relative_skew", 1),
        ("relative_skew", 2),
    }
    fresh = by_key[("absolute_age", 0)]
    assert (fresh["band_lower_ms"], fresh["band_upper_ms"]) == (0, 100)
    assert fresh["evaluations"] == 4 and fresh["episodes_opened"] == 2
    assert fresh["open_rate"] == 0.5
    assert fresh["closed_book_ineligible"] == 1 and fresh["fee_survivors"] == 1
    assert fresh["duration_ms_p50"] == 100.0 and fresh["duration_ms_p90"] == 300.0
    assert fresh["duration_ms_max"] == 300.0
    assert fresh["peak_spread_pct_p50"] == 0.4 and fresh["peak_spread_pct_p90"] == 0.6
    fresh_survival = cast("list[dict[str, Any]]", fresh["survival_by_notional"])
    assert fresh_survival[0]["survivors"] == 1
    assert fresh_survival[0]["priced"] == 2
    old = by_key[("absolute_age", 2)]
    assert (old["band_lower_ms"], old["band_upper_ms"]) == (1_000, None)
    assert old["evaluations"] == 1 and old["episodes_opened"] == 1
    assert old["duration_ms_p50"] is None, "an open episode has no lifetime yet"
    old_survival = cast("list[dict[str, Any]]", old["survival_by_notional"])
    assert old_survival[0]["insufficient_depth"] == 1
    empty = by_key[("absolute_age", 1)]
    assert empty["evaluations"] == 0 and empty["open_rate"] is None
    # The same episodes land in different bands on the skew dimension.
    assert by_key[("relative_skew", 0)]["episodes_opened"] == 2
    assert by_key[("relative_skew", 2)]["episodes_opened"] == 1


def test_gate_sensitivity_reports_retained_and_rejected_per_cutoff_per_quote() -> None:
    recorder = AgeSkewRecorder(bands())
    recorder.record(event("open", ages(10, 20), start_ns=1))
    recorder.record(event("open", ages(800, 20), start_ns=2, buy="gemini", sell="coinbase"))
    recorder.record(event("open", ages(3_000, 10), start_ns=3, buy="binance", sell="gemini"))
    recorder.record(event("open", ages(None, 10), start_ns=4, buy="gemini", sell="binance"))
    episodes = [
        episode(1, peak_profit="1", net="0.1"),
        episode(
            2,
            peak_profit="2",
            net="0.2",
            close_reason="book_ineligible",
            buy="gemini",
            sell="coinbase",
        ),
        episode(3, peak_profit="4", net=None, quote_asset="USDT", buy="binance", sell="gemini"),
        episode(4, peak_profit="8", net="0.3", quote_asset="USDT", buy="gemini", sell="binance"),
    ]
    rows = {(r["dimension"], r["cutoff_ms"]): r for r in gate_sensitivity_rows(recorder, episodes)}
    assert set(rows) == {
        ("absolute_age", 100),
        ("absolute_age", 1_000),
        ("relative_skew", 50),
        ("relative_skew", 500),
    }
    strict = rows[("absolute_age", 100)]
    assert (
        strict["episodes_retained"],
        strict["episodes_rejected"],
        strict["episodes_unknown"],
    ) == (
        1,
        2,
        1,
    )
    # Episode 4 has no age, so the gate never judged it: it is not a rejected
    # survivor and its USDT profit is in no total.
    assert (strict["fee_survivors_retained"], strict["fee_survivors_rejected"]) == (1, 1)
    assert strict["retained_survivor_share"] == 0.5
    assert strict["book_ineligible_rejected"] == 1 and strict["book_ineligible_retained"] == 0
    # USD and USDT profit are never added together.
    assert strict["total_peak_profit_by_quote"] == {"USD": "3", "USDT": "4"}
    assert strict["retained_peak_profit_by_quote"] == {"USD": "1"}
    assert strict["retained_peak_profit_share_by_quote"] == {
        "USD": str(Decimal(1) / Decimal(3)),
        "USDT": "0",
    }
    loose = rows[("absolute_age", 1_000)]
    assert (loose["episodes_retained"], loose["episodes_rejected"]) == (2, 1)
    assert loose["book_ineligible_retained"] == 1
    # Skew: episode 3 has 2990 ms skew, episode 2 has 780 ms, episode 1 has 10 ms.
    assert rows[("relative_skew", 50)]["episodes_retained"] == 1
    assert rows[("relative_skew", 500)]["episodes_rejected"] == 2


def test_percentile_is_nearest_rank_and_survivor_flag_needs_a_priced_positive_net() -> None:
    assert percentile([], 0.5) is None
    assert percentile([3.0, 1.0, 2.0], 0.5) == 2.0
    assert percentile([3.0, 1.0, 2.0], 0.9) == 3.0
    assert percentile([5.0], 0.1) == 5.0
    assert survives_any_notional(episode(1, net="0.1")) is True
    assert survives_any_notional(episode(1, net="0")) is False
    assert survives_any_notional(episode(1, net=None)) is False
    insufficient = episode(1, net="0.5")
    insufficient["pricing_ledgers"][0]["insufficient_depth"] = True  # type: ignore[index]
    assert survives_any_notional(insufficient) is False


def test_fixture_replay_records_ages_for_every_episode_and_evaluation() -> None:
    """On the committed capture every route comparison and episode carries local ages."""
    config = load_config("config.toml")
    config = replace(config, detector=replace(config.detector, threshold_pct=0.0))
    header, frames = read_capture(FIXTURE)
    report, sampler, recorder = asyncio.run(replay_for_research(header, frames, config, bands()))
    episodes = _canonical_episode_rows(report)
    assert episodes, "a zero threshold must open episodes on real traffic"
    rows = route_leg_age_rows(recorder, episodes)
    assert len(rows) == len(episodes)
    for row in rows:
        assert row["open_buy_age_ms"] is not None and row["open_sell_age_ms"] is not None
        assert row["open_age_skew_ms"] == abs(row["open_buy_age_ms"] - row["open_sell_age_ms"])  # type: ignore[operator]
        assert row["close_buy_age_ms"] is not None, "shutdown closes keep last ages"
        assert row["max_age_skew_ms"] >= row["open_age_skew_ms"]  # type: ignore[operator]
    for dimension in ("absolute_age", "relative_skew"):
        counts = recorder.evaluations[dimension]
        assert None not in counts, "replayed tops always carry receipt times"
        assert sum(counts.values()) > len(episodes)
    datasets = analyze_capture(report, sampler, recorder, null_surrogates=0, sensitivity=False)
    band_rows = datasets["age_skew_bands"]
    assert isinstance(band_rows, list)
    assert sum(r["episodes_opened"] for r in band_rows if r["dimension"] == "absolute_age") == len(
        episodes
    )
    gate_rows = datasets["age_skew_gate_sensitivity"]
    assert isinstance(gate_rows, list)
    for row in gate_rows:
        assert row["episodes_retained"] + row["episodes_rejected"] == len(episodes)
    measurement = datasets["measurement"]
    assert isinstance(measurement, dict)
    assert measurement["age_bands_ms"] == [100, 1_000]
    assert measurement["skew_bands_ms"] == [50, 500]
