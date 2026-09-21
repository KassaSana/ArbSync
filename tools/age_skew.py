"""Route leg age and cross-venue receipt skew diagnostics over a replayed capture.

Canonical eligibility limits each book's age on its own. A route compares two
books whose ages are unrelated, so two questions stay open: how old was the
older leg when a spread appeared (absolute age), and how far apart were the
two receipt times (relative skew)? Both are measured on the local monotonic
clock from the detector's `RouteAgeEvent` stream; exchange timestamps never
enter. They are kept as two separate dimensions because relative skew shows
the inputs were asynchronous without proving the quieter book wrong, while
equal ages say nothing about whether either book is fresh.

Connection state and sequence continuity are not folded into either
dimension: an episode closed by a lost leg still carries `close_reason`
`book_ineligible`, and the replay lifecycle trace records the boundary.

Nothing here gates detection. The gate-sensitivity dataset reports what a
cutoff at each band edge would have retained and rejected so a policy can be
argued from evidence rather than encoded first.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from arb.types import EpisodeCloseReason, RouteAgeEvent, RouteLegAges
from episode_stats import percentile, survival_by_notional, survives_any_notional

Dimension = Literal["absolute_age", "relative_skew"]
DIMENSIONS: tuple[Dimension, ...] = ("absolute_age", "relative_skew")
EpisodeKey = tuple[int, str, str, str]

DEFAULT_AGE_EDGES_MS = (100, 500, 1_000, 5_000, 15_000)
DEFAULT_SKEW_EDGES_MS = (50, 250, 1_000, 5_000)


@dataclass(frozen=True)
class AgeSkewBands:
    """Upper band edges in nanoseconds for each dimension.

    Band `i` holds values `edges[i-1] < value <= edges[i]` (the first band
    starts at zero inclusive); one open-ended band above the last edge
    collects the rest, and a value without an age lands in `unknown`.
    """

    age_edges_ns: tuple[int, ...]
    skew_edges_ns: tuple[int, ...]

    def validate(self) -> None:
        for name, edges in (("age", self.age_edges_ns), ("skew", self.skew_edges_ns)):
            if not edges:
                raise ValueError(f"{name} bands need at least one edge")
            if edges[0] <= 0 or any(b <= a for a, b in zip(edges, edges[1:], strict=False)):
                raise ValueError(f"{name} band edges must be positive and strictly increasing")

    def edges(self, dimension: Dimension) -> tuple[int, ...]:
        return self.age_edges_ns if dimension == "absolute_age" else self.skew_edges_ns

    def band(self, dimension: Dimension, value_ns: int | None) -> int | None:
        """Index into `edges(dimension)`; `len(edges)` is the open band, None is unknown."""
        if value_ns is None:
            return None
        edges = self.edges(dimension)
        for index, edge in enumerate(edges):
            if value_ns <= edge:
                return index
        return len(edges)

    def bounds_ms(self, dimension: Dimension, index: int) -> tuple[int, int | None]:
        edges = self.edges(dimension)
        lower = 0 if index == 0 else edges[index - 1] // 1_000_000
        upper = None if index == len(edges) else edges[index] // 1_000_000
        return lower, upper


def dimension_value(dimension: Dimension, ages: RouteLegAges) -> int | None:
    """The older leg's age for the absolute dimension, the skew for the relative one."""
    if dimension == "absolute_age":
        if ages.buy_age_ns is None or ages.sell_age_ns is None:
            return None
        return max(ages.buy_age_ns, ages.sell_age_ns)
    return ages.skew_ns


@dataclass
class EpisodeAges:
    key: EpisodeKey
    open_ages: RouteLegAges
    peak_ages: RouteLegAges | None = None
    close_ages: RouteLegAges | None = None
    close_reason: EpisodeCloseReason | None = None
    max_skew_ns: int | None = None

    def note_skew(self, ages: RouteLegAges) -> None:
        if ages.skew_ns is None:
            return
        self.max_skew_ns = (
            ages.skew_ns if self.max_skew_ns is None else max(self.max_skew_ns, ages.skew_ns)
        )


