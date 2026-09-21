from __future__ import annotations

from decimal import Decimal

import pytest
from arb.orderbook import OrderBookManager
from arb.pricing import DepthFill, DepthSampler, pricing_ledger
from arb.types import EventKind, MarketEvent, PriceLevel, TopOfBook
from net_intervals import (
    DEPTH_WINDOW_LEVELS,
    NetInterval,
    NetKey,
    NetSignalRecorder,
    NetSignalRow,
    NetVariant,
    add_theoretical_overlap,
    build_net_datasets,
    halved_fees,
    intervalize_signals,
    net_interval_row,
    net_sensitivity_variants,
)

KEY: NetKey = ("BTC-USD", "coinbase", "gemini", Decimal("100"))
FEES = {"coinbase": Decimal("0.6"), "gemini": Decimal("0.4")}


def _row(
    mono_ns: int,
    net: str | None,
    state: str = "priced",
    *,
    proceeds: str | None = None,
    skew_ns: int | None = 0,
) -> NetSignalRow:
    """A hand-built signal row; priced rows carry fills consistent with `proceeds`."""
    priced = state == "priced"
    value = None if net is None else Decimal(net)
    sold = Decimal(proceeds) if proceeds is not None else None
    return NetSignalRow(
        pair=KEY[0],
        buy_exchange=KEY[1],
        sell_exchange=KEY[2],
        notional=KEY[3],
        mono_ns=mono_ns,
        wall_ns=mono_ns,
        state=state,
        gross_spread_pct=(
            None if not priced else (value if sold is None else (sold - 100) / 100 * 100)
        ),
        net_spread_pct=value if priced else None,
        executable_base=Decimal("0.001") if priced else None,
        buy_cost_quote=Decimal("100") if priced else None,
        sell_proceeds_quote=(Decimal("101") if sold is None else sold) if priced else None,
        buy_age_ns=0 if priced else None,
        sell_age_ns=0 if priced else None,
        skew_ns=skew_ns if priced else None,
    )


def _intervals(
    rows: list[NetSignalRow],
    *,
    threshold: str = "0",
    hysteresis: str = "0",
    delay_ns: int = 0,
    end_ns: int,
    fees: dict[str, Decimal] | None = None,
) -> list[NetInterval]:
    return intervalize_signals(
        {KEY: rows},
        threshold_pct=Decimal(threshold),
        hysteresis_pct=Decimal(hysteresis),
        delay_ns=delay_ns,
        end_mono_ns=end_ns,
        end_wall_ns=end_ns,
        fees=fees,
    )


def test_spread_close_tracks_peak_and_last_open_value() -> None:
    [interval] = _intervals(
        [_row(1, "0.5"), _row(2, "1.2"), _row(3, "0.8"), _row(4, "-0.1")], end_ns=5
    )
    assert interval.close_reason == "spread_closed"
    assert interval.peak_net_spread_pct == Decimal("1.2")
    # Terminal is the last value seen while open, not the value that closed it.
    assert interval.terminal_net_spread_pct == Decimal("0.8")
    assert (interval.open_mono_ns, interval.close_mono_ns, interval.duration_ns) == (1, 4, 3)


def test_threshold_opens_strictly_above_and_reopens_after_close() -> None:
    rows = [_row(1, "0.1"), _row(2, "0.3"), _row(3, "0.1"), _row(4, "0.4"), _row(5, "-1")]
    intervals = _intervals(rows, threshold="0.1", end_ns=6)
    assert [(i.open_mono_ns, i.close_mono_ns) for i in intervals] == [(2, 3), (4, 5)]


def test_hysteresis_band_keeps_the_interval_open() -> None:
    [interval] = _intervals(
        [_row(1, "0.5"), _row(2, "0.05"), _row(3, "-0.2")], hysteresis="0.1", end_ns=4
    )
    assert interval.close_reason == "spread_closed"
    assert interval.terminal_net_spread_pct == Decimal("0.05")
    assert interval.duration_ns == 2


def test_insufficient_and_invalidated_closes_are_distinct() -> None:
    [thin] = _intervals([_row(1, "0.4"), _row(2, None, "insufficient_depth")], end_ns=3)
    assert (thin.close_reason, thin.ineligibility_reason) == ("insufficient_depth", None)

    [lost] = _intervals([_row(1, "0.4"), _row(2, None, "ineligible:stale")], end_ns=3)
    assert (lost.close_reason, lost.ineligibility_reason) == ("invalidated", "stale")
    assert lost.terminal_net_spread_pct == Decimal("0.4")


