from __future__ import annotations

from decimal import Decimal

import pytest
from arb.pricing import (
    matched_route_fills,
    price_book,
    pricing_ledger,
    walk_levels,
)
from arb.types import MarketEvent, PriceLevel, TopOfBook
from hypothesis import given, settings
from hypothesis import strategies as st


def levels(*pairs: tuple[str, str]) -> list[PriceLevel]:
    return [PriceLevel(Decimal(price), Decimal(size)) for price, size in pairs]


def test_walk_takes_a_partial_last_level_and_spends_exactly_the_notional() -> None:
    asks = levels(("100", "1"), ("102", "2"), ("110", "5"))
    fill = walk_levels(asks, Decimal("250"))

    # 100 from the first level, 150 of the 204 available at 102.
    assert fill.insufficient_depth is False
    assert fill.filled_notional == Decimal("250")
    assert fill.levels_used == 2
    # 1 + 150/102 = 42/17 base; 250 / (42/17) = 4250/42 = 2125/21, each rounded once.
    assert fill.filled_base == Decimal(42) / Decimal(17)
    assert fill.vwap == Decimal(2125) / Decimal(21)


def test_walk_reports_insufficient_depth_with_what_was_available() -> None:
    asks = levels(("100", "1"), ("102", "2"))
    fill = walk_levels(asks, Decimal("1000"))

    assert fill.insufficient_depth is True
    assert fill.vwap is None
    assert fill.filled_notional == Decimal("304")
    assert fill.filled_base == Decimal("3")
    assert fill.levels_used == 2


def test_walk_rejects_a_non_positive_notional_and_skips_empty_levels() -> None:
    with pytest.raises(ValueError):
        walk_levels(levels(("100", "1")), Decimal("0"))
    fill = walk_levels(levels(("100", "0"), ("101", "1")), Decimal("50"))
    assert fill.levels_used == 1
    assert fill.vwap == Decimal("101")


def test_price_book_quotes_both_sides_for_every_notional() -> None:
    quotes = price_book(
        "gemini",
        "BTC-USD",
        bids=levels(("99", "1")),
        asks=levels(("101", "1")),
        notionals=[Decimal("50"), Decimal("500")],
        subscribed_depth_levels=None,
    )
    assert [(q.side, str(q.fill.notional), q.fill.insufficient_depth) for q in quotes] == [
        ("buy", "50", False),
        ("sell", "50", False),
        ("buy", "500", True),
        ("sell", "500", True),
    ]
    payload = quotes[0].as_payload()
    assert payload["vwap"] == "101"
    assert payload["subscribed_depth_levels"] is None
    assert quotes[2].as_payload()["vwap"] is None


def test_pricing_ledger_keeps_theoretical_depth_fee_and_net_tiers_distinct() -> None:
    buy_top = TopOfBook(
        "gemini", "BTC-USD", Decimal("99"), Decimal("2"), Decimal("100"), Decimal("1"), 1, 1
    )
    sell_top = TopOfBook(
        "coinbase", "BTC-USD", Decimal("103"), Decimal("1"), Decimal("104"), Decimal("2"), 1, 1
    )
    buy_fill, sell_fill = matched_route_fills(
        levels(("100", "1"), ("102", "2")),
        levels(("103", "1"), ("101", "2")),
        Decimal("200"),
    )

    ledger = pricing_ledger(
        notional=Decimal("200"),
        buy_top=buy_top,
        sell_top=sell_top,
        buy_fill=buy_fill,
        sell_fill=sell_fill,
        buy_taker_fee_pct=Decimal("0.4"),
        sell_taker_fee_pct=Decimal("0.6"),
    )

    assert ledger.top_of_book_spread_pct == Decimal("3")
    assert ledger.buy_vwap is not None and ledger.sell_vwap is not None
    assert ledger.gross_executable_spread_pct is not None
    assert ledger.depth_impact_pct == ledger.gross_executable_spread_pct - Decimal("3")
    assert ledger.net_executable_spread_pct is not None
    assert (
        ledger.fee_impact_pct
        == ledger.net_executable_spread_pct - ledger.gross_executable_spread_pct
    )
    payload = ledger.as_payload()
    assert payload["buy_taker_fee_pct"] == "0.4"
    assert payload["net_executable_spread_pct"] == str(ledger.net_executable_spread_pct)


