"""Offline net-executable interval analysis over faithful replay.

Theoretical episodes track top-of-book dislocations and refresh their pricing
ledger only at open or at a wider theoretical peak, so they cannot say how
long a configured notional stayed executable and net positive. This module
answers that separately and offline, from matched depth and explicit fees, and
never touches live episode semantics or SQLite.

Signal extraction runs inside the replay loop through the `book_observer`
hook: depth exists only in the live `OrderBookManager` during the run, while
replay observations carry top-of-book alone. Every observation that can change
a book (accepted or rejected update, age expiry, connection boundary)
re-prices the directed routes on that pair which include the updated venue.
The signal is kept in memory as change-only rows so intervalization is a pure
second pass: threshold, hysteresis, delay, and fee variants run over the same
rows without a second replay. Each priced row keeps the gross spread and the
matched fills (base acquired, quote spent, quote received), so a variant fee
schedule re-nets through `arb.pricing.executable_spread_pcts` instead of
re-walking books.

Interval semantics for one `(pair, buy venue, sell venue, notional)` key:

- open: state `priced` and net > threshold (default 0: "net positive").
- spread_closed: `priced` and net <= threshold - hysteresis.
- insufficient_depth: the matched walk cannot fill the notional while both legs
  are still eligible.
- invalidated: a leg became ineligible; its canonical reason is preserved.
- end_of_capture: still open at the last recorded frame, the same boundary the
  research replay uses to close standing theoretical episodes.
- delay: an interval opening at `t` counts only if still open at `t + delay`;
  its reported open moves to `t + delay`, its close does not change. The base
  dataset uses delay 0.

`peak_net_spread_pct` is the largest net observed while open;
`terminal_net_spread_pct` is the last net observed while still open, before the
closing observation. Change-only rows mean a value stays in force until the
next row for that key, so the state at a delayed open is the last row at or
before it.

Memory: rows are appended only when `(state, gross, net)` changes for a key,
but a multi-hour, many-pair capture still accumulates millions of rows. The
research report records `signal_rows` and a byte estimate; keep captures for
this analysis under roughly an hour or split them rather than streaming rows
to disk.

Tiers stay distinct: top-of-book theoretical values, depth-walked executable
values, and fee-adjusted net values are never substituted for one another.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from arb.orderbook import OrderBookManager
from arb.pricing import DepthFill, DepthSampler, executable_spread_pcts, matched_route_fills
from arb.types import RouteLegAges

FeeMode = Literal["configured", "halved"]
CloseReason = Literal["spread_closed", "insufficient_depth", "invalidated", "end_of_capture"]

NetKey = tuple[str, str, str, Decimal]
"""(pair, buy exchange, sell exchange, notional)."""

# Levels materialized per side before falling back to the whole book. The
# whole book on a full-depth venue runs to thousands of levels; the configured
# notionals rarely need more than a few dozen, and a walk that exhausts this
# window escalates so insufficient depth is still decided on the full book.
DEPTH_WINDOW_LEVELS = 64

SIGNAL_ROW_BYTES_ESTIMATE = 400
"""Rough per-row heap footprint (slots dataclass plus its Decimal fields)."""


@dataclass(frozen=True, slots=True)
class NetSignalRow:
    """One change-only observation of a route at a notional.

    `state` is `priced`, `insufficient_depth`, or `ineligible:<reason>` with the
    canonical `BookEligibility.reason` of the failing leg. Priced rows carry
    the gross spread and the matched fills so fee variants re-net without a
    replay; `buy_cost_quote` equals the notional whenever the buy leg filled.
    """

    pair: str
    buy_exchange: str
    sell_exchange: str
    notional: Decimal
    mono_ns: int
    wall_ns: int
    state: str
    gross_spread_pct: Decimal | None
    net_spread_pct: Decimal | None
    executable_base: Decimal | None
    buy_cost_quote: Decimal | None
    sell_proceeds_quote: Decimal | None
    buy_age_ns: int | None
    sell_age_ns: int | None
    skew_ns: int | None

    @property
    def key(self) -> NetKey:
        return (self.pair, self.buy_exchange, self.sell_exchange, self.notional)


@dataclass(frozen=True, slots=True)
class NetInterval:
    """One continuous net-positive stretch for a route at a notional."""

    pair: str
    buy_exchange: str
    sell_exchange: str
    notional: Decimal
    open_mono_ns: int
    open_wall_ns: int
    close_mono_ns: int
    close_wall_ns: int
    duration_ns: int
    peak_net_spread_pct: Decimal
    terminal_net_spread_pct: Decimal
    open_executable_base: Decimal | None
    open_executable_quote: Decimal | None
    close_executable_base: Decimal | None
    close_executable_quote: Decimal | None
    open_buy_age_ns: int | None
    open_sell_age_ns: int | None
    open_skew_ns: int | None
    max_skew_ns: int | None
    close_reason: CloseReason
    ineligibility_reason: str | None


@dataclass(frozen=True)
class NetVariant:
    """One intervalization setting; `halved` re-nets recorded fills at half fees."""

    name: str
    threshold_pct: Decimal
    hysteresis_pct: Decimal
    delay_ns: int
    fee_mode: FeeMode


def net_sensitivity_variants(
    *,
    threshold_pct: Decimal,
    hysteresis_pct: Decimal,
    detector_threshold_pct: Decimal,
    delays_ms: tuple[int, ...],
) -> list[NetVariant]:
    """Baseline plus one-at-a-time variants, mirroring the lead/lag sensitivity."""
    baseline = NetVariant("baseline", threshold_pct, hysteresis_pct, 0, "configured")
    variants = [baseline]
    if detector_threshold_pct != threshold_pct:
        variants.append(
            NetVariant(
                "threshold_detector", detector_threshold_pct, hysteresis_pct, 0, "configured"
            )
        )
    for value in (Decimal("0.01"), Decimal("0.05")):
        if value == hysteresis_pct:
            continue
        label = str(value).replace(".", "_")
        variants.append(NetVariant(f"hysteresis_{label}", threshold_pct, value, 0, "configured"))
    for delay_ms in delays_ms:
        if delay_ms <= 0:
            continue
        variants.append(
            NetVariant(
                f"delay_{delay_ms}ms",
                threshold_pct,
                hysteresis_pct,
                delay_ms * 1_000_000,
                "configured",
            )
        )
    variants.append(NetVariant("fees_halved", threshold_pct, hysteresis_pct, 0, "halved"))
    return variants


class _RouteDepth:
    """Bounded depth for one route, escalating to the whole book only when a walk needs it.

    Both legs passed eligibility before construction. `level_snapshot` reads
    the best `DEPTH_WINDOW_LEVELS` per side without materializing the full
    book; a walk that uses every window level and still falls short repeats on
    `depth_levels`, so the insufficient-depth decision matches
    `DepthSampler.ledgers_for_route` exactly.
    """

    __slots__ = ("_asks", "_bids", "_full", "_manager", "_now_ns", "_pair", "_route")

    def __init__(
        self,
        manager: OrderBookManager,
        pair: str,
        buy_exchange: str,
        sell_exchange: str,
        now_monotonic_ns: int,
    ) -> None:
        buy_window = manager.level_snapshot(buy_exchange, pair, DEPTH_WINDOW_LEVELS)
        sell_window = manager.level_snapshot(sell_exchange, pair, DEPTH_WINDOW_LEVELS)
        assert buy_window is not None and sell_window is not None
        self._manager = manager
        self._pair = pair
        self._route = (buy_exchange, sell_exchange)
        self._now_ns = now_monotonic_ns
        self._asks = buy_window[1]
        self._bids = sell_window[0]
        self._full = False

    def fills(self, notional: Decimal) -> tuple[DepthFill, DepthFill]:
        buy_fill, sell_fill = matched_route_fills(self._asks, self._bids, notional)
        if self._full:
            return buy_fill, sell_fill
        exhausted = (
            buy_fill.insufficient_depth and buy_fill.levels_used >= DEPTH_WINDOW_LEVELS
        ) or (sell_fill.insufficient_depth and sell_fill.levels_used >= DEPTH_WINDOW_LEVELS)
        if not exhausted:
            return buy_fill, sell_fill
        buy_exchange, sell_exchange = self._route
        buy_sides = self._manager.depth_levels(buy_exchange, self._pair, self._now_ns)
        sell_sides = self._manager.depth_levels(sell_exchange, self._pair, self._now_ns)
        assert buy_sides is not None and sell_sides is not None
        self._asks, self._bids, self._full = buy_sides[1], sell_sides[0], True
        return matched_route_fills(self._asks, self._bids, notional)


class NetSignalRecorder:
    """Record change-only net signals for routes containing each updated book.

    One `observe` call re-prices only the directed routes on `pair` that include
    `exchange` (4 of 6 with three venues). An accepted event is a change by
    definition, so no depth diffing is attempted; rows are deduplicated on the
    resulting `(state, gross, net)` instead. Routes missing a configured fee are
    skipped, fail-closed, as `ledgers_for_route` does.
    """

    def __init__(self, book_manager: OrderBookManager, sampler: DepthSampler) -> None:
        self.book_manager = book_manager
        self.sampler = sampler
        self.signals: dict[NetKey, list[NetSignalRow]] = {}
        self._last: dict[NetKey, tuple[str, Decimal | None, Decimal | None]] = {}
        self._pair_exchanges: dict[str, tuple[str, ...]] = {}
        self._pair_exchanges_size = -1

    @property
    def signal_rows(self) -> int:
        return sum(len(rows) for rows in self.signals.values())

    @property
    def taker_fees_pct(self) -> Mapping[str, Decimal]:
        return self.sampler.taker_fees_pct

    def _exchanges_for(self, pair: str) -> tuple[str, ...]:
        known = self.book_manager.known_pairs()
        if len(known) != self._pair_exchanges_size:
            grouped: dict[str, list[str]] = {}
            for exchange, known_pair in known:
                grouped.setdefault(known_pair, []).append(exchange)
            self._pair_exchanges = {
                known_pair: tuple(sorted(exchanges)) for known_pair, exchanges in grouped.items()
            }
            self._pair_exchanges_size = len(known)
        return self._pair_exchanges.get(pair, ())

    def observe(self, exchange: str, pair: str, wall_ns: int, mono_ns: int) -> None:
        exchanges = self._exchanges_for(pair)
        if exchange not in exchanges:
            return
        for other in exchanges:
            if other == exchange:
                continue
            self._observe_route(pair, exchange, other, wall_ns, mono_ns)
            self._observe_route(pair, other, exchange, wall_ns, mono_ns)

    def _observe_route(
        self, pair: str, buy_exchange: str, sell_exchange: str, wall_ns: int, mono_ns: int
    ) -> None:
        fees = self.sampler.taker_fees_pct
        if buy_exchange not in fees or sell_exchange not in fees:
            return
        manager = self.book_manager
        buy_status = manager.eligibility(buy_exchange, pair, mono_ns)
        sell_status = manager.eligibility(sell_exchange, pair, mono_ns)
        buy_top = manager.top_of_book(buy_exchange, pair)
        sell_top = manager.top_of_book(sell_exchange, pair)
        if (
            not buy_status.eligible
            or not sell_status.eligible
            or buy_top is None
            or sell_top is None
        ):
            failing = buy_status if not buy_status.eligible else sell_status
            reason = failing.reason if not failing.eligible else "no_top_of_book"
            self._append_unpriced(
                pair, buy_exchange, sell_exchange, wall_ns, mono_ns, f"ineligible:{reason}", None
            )
            return
        ages = RouteLegAges.between(buy_top, sell_top, mono_ns)
        depth = _RouteDepth(manager, pair, buy_exchange, sell_exchange, mono_ns)
        for notional in self.sampler.notionals:
            buy_fill, sell_fill = depth.fills(notional)
            if buy_fill.insufficient_depth or sell_fill.insufficient_depth:
                self._append_unpriced(
                    pair,
                    buy_exchange,
                    sell_exchange,
                    wall_ns,
                    mono_ns,
                    "insufficient_depth",
                    ages,
                    notionals=(notional,),
                )
                continue
            cost = buy_fill.filled_notional
            proceeds = sell_fill.filled_notional
            gross, net = executable_spread_pcts(
                cost=cost,
                proceeds=proceeds,
                buy_taker_fee_pct=fees[buy_exchange],
                sell_taker_fee_pct=fees[sell_exchange],
            )
            self._append(
                NetSignalRow(
                    pair=pair,
                    buy_exchange=buy_exchange,
                    sell_exchange=sell_exchange,
                    notional=notional,
                    mono_ns=mono_ns,
                    wall_ns=wall_ns,
                    state="priced",
                    gross_spread_pct=gross,
                    net_spread_pct=net,
                    executable_base=buy_fill.filled_base,
                    buy_cost_quote=cost,
                    sell_proceeds_quote=proceeds,
                    buy_age_ns=ages.buy_age_ns,
                    sell_age_ns=ages.sell_age_ns,
                    skew_ns=ages.skew_ns,
                )
            )

    def _append_unpriced(
        self,
        pair: str,
        buy_exchange: str,
        sell_exchange: str,
        wall_ns: int,
        mono_ns: int,
        state: str,
        ages: RouteLegAges | None,
        *,
        notionals: tuple[Decimal, ...] | None = None,
    ) -> None:
        for notional in self.sampler.notionals if notionals is None else notionals:
            self._append(
                NetSignalRow(
                    pair=pair,
                    buy_exchange=buy_exchange,
                    sell_exchange=sell_exchange,
                    notional=notional,
                    mono_ns=mono_ns,
                    wall_ns=wall_ns,
                    state=state,
                    gross_spread_pct=None,
                    net_spread_pct=None,
                    executable_base=None,
                    buy_cost_quote=None,
                    sell_proceeds_quote=None,
                    buy_age_ns=None if ages is None else ages.buy_age_ns,
                    sell_age_ns=None if ages is None else ages.sell_age_ns,
                    skew_ns=None if ages is None else ages.skew_ns,
                )
            )

    def _append(self, row: NetSignalRow) -> None:
        key = row.key
        marker = (row.state, row.gross_spread_pct, row.net_spread_pct)
        if self._last.get(key) == marker:
            return
        self._last[key] = marker
        self.signals.setdefault(key, []).append(row)


NetOf = Callable[[NetSignalRow], Decimal | None]


def net_under_fees(fees: Mapping[str, Decimal] | None) -> NetOf:
    """Return the net for a priced row: recorded, or re-netted under `fees`."""

    def recorded(row: NetSignalRow) -> Decimal | None:
        return row.net_spread_pct if row.state == "priced" else None

    if fees is None:
        return recorded

    def renetted(row: NetSignalRow) -> Decimal | None:
        if (
            row.state != "priced"
            or row.buy_cost_quote is None
            or row.sell_proceeds_quote is None
            or row.buy_exchange not in fees
            or row.sell_exchange not in fees
        ):
            return None
        _, net = executable_spread_pcts(
            cost=row.buy_cost_quote,
            proceeds=row.sell_proceeds_quote,
            buy_taker_fee_pct=fees[row.buy_exchange],
            sell_taker_fee_pct=fees[row.sell_exchange],
        )
        return net

    return renetted


def halved_fees(fees: Mapping[str, Decimal]) -> dict[str, Decimal]:
    return {exchange: fee / 2 for exchange, fee in fees.items()}


def intervalize_signals(
    signals: Mapping[NetKey, list[NetSignalRow]],
    *,
    threshold_pct: Decimal,
    hysteresis_pct: Decimal,
    delay_ns: int,
    end_mono_ns: int,
    end_wall_ns: int,
    fees: Mapping[str, Decimal] | None = None,
) -> list[NetInterval]:
    """Fold change-only signals into net intervals; see the module docstring."""
    if hysteresis_pct < 0:
        raise ValueError("hysteresis must be non-negative")
    if delay_ns < 0:
        raise ValueError("delay must be non-negative")
    net_of = net_under_fees(fees)
    intervals: list[NetInterval] = []
    for key in sorted(signals, key=lambda item: (item[0], item[1], item[2], item[3])):
        rows = sorted(signals[key], key=lambda row: (row.mono_ns, row.wall_ns))
        intervals.extend(
            _intervalize_key(
                rows,
                net_of=net_of,
                threshold_pct=threshold_pct,
                hysteresis_pct=hysteresis_pct,
                delay_ns=delay_ns,
                end_mono_ns=end_mono_ns,
                end_wall_ns=end_wall_ns,
            )
        )
    return sorted(
        intervals,
        key=lambda item: (
            item.open_mono_ns,
            item.pair,
            item.buy_exchange,
            item.sell_exchange,
            item.notional,
        ),
    )


def _intervalize_key(
    rows: list[NetSignalRow],
    *,
    net_of: NetOf,
    threshold_pct: Decimal,
    hysteresis_pct: Decimal,
    delay_ns: int,
    end_mono_ns: int,
    end_wall_ns: int,
) -> list[NetInterval]:
    close_below = threshold_pct - hysteresis_pct
    intervals: list[NetInterval] = []
    open_idx: int | None = None

    def close(close_idx: int | None, reason: CloseReason, detail: str | None) -> None:
        assert open_idx is not None
        interval = _build_interval(
            rows,
            net_of=net_of,
            open_idx=open_idx,
            close_idx=close_idx,
            close_reason=reason,
            ineligibility_reason=detail,
            delay_ns=delay_ns,
            end_mono_ns=end_mono_ns,
            end_wall_ns=end_wall_ns,
        )
        if interval is not None:
            intervals.append(interval)

    for index, row in enumerate(rows):
        net = net_of(row)
        if open_idx is None:
            if net is not None and net > threshold_pct:
                open_idx = index
            continue
        if row.state.startswith("ineligible:"):
            close(index, "invalidated", row.state.split(":", 1)[1])
            open_idx = None
        elif row.state == "insufficient_depth":
            close(index, "insufficient_depth", None)
            open_idx = None
        elif net is not None and net <= close_below:
            close(index, "spread_closed", None)
            open_idx = None
    if open_idx is not None:
        close(None, "end_of_capture", None)
    return intervals


def _build_interval(
    rows: list[NetSignalRow],
    *,
    net_of: NetOf,
    open_idx: int,
    close_idx: int | None,
    close_reason: CloseReason,
    ineligibility_reason: str | None,
    delay_ns: int,
    end_mono_ns: int,
    end_wall_ns: int,
) -> NetInterval | None:
    opening = rows[open_idx]
    if close_idx is None:
        close_mono, close_wall = end_mono_ns, end_wall_ns
    else:
        close_mono, close_wall = rows[close_idx].mono_ns, rows[close_idx].wall_ns
    if close_mono < opening.mono_ns:
        return None
    open_mono = opening.mono_ns + delay_ns
    open_wall = opening.wall_ns + delay_ns
    if delay_ns > 0 and close_mono <= open_mono:
        return None
    # Rows in force while open: from the opening row up to, not including, the
    # closing observation. Every one of them is priced above the close band.
    active = rows[open_idx : len(rows) if close_idx is None else close_idx]
    # Change-only rows: the value at the (possibly delayed) open is the last
    # row at or before it.
    in_force = [row for row in active if row.mono_ns <= open_mono]
    first = in_force[-1]
    considered = active[len(in_force) - 1 :]
    nets = [net for row in considered if (net := net_of(row)) is not None]
    assert nets, "an open interval has at least one priced row in force"
    last = considered[-1]
    skews = [row.skew_ns for row in considered if row.skew_ns is not None]
    return NetInterval(
        pair=opening.pair,
        buy_exchange=opening.buy_exchange,
        sell_exchange=opening.sell_exchange,
        notional=opening.notional,
        open_mono_ns=open_mono,
        open_wall_ns=open_wall,
        close_mono_ns=close_mono,
        close_wall_ns=close_wall,
        duration_ns=close_mono - open_mono,
        peak_net_spread_pct=max(nets),
        terminal_net_spread_pct=nets[-1],
        open_executable_base=first.executable_base,
        open_executable_quote=first.buy_cost_quote,
        close_executable_base=last.executable_base,
        close_executable_quote=last.buy_cost_quote,
        open_buy_age_ns=first.buy_age_ns,
        open_sell_age_ns=first.sell_age_ns,
        open_skew_ns=first.skew_ns,
        max_skew_ns=max(skews) if skews else None,
        close_reason=close_reason,
        ineligibility_reason=ineligibility_reason,
    )


def add_theoretical_overlap(
    rows: list[dict[str, object]], episodes: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Join net intervals to theoretical episodes on the same route (wall time).

    `theoretical_open_at_start` says whether a theoretical episode on the same
    route was open when the net interval opened; `theoretical_coverage_fraction`
    is the share of the net interval's duration inside such episodes. An
    episode still open at the capture end covers through the interval's close.
    """
    by_route: dict[tuple[str, str, str], list[tuple[int, int | None]]] = {}
    for episode in episodes:
        route = (
            str(episode["pair"]),
            str(episode["buy_exchange"]),
            str(episode["sell_exchange"]),
        )
        end_raw = episode.get("end_ns")
        end = None if end_raw is None else int(str(end_raw))
        by_route.setdefault(route, []).append((int(str(episode["start_ns"])), end))
    enriched: list[dict[str, object]] = []
    for row in rows:
        route = (str(row["pair"]), str(row["buy_exchange"]), str(row["sell_exchange"]))
        open_wall = int(str(row["open_wall_ns"]))
        close_wall = int(str(row["close_wall_ns"]))
        spans = by_route.get(route, [])
        open_covered = any(
            start <= open_wall and (end is None or end > open_wall) for start, end in spans
        )
        duration = close_wall - open_wall
        if duration <= 0:
            coverage = Decimal(1 if open_covered else 0)
        else:
            covered = 0
            for start, end in spans:
                low = max(start, open_wall)
                high = min(close_wall if end is None else end, close_wall)
                if high > low:
                    covered += high - low
            coverage = (Decimal(covered) / Decimal(duration)).quantize(Decimal("0.000001"))
        enriched.append(
            {
                **row,
                "theoretical_open_at_start": open_covered,
                "theoretical_coverage_fraction": str(coverage),
            }
        )
    return enriched