def test_end_of_capture_closes_at_the_last_frame() -> None:
    [interval] = _intervals([_row(10, "0.3"), _row(20, "0.4")], end_ns=30)
    assert interval.close_reason == "end_of_capture"
    assert (interval.close_mono_ns, interval.close_wall_ns) == (30, 30)
    assert interval.terminal_net_spread_pct == Decimal("0.4")
    assert interval.close_executable_quote == Decimal("100")


def test_delay_drops_intervals_that_close_first_and_shifts_the_rest() -> None:
    assert _intervals([_row(0, "0.5"), _row(5, "-1")], delay_ns=10, end_ns=6) == []
    # Closing exactly at t + delay is not "still open at t + delay".
    assert _intervals([_row(0, "0.5"), _row(10, "-1")], delay_ns=10, end_ns=11) == []

    [interval] = _intervals(
        [_row(0, "0.9", skew_ns=5), _row(20, "0.6", skew_ns=9), _row(30, "-1")],
        delay_ns=10,
        end_ns=31,
    )
    assert (interval.open_mono_ns, interval.close_mono_ns, interval.duration_ns) == (10, 30, 20)
    # Change-only rows: at t=10 the t=0 value is still in force, so it is the
    # opening value and counts toward the peak.
    assert interval.peak_net_spread_pct == Decimal("0.9")
    assert interval.open_skew_ns == 5
    assert interval.max_skew_ns == 9


def test_delay_uses_the_value_in_force_when_no_row_falls_inside() -> None:
    [interval] = _intervals([_row(0, "0.5"), _row(30, "-1")], delay_ns=10, end_ns=31)
    assert interval.open_mono_ns == 10
    assert interval.peak_net_spread_pct == Decimal("0.5")
    assert interval.terminal_net_spread_pct == Decimal("0.5")


def test_variant_fees_renet_recorded_fills_through_the_pricing_formula() -> None:
    # Gross 1.5% on a 100 quote budget; net at 0.6 + 0.4 fees is below 0.8%.
    ledger = pricing_ledger(
        notional=Decimal("100"),
        buy_top=_top("coinbase", "99", "100"),
        sell_top=_top("gemini", "101.5", "102"),
        buy_fill=_fill(Decimal("100"), Decimal("1")),
        sell_fill=_fill(Decimal("101.5"), Decimal("1")),
        buy_taker_fee_pct=FEES["coinbase"],
        sell_taker_fee_pct=FEES["gemini"],
    )
    assert ledger.net_executable_spread_pct is not None
    rows = [
        _row(1, str(ledger.net_executable_spread_pct), proceeds="101.5"),
        _row(2, "-1", proceeds="99"),
    ]
    assert _intervals(rows, threshold="0.8", end_ns=3) == []

    [halved] = _intervals(rows, threshold="0.8", end_ns=3, fees=halved_fees(FEES))
    expected = pricing_ledger(
        notional=Decimal("100"),
        buy_top=_top("coinbase", "99", "100"),
        sell_top=_top("gemini", "101.5", "102"),
        buy_fill=_fill(Decimal("100"), Decimal("1")),
        sell_fill=_fill(Decimal("101.5"), Decimal("1")),
        buy_taker_fee_pct=FEES["coinbase"] / 2,
        sell_taker_fee_pct=FEES["gemini"] / 2,
    )
    assert halved.peak_net_spread_pct == expected.net_executable_spread_pct


def test_rejects_negative_hysteresis_and_delay() -> None:
    with pytest.raises(ValueError):
        _intervals([_row(1, "0.5")], hysteresis="-0.1", end_ns=2)
    with pytest.raises(ValueError):
        _intervals([_row(1, "0.5")], delay_ns=-1, end_ns=2)