def test_pricing_ledger_never_fabricates_executable_values_from_short_depth() -> None:
    top = TopOfBook(
        "gemini", "BTC-USD", Decimal("99"), Decimal("1"), Decimal("100"), Decimal("1"), 1, 1
    )
    short = walk_levels(levels(("100", "1")), Decimal("1000"))
    full = walk_levels(levels(("100", "20")), Decimal("1000"))
    ledger = pricing_ledger(
        notional=Decimal("1000"),
        buy_top=top,
        sell_top=top,
        buy_fill=full,
        sell_fill=short,
        buy_taker_fee_pct=Decimal("0"),
        sell_taker_fee_pct=Decimal("0"),
    )
    assert ledger.insufficient_depth
    assert ledger.gross_executable_spread_pct is None
    assert ledger.net_executable_spread_pct is None


def _route_tops() -> tuple[TopOfBook, TopOfBook]:
    return (
        TopOfBook(
            "gemini", "BTC-USD", Decimal("99"), Decimal("1"), Decimal("100"), Decimal("1"), 1, 1
        ),
        TopOfBook(
            "coinbase", "BTC-USD", Decimal("110"), Decimal("1"), Decimal("111"), Decimal("1"), 1, 1
        ),
    )


def test_matched_route_sell_leg_takes_a_partial_last_level() -> None:
    # A $250 buy at $100 acquires exactly 2.5 base; the sell leg then walks
    # the same bids the removed base-quantity wrapper used to walk directly.
    _, sell_fill = matched_route_fills(
        levels(("100", "10")), levels(("150", "1"), ("90", "2")), Decimal("250")
    )

    assert sell_fill.insufficient_depth is False
    assert sell_fill.filled_base == Decimal("2.5")
    assert sell_fill.filled_notional == Decimal("285")
    assert sell_fill.vwap == Decimal("114")
    assert sell_fill.levels_used == 2


def test_matched_route_uses_asymmetric_prices_on_one_base_quantity() -> None:
    buy_fill, sell_fill = matched_route_fills(
        levels(("100", "10")), levels(("110", "10")), Decimal("200")
    )

    assert buy_fill.filled_base == Decimal("2")
    assert sell_fill.filled_base == Decimal("2")
    assert buy_fill.filled_notional == Decimal("200")
    assert sell_fill.filled_notional == Decimal("220")
    assert buy_fill.vwap == Decimal("100")
    assert sell_fill.vwap == Decimal("110")

    buy_top, sell_top = _route_tops()
    ledger = pricing_ledger(
        notional=Decimal("200"),
        buy_top=buy_top,
        sell_top=sell_top,
        buy_fill=buy_fill,
        sell_fill=sell_fill,
        buy_taker_fee_pct=Decimal("0"),
        sell_taker_fee_pct=Decimal("0"),
    )
    assert ledger.top_of_book_spread_pct == Decimal("10")
    assert ledger.gross_executable_spread_pct == Decimal("10")
    assert ledger.depth_impact_pct == Decimal("0")
    assert ledger.net_executable_spread_pct == Decimal("10")
    assert ledger.insufficient_depth is False


def test_matched_route_walks_multi_level_books_and_partial_final_levels() -> None:
    buy_fill, sell_fill = matched_route_fills(
        levels(("100", "1"), ("120", "2")),
        levels(("150", "1"), ("90", "2")),
        Decimal("200"),
    )

    acquired = Decimal(11) / Decimal(6)
    assert buy_fill.insufficient_depth is False
    assert sell_fill.insufficient_depth is False
    assert buy_fill.filled_base == acquired
    assert sell_fill.filled_base == acquired
    assert buy_fill.filled_notional == Decimal("200")
    assert sell_fill.filled_notional == Decimal("225")
    assert buy_fill.levels_used == 2
    assert sell_fill.levels_used == 2
    assert buy_fill.vwap == Decimal(1200) / Decimal(11)
    assert sell_fill.vwap == Decimal(1350) / Decimal(11)

    buy_top, sell_top = _route_tops()
    ledger = pricing_ledger(
        notional=Decimal("200"),
        buy_top=buy_top,
        sell_top=sell_top,
        buy_fill=buy_fill,
        sell_fill=sell_fill,
        buy_taker_fee_pct=Decimal("0.4"),
        sell_taker_fee_pct=Decimal("0.6"),
    )
    assert ledger.gross_executable_spread_pct == Decimal("12.5")
    assert (
        ledger.depth_impact_pct
        == ledger.gross_executable_spread_pct - ledger.top_of_book_spread_pct
    )
    assert ledger.net_executable_spread_pct is not None
    assert (
        ledger.fee_impact_pct
        == ledger.net_executable_spread_pct - ledger.gross_executable_spread_pct
    )
    assert ledger.net_executable_spread_pct < ledger.gross_executable_spread_pct