def _ms(value: int | None) -> int | None:
    return None if value is None else value // 1_000_000


def _decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def net_interval_row(
    interval: NetInterval,
    *,
    module: str,
    variant: NetVariant,
) -> dict[str, object]:
    """Serialize one interval with every price and size as a decimal string."""
    row: dict[str, object] = {
        "module": module,
        "variant": variant.name,
        "pair": interval.pair,
        "buy_exchange": interval.buy_exchange,
        "sell_exchange": interval.sell_exchange,
        "notional": str(interval.notional),
        "open_mono_ns": str(interval.open_mono_ns),
        "open_wall_ns": str(interval.open_wall_ns),
        "close_mono_ns": str(interval.close_mono_ns),
        "close_wall_ns": str(interval.close_wall_ns),
        "duration_ns": str(interval.duration_ns),
        "peak_net_spread_pct": str(interval.peak_net_spread_pct),
        "terminal_net_spread_pct": str(interval.terminal_net_spread_pct),
        "open_executable_base": _decimal(interval.open_executable_base),
        "open_executable_quote": _decimal(interval.open_executable_quote),
        "close_executable_base": _decimal(interval.close_executable_base),
        "close_executable_quote": _decimal(interval.close_executable_quote),
        "open_buy_age_ms": _ms(interval.open_buy_age_ns),
        "open_sell_age_ms": _ms(interval.open_sell_age_ns),
        "open_age_skew_ms": _ms(interval.open_skew_ns),
        "max_age_skew_ms": _ms(interval.max_skew_ns),
        "close_reason": interval.close_reason,
        "ineligibility_reason": interval.ineligibility_reason,
        "threshold_pct": str(variant.threshold_pct),
        "hysteresis_pct": str(variant.hysteresis_pct),
        "delay_ns": str(variant.delay_ns),
        "fee_mode": variant.fee_mode,
    }
    return row