def test_interval_row_serializes_decimal_strings() -> None:
    [interval] = _intervals([_row(1, "0.25"), _row(2, "-0.5")], end_ns=3)
    variant = NetVariant("baseline", Decimal("0"), Decimal("0"), 0, "configured")
    row = net_interval_row(interval, module="net_interval", variant=variant)
    assert row["peak_net_spread_pct"] == "0.25"
    assert row["terminal_net_spread_pct"] == "0.25"
    assert row["notional"] == "100"
    assert row["duration_ns"] == "1"
    assert row["open_executable_base"] == "0.001"
    assert row["open_executable_quote"] == "100"
    assert (row["threshold_pct"], row["hysteresis_pct"], row["delay_ns"]) == ("0", "0", "0")
    assert row["fee_mode"] == "configured"
    assert all(not isinstance(value, float) for value in row.values())


def test_overlap_join_reports_start_state_and_coverage() -> None:
    rows: list[dict[str, object]] = [
        {
            "pair": "BTC-USD",
            "buy_exchange": "coinbase",
            "sell_exchange": "gemini",
            "open_wall_ns": "10",
            "close_wall_ns": "20",
        },
        {
            "pair": "BTC-USD",
            "buy_exchange": "gemini",
            "sell_exchange": "coinbase",
            "open_wall_ns": "10",
            "close_wall_ns": "20",
        },
    ]
    episodes: list[dict[str, object]] = [
        {
            "pair": "BTC-USD",
            "buy_exchange": "coinbase",
            "sell_exchange": "gemini",
            "start_ns": "5",
            "end_ns": "15",
        },
        {
            "pair": "BTC-USD",
            "buy_exchange": "coinbase",
            "sell_exchange": "gemini",
            "start_ns": "18",
            "end_ns": None,
        },
    ]
    covered, other_route = add_theoretical_overlap(rows, episodes)
    assert covered["theoretical_open_at_start"] is True
    # 10-15 from the first episode plus 18-20 from the still-open one.
    assert covered["theoretical_coverage_fraction"] == "0.700000"
    assert other_route["theoretical_open_at_start"] is False
    assert other_route["theoretical_coverage_fraction"] == "0.000000"


def test_sensitivity_variants_are_one_at_a_time_from_the_baseline() -> None:
    variants = net_sensitivity_variants(
        threshold_pct=Decimal("0"),
        hysteresis_pct=Decimal("0"),
        detector_threshold_pct=Decimal("0.1"),
        delays_ms=(50, 250, 1000),
    )
    assert [variant.name for variant in variants] == [
        "baseline",
        "threshold_detector",
        "hysteresis_0_01",
        "hysteresis_0_05",
        "delay_50ms",
        "delay_250ms",
        "delay_1000ms",
        "fees_halved",
    ]
    baseline = variants[0]
    for variant in variants[1:]:
        changed = [
            name
            for name in ("threshold_pct", "hysteresis_pct", "delay_ns", "fee_mode")
            if getattr(variant, name) != getattr(baseline, name)
        ]
        assert len(changed) == 1, variant

    # A detector threshold equal to the baseline adds no redundant variant.
    same = net_sensitivity_variants(
        threshold_pct=Decimal("0.1"),
        hysteresis_pct=Decimal("0.01"),
        detector_threshold_pct=Decimal("0.1"),
        delays_ms=(),
    )
    assert [variant.name for variant in same] == ["baseline", "hysteresis_0_05", "fees_halved"]


def _top(exchange: str, bid: str, ask: str) -> TopOfBook:
    return TopOfBook(
        exchange=exchange,
        pair="BTC-USD",
        best_bid_price=Decimal(bid),
        best_bid_size=Decimal("1"),
        best_ask_price=Decimal(ask),
        best_ask_size=Decimal("1"),
        sequence=1,
        timestamp_ns=1,
        received_monotonic_ns=1,
    )


def _fill(notional: Decimal, base: Decimal) -> DepthFill:
    return DepthFill(
        notional=notional,
        vwap=notional / base,
        filled_notional=notional,
        filled_base=base,
        levels_used=1,
        insufficient_depth=False,
    )


def _snapshot(
    exchange: str,
    sequence: int,
    bids: list[tuple[str, str]],
    asks: list[tuple[str, str]],
) -> MarketEvent:
    return MarketEvent(
        exchange,
        "BTC-USD",
        EventKind.SNAPSHOT,
        sequence,
        sequence,
        tuple(PriceLevel(Decimal(price), Decimal(size)) for price, size in bids),
        tuple(PriceLevel(Decimal(price), Decimal(size)) for price, size in asks),
    )