def test_matched_route_reports_insufficient_sell_side_depth() -> None:
    buy_fill, sell_fill = matched_route_fills(
        levels(("100", "5")), levels(("110", "0.5")), Decimal("200")
    )

    assert buy_fill.insufficient_depth is False
    assert buy_fill.filled_base == Decimal("2")
    assert sell_fill.insufficient_depth is True
    assert sell_fill.vwap is None
    assert sell_fill.filled_base == Decimal("0.5")
    assert sell_fill.filled_notional == Decimal("55")

    buy_top, sell_top = _route_tops()
    ledger = pricing_ledger(
        notional=Decimal("200"),
        buy_top=buy_top,
        sell_top=sell_top,
        buy_fill=buy_fill,
        sell_fill=sell_fill,
        buy_taker_fee_pct=Decimal("0"),
        sell_taker_fee_pct=Decimal("0"),
    )
    assert ledger.insufficient_depth is True
    assert ledger.buy_vwap == Decimal("100")
    assert ledger.sell_vwap is None
    assert ledger.gross_executable_spread_pct is None
    assert ledger.net_executable_spread_pct is None


def test_matched_route_sell_depth_is_the_acquired_base_not_the_quote_budget() -> None:
    # Quote-notional sell walk cannot spend 100; the acquired 0.5 base still fits.
    cheap_sell_buy, cheap_sell_sell = matched_route_fills(
        levels(("200", "10")), levels(("50", "1")), Decimal("100")
    )
    assert cheap_sell_buy.filled_base == Decimal("0.5")
    assert cheap_sell_sell.filled_base == Decimal("0.5")
    assert cheap_sell_sell.insufficient_depth is False
    assert cheap_sell_sell.filled_notional == Decimal("25")

    # Quote-notional sell walk can raise 200; that only sells 1 of the 4 acquired.
    short_sell_buy, short_sell_sell = matched_route_fills(
        levels(("50", "10")), levels(("200", "2")), Decimal("200")
    )
    assert short_sell_buy.filled_base == Decimal("4")
    assert short_sell_sell.insufficient_depth is True
    assert short_sell_sell.filled_base == Decimal("2")


def test_sampler_route_prices_conserves_acquired_base_across_legs() -> None:
    from arb.orderbook import OrderBookManager
    from arb.pricing import DepthSampler

    manager = OrderBookManager(max_age_seconds=1.0, clock=lambda: 0)
    manager.apply(snapshot("gemini", [("1", "1")], [("100", "1"), ("120", "2")]))
    manager.apply(snapshot("coinbase", [("150", "1"), ("90", "2")], [("999", "1")]))
    sampler = DepthSampler(
        manager,
        [Decimal("200")],
        {"gemini": None, "coinbase": None},
        {"gemini": Decimal("0"), "coinbase": Decimal("0")},
        interval_seconds=5.0,
    )

    route = next(
        item
        for item in sampler.route_prices("BTC-USD", 0)
        if item.buy_exchange == "gemini" and item.sell_exchange == "coinbase"
    )
    ledger = route.ledger
    buy_fill, sell_fill = matched_route_fills(
        levels(("100", "1"), ("120", "2")),
        levels(("150", "1"), ("90", "2")),
        Decimal("200"),
    )
    assert buy_fill.filled_base == sell_fill.filled_base
    assert ledger.buy_vwap == buy_fill.vwap
    assert ledger.sell_vwap == sell_fill.vwap
    assert ledger.gross_executable_spread_pct == Decimal("12.5")
    assert ledger.notional == Decimal("200")