@dataclass
class AgeSkewRecorder:
    """Collect the detector's route age events for one replay.

    Evaluations are counted per band and never stored, so memory is bounded by
    the number of episodes rather than the number of book updates.
    """

    bands: AgeSkewBands
    episodes: dict[EpisodeKey, EpisodeAges] = field(default_factory=dict)
    evaluations: dict[Dimension, Counter[int | None]] = field(
        default_factory=lambda: {dimension: Counter() for dimension in DIMENSIONS}
    )
    _open_routes: dict[tuple[str, str, str], EpisodeKey] = field(default_factory=dict)

    def record(self, event: RouteAgeEvent) -> None:
        route = (event.pair, event.buy_exchange, event.sell_exchange)
        if event.kind == "evaluated":
            for dimension in DIMENSIONS:
                value = dimension_value(dimension, event.ages)
                self.evaluations[dimension][self.bands.band(dimension, value)] += 1
            open_key = self._open_routes.get(route)
            if open_key is not None:
                self.episodes[open_key].note_skew(event.ages)
            return
        assert event.start_ns is not None
        key: EpisodeKey = (event.start_ns, *route)
        if event.kind == "open":
            record = EpisodeAges(key=key, open_ages=event.ages)
            record.note_skew(event.ages)
            self.episodes[key] = record
            self._open_routes[route] = key
            return
        record = self.episodes[key]
        record.note_skew(event.ages)
        if event.kind == "peak":
            record.peak_ages = event.ages
        else:
            record.close_ages = event.ages
            record.close_reason = event.close_reason
            if self._open_routes.get(route) == key:
                del self._open_routes[route]


def _episode_key(row: dict[str, object]) -> EpisodeKey:
    return (
        int(str(row["start_ns"])),
        str(row["pair"]),
        str(row["buy_exchange"]),
        str(row["sell_exchange"]),
    )


def _ages_payload(prefix: str, ages: RouteLegAges | None) -> dict[str, object]:
    if ages is None:
        return {
            f"{prefix}_buy_age_ms": None,
            f"{prefix}_sell_age_ms": None,
            f"{prefix}_age_skew_ms": None,
        }
    payload = ages.as_payload()
    return {
        f"{prefix}_buy_age_ms": payload["buy_age_ms"],
        f"{prefix}_sell_age_ms": payload["sell_age_ms"],
        f"{prefix}_age_skew_ms": payload["age_skew_ms"],
    }


def route_leg_age_rows(
    recorder: AgeSkewRecorder, episodes: list[dict[str, object]]
) -> list[dict[str, object]]:
    """One row per canonical episode, joined to its recorded leg ages by episode key."""
    rows: list[dict[str, object]] = []
    for episode in episodes:
        key = _episode_key(episode)
        record = recorder.episodes.get(key)
        if record is None:
            # An episode the detector reported without an observer event
            # cannot happen in one replay; skip rather than fabricate ages.
            continue
        rows.append(
            {
                "module": "route_leg_ages",
                "start_ns": episode["start_ns"],
                "pair": episode["pair"],
                "buy_exchange": episode["buy_exchange"],
                "sell_exchange": episode["sell_exchange"],
                **_ages_payload("open", record.open_ages),
                **_ages_payload("peak", record.peak_ages),
                **_ages_payload("close", record.close_ages),
                "max_age_skew_ms": (
                    None if record.max_skew_ns is None else record.max_skew_ns // 1_000_000
                ),
                "duration_ns": episode["duration_ns"],
                "peak_spread_pct": episode["peak_spread_pct"],
                "peak_profit": episode["peak_profit"],
                "close_reason": episode["close_reason"],
                "fee_survivor": survives_any_notional(episode),
            }
        )
    return rows


def _band_of(bands: AgeSkewBands, dimension: Dimension, record: EpisodeAges) -> int | None:
    return bands.band(dimension, dimension_value(dimension, record.open_ages))