def _sampler(
    manager: OrderBookManager,
    fees: dict[str, Decimal],
    notionals: tuple[Decimal, ...] = (Decimal("100"),),
) -> DepthSampler:
    return DepthSampler(
        manager,
        notionals,
        {"coinbase": None, "gemini": None, "binance": 5000},
        fees,
        interval_seconds=5.0,
    )


def test_missing_fee_skips_the_route_fail_closed() -> None:
    manager = OrderBookManager(max_age_seconds=60.0)
    sampler = _sampler(manager, {"coinbase": Decimal("0.6")})
    manager.apply(_snapshot("coinbase", 1, [("99", "2")], [("101", "2")]), received_monotonic_ns=1)
    manager.apply(_snapshot("gemini", 1, [("99", "2")], [("101", "2")]), received_monotonic_ns=1)
    recorder = NetSignalRecorder(manager, sampler)
    recorder.observe("coinbase", "BTC-USD", 1, 1)
    assert recorder.signal_rows == 0


def test_observe_prices_only_routes_through_the_updated_venue() -> None:
    manager = OrderBookManager(max_age_seconds=60.0)
    fees = {**FEES, "binance": Decimal("0.6")}
    sampler = _sampler(manager, fees)
    for exchange in ("coinbase", "gemini", "binance"):
        manager.apply(
            _snapshot(exchange, 1, [("99", "2")], [("101", "2")]), received_monotonic_ns=1
        )
    recorder = NetSignalRecorder(manager, sampler)
    recorder.observe("coinbase", "BTC-USD", 1, 1)
    routes = {(key[1], key[2]) for key in recorder.signals}
    assert routes == {
        ("coinbase", "gemini"),
        ("gemini", "coinbase"),
        ("coinbase", "binance"),
        ("binance", "coinbase"),
    }


def test_change_only_rows_suppress_repeated_observations() -> None:
    manager = OrderBookManager(max_age_seconds=60.0)
    sampler = _sampler(manager, FEES)
    manager.apply(_snapshot("coinbase", 1, [("99", "2")], [("101", "2")]), received_monotonic_ns=1)
    manager.apply(_snapshot("gemini", 1, [("102", "2")], [("104", "2")]), received_monotonic_ns=1)
    recorder = NetSignalRecorder(manager, sampler)
    recorder.observe("coinbase", "BTC-USD", 1, 1)
    first = recorder.signal_rows
    assert first == 2  # both directed routes, one notional
    recorder.observe("coinbase", "BTC-USD", 2, 2)
    assert recorder.signal_rows == first


def test_ineligible_leg_records_the_canonical_reason() -> None:
    manager = OrderBookManager(max_age_seconds=60.0)
    sampler = _sampler(manager, FEES)
    manager.apply(_snapshot("coinbase", 1, [("99", "2")], [("101", "2")]), received_monotonic_ns=1)
    manager.apply(_snapshot("gemini", 1, [("102", "2")], [("104", "2")]), received_monotonic_ns=1)
    manager.set_exchange_connected("gemini", False)
    recorder = NetSignalRecorder(manager, sampler)
    recorder.observe("gemini", "BTC-USD", 2, 2)
    states = {row.state for rows in recorder.signals.values() for row in rows}
    expected = manager.eligibility("gemini", "BTC-USD", 2).reason
    assert expected is not None
    assert states == {f"ineligible:{expected}"}