def test_sampler_requested_route_does_not_walk_unrelated_venues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arb.orderbook import OrderBookManager
    from arb.pricing import DepthSampler

    manager = OrderBookManager(max_age_seconds=1.0, clock=lambda: 0)
    for exchange in ("gemini", "coinbase", "binance"):
        manager.apply(snapshot(exchange, [("150", "10")], [("100", "10")]))
    sampler = DepthSampler(
        manager,
        [Decimal("200")],
        {"gemini": None, "coinbase": None, "binance": 5000},
        {exchange: Decimal("0") for exchange in ("gemini", "coinbase", "binance")},
        interval_seconds=5.0,
    )
    expected = tuple(
        route.ledger
        for route in sampler.route_prices("BTC-USD", 0)
        if route.buy_exchange == "gemini" and route.sell_exchange == "coinbase"
    )
    calls: list[str] = []
    original = manager.depth_levels

    def counting_depth_levels(
        exchange: str, pair: str, now_monotonic_ns: int | None = None
    ) -> tuple[list[PriceLevel], list[PriceLevel]] | None:
        calls.append(exchange)
        return original(exchange, pair, now_monotonic_ns)

    monkeypatch.setattr(manager, "depth_levels", counting_depth_levels)

    assert sampler.ledgers_for_route("BTC-USD", "gemini", "coinbase", 0) == expected
    assert calls == ["gemini", "coinbase"]


# --- Properties (ARB-032) ---

price = st.integers(min_value=1_00, max_value=200_00).map(lambda v: Decimal(v) / 100)
size = st.integers(min_value=1, max_value=50).map(lambda v: Decimal(v) / 10)
book_side = st.lists(st.tuples(price, size), min_size=1, max_size=8).map(
    lambda rows: [PriceLevel(p, s) for p, s in sorted(rows, key=lambda r: r[0])]
)
notional = st.integers(min_value=1, max_value=100_000).map(Decimal)


@settings(max_examples=300)
@given(asks=book_side, small=notional, large=notional)
def test_vwap_is_monotonically_non_improving_in_notional(
    asks: list[PriceLevel], small: Decimal, large: Decimal
) -> None:
    small, large = min(small, large), max(small, large)
    lesser = walk_levels(asks, small)
    greater = walk_levels(asks, large)
    if greater.insufficient_depth:
        return
    assert lesser.vwap is not None and greater.vwap is not None
    # Buying more can only walk deeper into worse prices.
    assert greater.vwap >= lesser.vwap


@settings(max_examples=300)
@given(asks=book_side, fraction=st.integers(min_value=1, max_value=100))
def test_vwap_equals_top_of_book_within_the_first_level(
    asks: list[PriceLevel], fraction: int
) -> None:
    top = asks[0]
    within_first = top.price * top.size * fraction / 100
    fill = walk_levels(asks, within_first)
    assert fill.insufficient_depth is False
    assert fill.levels_used == 1
    assert fill.vwap == top.price


@settings(max_examples=300)
@given(asks=book_side, wanted=notional)
def test_insufficient_depth_exactly_when_summed_depth_is_short(
    asks: list[PriceLevel], wanted: Decimal
) -> None:
    available = sum((level.price * level.size for level in asks), Decimal(0))
    fill = walk_levels(asks, wanted)
    assert fill.insufficient_depth is (available < wanted)
    if fill.insufficient_depth:
        assert fill.filled_notional == available
        assert fill.vwap is None
    else:
        assert fill.filled_notional == wanted
        assert fill.vwap is not None


@settings(max_examples=300)
@given(asks=book_side, bids=book_side, wanted=notional)
def test_matched_route_conserves_base_quantity_across_legs(
    asks: list[PriceLevel], bids: list[PriceLevel], wanted: Decimal
) -> None:
    buy_fill, sell_fill = matched_route_fills(asks, list(reversed(bids)), wanted)
    if buy_fill.insufficient_depth:
        assert sell_fill.insufficient_depth is True
        assert sell_fill.vwap is None
        assert sell_fill.filled_base == Decimal(0)
        return
    if sell_fill.insufficient_depth:
        assert sell_fill.vwap is None
        assert sell_fill.filled_base < buy_fill.filled_base
        return
    assert buy_fill.filled_base == sell_fill.filled_base
    assert buy_fill.filled_notional == wanted
    assert buy_fill.vwap is not None and sell_fill.vwap is not None


# --- Manager depth read and the sampler ---


def snapshot(
    exchange: str, bids: list[tuple[str, str]], asks: list[tuple[str, str]]
) -> MarketEvent:
    from arb.types import EventKind

    return MarketEvent(
        exchange=exchange,
        pair="BTC-USD",
        kind=EventKind.SNAPSHOT,
        sequence=1,
        timestamp_ns=1,
        bids=tuple(PriceLevel(Decimal(p), Decimal(s)) for p, s in bids),
        asks=tuple(PriceLevel(Decimal(p), Decimal(s)) for p, s in asks),
    )