def net_signal_row(row: NetSignalRow) -> dict[str, object]:
    """Serialize one raw signal row for the opt-in `net_signal.jsonl` export."""
    return {
        "module": "net_signal",
        "pair": row.pair,
        "buy_exchange": row.buy_exchange,
        "sell_exchange": row.sell_exchange,
        "notional": str(row.notional),
        "mono_ns": str(row.mono_ns),
        "wall_ns": str(row.wall_ns),
        "state": row.state,
        "gross_spread_pct": _decimal(row.gross_spread_pct),
        "net_spread_pct": _decimal(row.net_spread_pct),
        "executable_base": _decimal(row.executable_base),
        "buy_cost_quote": _decimal(row.buy_cost_quote),
        "sell_proceeds_quote": _decimal(row.sell_proceeds_quote),
        "buy_age_ms": _ms(row.buy_age_ns),
        "sell_age_ms": _ms(row.sell_age_ns),
        "age_skew_ms": _ms(row.skew_ns),
    }


@dataclass
class NetDatasets:
    intervals: list[dict[str, object]] = field(default_factory=list)
    sensitivity: list[dict[str, object]] = field(default_factory=list)
    variants: list[NetVariant] = field(default_factory=list)
    signal_rows: int = 0


def build_net_datasets(
    recorder: NetSignalRecorder,
    episodes: list[dict[str, object]],
    *,
    threshold_pct: Decimal,
    hysteresis_pct: Decimal,
    detector_threshold_pct: Decimal,
    delays_ms: tuple[int, ...],
    end_mono_ns: int,
    end_wall_ns: int,
    sensitivity: bool = True,
) -> NetDatasets:
    """Baseline net intervals plus one-at-a-time sensitivity rows, both joined to episodes."""
    variants = net_sensitivity_variants(
        threshold_pct=threshold_pct,
        hysteresis_pct=hysteresis_pct,
        detector_threshold_pct=detector_threshold_pct,
        delays_ms=delays_ms,
    )
    configured = recorder.taker_fees_pct
    datasets = NetDatasets(variants=variants, signal_rows=recorder.signal_rows)
    for variant in variants:
        if variant.name != "baseline" and not sensitivity:
            break
        intervals = intervalize_signals(
            recorder.signals,
            threshold_pct=variant.threshold_pct,
            hysteresis_pct=variant.hysteresis_pct,
            delay_ns=variant.delay_ns,
            end_mono_ns=end_mono_ns,
            end_wall_ns=end_wall_ns,
            fees=halved_fees(configured) if variant.fee_mode == "halved" else None,
        )
        module = "net_interval" if variant.name == "baseline" else "net_interval_sensitivity"
        rows = add_theoretical_overlap(
            [net_interval_row(item, module=module, variant=variant) for item in intervals],
            episodes,
        )
        if variant.name == "baseline":
            datasets.intervals = rows
        else:
            datasets.sensitivity.extend(rows)
    return datasets


__all__ = [
    "DEPTH_WINDOW_LEVELS",
    "SIGNAL_ROW_BYTES_ESTIMATE",
    "CloseReason",
    "FeeMode",
    "NetDatasets",
    "NetInterval",
    "NetKey",
    "NetSignalRecorder",
    "NetSignalRow",
    "NetVariant",
    "add_theoretical_overlap",
    "build_net_datasets",
    "halved_fees",
    "intervalize_signals",
    "net_interval_row",
    "net_sensitivity_variants",
    "net_signal_row",
    "net_under_fees",
]