def test_recorder_fills_match_the_sampler_ledgers_on_deep_and_thin_books() -> None:
    """Windowed walks escalate to the full book and agree with `ledgers_for_route`."""
    manager = OrderBookManager(max_age_seconds=60.0)
    notionals = (Decimal("100"), Decimal("1000"), Decimal("10000"), Decimal("50000"))
    fees = {**FEES, "binance": Decimal("0.6")}
    sampler = _sampler(manager, fees, notionals)
    levels = DEPTH_WINDOW_LEVELS * 3
    # Deep coinbase and binance books: 0.1 base per level near 1000 quote, so
    # 10,000 quote needs about 100 levels (past the window, priced after
    # escalation) and 50,000 exhausts the full book. Thin gemini: 5 levels.
    manager.apply(
        _snapshot(
            "coinbase",
            1,
            [(str(999 - index), "0.1") for index in range(levels)],
            [(str(1000 + index), "0.1") for index in range(levels)],
        ),
        received_monotonic_ns=1,
    )
    manager.apply(
        _snapshot(
            "binance",
            1,
            [(str(1001 - index), "0.1") for index in range(levels)],
            [(str(1002 + index), "0.1") for index in range(levels)],
        ),
        received_monotonic_ns=1,
    )
    manager.apply(
        _snapshot(
            "gemini",
            1,
            [(str(1001 - index), "0.1") for index in range(5)],
            [(str(1003 + index), "0.1") for index in range(5)],
        ),
        received_monotonic_ns=1,
    )
    recorder = NetSignalRecorder(manager, sampler)
    recorder.observe("coinbase", "BTC-USD", 1, 1)

    routes = (
        ("coinbase", "gemini"),
        ("gemini", "coinbase"),
        ("coinbase", "binance"),
        ("binance", "coinbase"),
    )
    for buy, sell in routes:
        ledgers = sampler.ledgers_for_route("BTC-USD", buy, sell, 1)
        for notional, ledger in zip(notionals, ledgers, strict=True):
            [row] = recorder.signals[("BTC-USD", buy, sell, notional)]
            if ledger.insufficient_depth:
                assert row.state == "insufficient_depth", (buy, sell, notional)
            else:
                assert row.state == "priced", (buy, sell, notional)
                assert row.gross_spread_pct == ledger.gross_executable_spread_pct
                assert row.net_spread_pct == ledger.net_executable_spread_pct
    # The deep route prices 10,000 only by escalating past the window and
    # still reports 50,000 as insufficient on the full book; the thin route
    # stops short without escalating.
    states = {
        notional: recorder.signals[("BTC-USD", "coinbase", "binance", notional)][0].state
        for notional in notionals
    }
    assert states == {
        Decimal("100"): "priced",
        Decimal("1000"): "priced",
        Decimal("10000"): "priced",
        Decimal("50000"): "insufficient_depth",
    }
    thin = recorder.signals[("BTC-USD", "coinbase", "gemini", Decimal("1000"))][0]
    assert thin.state == "insufficient_depth"


def test_build_net_datasets_opens_on_zero_fee_books_and_joins_episodes() -> None:
    manager = OrderBookManager(max_age_seconds=60.0)
    sampler = _sampler(manager, {"coinbase": Decimal("0"), "gemini": Decimal("0")})
    # Coinbase asks at 100, Gemini bids at 102: 2% gross before fees.
    manager.apply(_snapshot("coinbase", 1, [("99", "2")], [("100", "2")]), received_monotonic_ns=1)
    manager.apply(_snapshot("gemini", 1, [("102", "2")], [("103", "2")]), received_monotonic_ns=1)
    recorder = NetSignalRecorder(manager, sampler)
    recorder.observe("coinbase", "BTC-USD", 1, 1)
    recorder.observe("gemini", "BTC-USD", 2, 2)
    episodes: list[dict[str, object]] = [
        {
            "pair": "BTC-USD",
            "buy_exchange": "coinbase",
            "sell_exchange": "gemini",
            "start_ns": "1",
            "end_ns": None,
        }
    ]
    datasets = build_net_datasets(
        recorder,
        episodes,
        threshold_pct=Decimal("0"),
        hysteresis_pct=Decimal("0"),
        detector_threshold_pct=Decimal("0.1"),
        delays_ms=(1,),
        end_mono_ns=3_000_000,
        end_wall_ns=3_000_000,
    )
    assert datasets.signal_rows == recorder.signal_rows
    [interval] = datasets.intervals
    assert interval["module"] == "net_interval"
    assert interval["close_reason"] == "end_of_capture"
    assert interval["theoretical_open_at_start"] is True
    assert interval["theoretical_coverage_fraction"] == "1.000000"
    variants = {str(row["variant"]) for row in datasets.sensitivity}
    assert variants == {
        "threshold_detector",
        "hysteresis_0_01",
        "hysteresis_0_05",
        "delay_1ms",
        "fees_halved",
    }
    assert all(row["module"] == "net_interval_sensitivity" for row in datasets.sensitivity)

    without = build_net_datasets(
        recorder,
        episodes,
        threshold_pct=Decimal("0"),
        hysteresis_pct=Decimal("0"),
        detector_threshold_pct=Decimal("0.1"),
        delays_ms=(1,),
        end_mono_ns=3_000_000,
        end_wall_ns=3_000_000,
        sensitivity=False,
    )
    assert without.intervals == datasets.intervals
    assert without.sensitivity == []