def test_depth_levels_returns_every_level_best_first_only_while_eligible() -> None:
    from arb.orderbook import OrderBookManager

    manager = OrderBookManager(max_age_seconds=1.0, clock=lambda: 0)
    manager.apply(snapshot("gemini", [("99", "1"), ("100", "2")], [("101", "3"), ("105", "1")]))

    sides = manager.depth_levels("gemini", "BTC-USD", 0)
    assert sides is not None
    bids, asks = sides
    assert [str(level.price) for level in bids] == ["100", "99"]
    assert [str(level.price) for level in asks] == ["101", "105"]

    # Aged past freshness: no depth to price, rather than a stale walk.
    assert manager.depth_levels("gemini", "BTC-USD", 2_000_000_000) is None
    assert manager.depth_levels("gemini", "ETH-USD", 0) is None


def test_sampler_observes_eligible_books_and_counts_ineligible_ones() -> None:
    from arb.orderbook import OrderBookManager
    from arb.pricing import DepthSampler

    manager = OrderBookManager(max_age_seconds=1.0, clock=lambda: 0)
    manager.apply(snapshot("gemini", [("99", "10")], [("101", "10")]))  # 1010 available
    manager.apply(snapshot("binance", [("99", "1")], [("101", "1")]))  # 101 available
    sampler = DepthSampler(
        manager,
        [Decimal("100"), Decimal("1000")],
        {"gemini": None, "binance": 5000},
        {},
        interval_seconds=5.0,
    )

    sampler.sample_all(0)
    manager.invalidate("binance", "BTC-USD")
    sampler.sample_all(0)

    assert sampler.samples == 2
    rows = {(r["exchange"], r["notional"], r["side"]): r for r in sampler.fill_rates.rows()}
    assert rows[("gemini", "100", "buy")]["filled"] == 2
    assert rows[("gemini", "1000", "buy")]["filled"] == 2
    assert rows[("binance", "100", "buy")]["observations"] == 1
    assert rows[("binance", "1000", "buy")]["filled"] == 0
    assert rows[("binance", "1000", "buy")]["insufficient_depth"] == 1
    # The second binance sample was ineligible: not a failed fill, a missing observation.
    assert rows[("binance", "100", "buy")]["ineligible_samples"] == 1
    assert rows[("binance", "100", "buy")]["ineligible"]["uninitialized"] == 1  # type: ignore[index]
    assert rows[("binance", "100", "buy")]["samples"] == 2
    assert rows[("binance", "100", "buy")]["subscribed_depth_levels"] == 5000
    assert sampler.quote("binance", "BTC-USD", 0) == []
    assert len(sampler.quote_pair("BTC-USD", 0)) == 4  # gemini only: 2 notionals x 2 sides


def test_sampler_route_prices_carry_leg_receipt_ages_from_the_manager_clock() -> None:
    from arb.orderbook import OrderBookManager
    from arb.pricing import DepthSampler

    now = [10_000_000_000]
    manager = OrderBookManager(max_age_seconds=30.0, clock=lambda: now[0])
    manager.apply(
        snapshot("gemini", [("1", "1")], [("100", "1")]),
        received_monotonic_ns=9_400_000_000,
    )
    manager.apply(
        snapshot("coinbase", [("150", "1")], [("999", "1")]),
        received_monotonic_ns=9_950_000_000,
    )
    sampler = DepthSampler(
        manager,
        [Decimal("100")],
        {"gemini": None, "coinbase": None},
        {"gemini": Decimal("0"), "coinbase": Decimal("0")},
        interval_seconds=5.0,
    )

    # No explicit instant: the manager's clock is the age reference.
    routes = {(r.buy_exchange, r.sell_exchange): r for r in sampler.route_prices("BTC-USD")}
    forward = routes[("gemini", "coinbase")]
    assert (forward.leg_ages.buy_age_ns, forward.leg_ages.sell_age_ns) == (
        600_000_000,
        50_000_000,
    )
    assert forward.leg_ages.skew_ns == 550_000_000
    reverse = routes[("coinbase", "gemini")]
    assert reverse.leg_ages.skew_ns == 550_000_000
    payload = forward.as_payload()
    assert (payload["buy_age_ms"], payload["sell_age_ms"], payload["age_skew_ms"]) == (
        600,
        50,
        550,
    )

    # An explicit instant is honoured instead.
    later = sampler.route_prices("BTC-USD", 11_000_000_000)
    assert {r.leg_ages.buy_age_ns for r in later if r.buy_exchange == "gemini"} == {1_600_000_000}