def age_skew_band_rows(
    recorder: AgeSkewRecorder, episodes: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Per dimension and band: evaluations, episodes opened at that band, and outcomes."""
    bands = recorder.bands
    rows: list[dict[str, object]] = []
    for dimension in DIMENSIONS:
        grouped: dict[int | None, list[dict[str, object]]] = {}
        for episode in episodes:
            record = recorder.episodes.get(_episode_key(episode))
            if record is None:
                continue
            grouped.setdefault(_band_of(bands, dimension, record), []).append(episode)
        indices: list[int | None] = list(range(len(bands.edges(dimension)) + 1))
        if grouped.get(None) or recorder.evaluations[dimension].get(None):
            indices.append(None)
        for index in indices:
            members = grouped.get(index, [])
            evaluations = recorder.evaluations[dimension].get(index, 0)
            durations = [float(str(e["duration_ns"])) / 1e6 for e in members if e["duration_ns"]]
            spreads = [float(str(e["peak_spread_pct"])) for e in members]
            lower, upper = (None, None) if index is None else bands.bounds_ms(dimension, index)
            rows.append(
                {
                    "module": "age_skew_bands",
                    "dimension": dimension,
                    "band": "unknown" if index is None else index,
                    "band_lower_ms": lower,
                    "band_upper_ms": upper,
                    "evaluations": evaluations,
                    "episodes_opened": len(members),
                    "open_rate": len(members) / evaluations if evaluations else None,
                    "closed_book_ineligible": sum(
                        1 for e in members if e["close_reason"] == "book_ineligible"
                    ),
                    "fee_survivors": sum(1 for e in members if survives_any_notional(e)),
                    "peak_spread_pct_p50": percentile(spreads, 0.5),
                    "peak_spread_pct_p90": percentile(spreads, 0.9),
                    "duration_ms_p50": percentile(durations, 0.5),
                    "duration_ms_p90": percentile(durations, 0.9),
                    "duration_ms_max": max(durations) if durations else None,
                    "survival_by_notional": survival_by_notional(members),
                }
            )
    return rows


def _profit_by_quote(episodes: Iterable[dict[str, object]]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for episode in episodes:
        quote = str(episode["quote_asset"])
        totals[quote] = totals.get(quote, Decimal(0)) + Decimal(str(episode["peak_profit"]))
    return totals


def gate_sensitivity_rows(
    recorder: AgeSkewRecorder, episodes: list[dict[str, object]]
) -> list[dict[str, object]]:
    """What a cutoff at each band edge would have retained and rejected at episode open.

    Episodes without an age on the dimension are only counted in
    `episodes_unknown`: a gate could not have judged them, so they are in
    neither side's survivors, profit, or shares.
    """
    bands = recorder.bands
    rows: list[dict[str, object]] = []
    records = [
        (episode, recorder.episodes[_episode_key(episode)])
        for episode in episodes
        if _episode_key(episode) in recorder.episodes
    ]
    for dimension in DIMENSIONS:
        # Episodes the gate could not judge stay out of every total on this
        # dimension, so shares describe only what a cutoff actually decided.
        judged = [
            (episode, value)
            for episode, record in records
            if (value := dimension_value(dimension, record.open_ages)) is not None
        ]
        unknown = len(records) - len(judged)
        total_profit = _profit_by_quote(episode for episode, _ in judged)
        total_survivors = sum(1 for e, _ in judged if survives_any_notional(e))
        for edge in bands.edges(dimension):
            retained = [episode for episode, value in judged if value <= edge]
            rejected = [episode for episode, value in judged if value > edge]
            retained_profit = _profit_by_quote(retained)
            retained_survivors = sum(1 for e in retained if survives_any_notional(e))
            rows.append(
                {
                    "module": "age_skew_gate_sensitivity",
                    "dimension": dimension,
                    "cutoff_ms": edge // 1_000_000,
                    "episodes_retained": len(retained),
                    "episodes_rejected": len(rejected),
                    "episodes_unknown": unknown,
                    "fee_survivors_retained": retained_survivors,
                    "fee_survivors_rejected": total_survivors - retained_survivors,
                    "retained_survivor_share": (
                        retained_survivors / total_survivors if total_survivors else None
                    ),
                    "book_ineligible_rejected": sum(
                        1 for e in rejected if e["close_reason"] == "book_ineligible"
                    ),
                    "book_ineligible_retained": sum(
                        1 for e in retained if e["close_reason"] == "book_ineligible"
                    ),
                    # Theoretical top-of-book profit at peak, kept per quote asset
                    # because USD and USDT totals must never be added together.
                    "retained_peak_profit_by_quote": {
                        quote: str(value) for quote, value in sorted(retained_profit.items())
                    },
                    "total_peak_profit_by_quote": {
                        quote: str(value) for quote, value in sorted(total_profit.items())
                    },
                    "retained_peak_profit_share_by_quote": {
                        quote: (
                            None
                            if not total
                            else str(retained_profit.get(quote, Decimal(0)) / total)
                        )
                        for quote, total in sorted(total_profit.items())
                    },
                }
            )
    return rows
