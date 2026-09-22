"""Windowed, reproducible fill-rate statistics for the depth sampler.

A fill-rate sample asks one question of every configured book at one instant:
could each (notional, side) be filled from the book's current depth? Every
sample adds exactly one outcome to each row of the roster, so a row's counts
always satisfy

    samples == filled + insufficient_depth + sum(ineligible.values())

and nothing is silently absent. A book that is missing, never initialized,
disconnected, stale, or crossed is counted under that canonical eligibility
reason, never as a depth shortfall and never omitted.

Sampling runs on a fixed grid per session: tick k is due at
`anchor + k * interval` (k >= 1), where the anchor is the process start (live)
or the first captured frame (replay), and every sample is stamped with its
scheduled grid time rather than the instant the loop happened to wake. A live
loop that wakes one or more whole intervals late skips those ticks and counts
them as `missed_samples`; it never backfills them.

Counts are grouped per session into one-minute buckets keyed by the scheduled
wall time. A bucket is emitted once its minute closes (and on shutdown), which
keeps the persisted representation bounded at roster x notionals x 2 rows per
minute. Windowed aggregates only ever sum these exact integer counts, so the
same buckets always reduce to the same answer, and buckets from different
configurations (roster, notionals, cadence, depth caps, age limit) are grouped
by configuration fingerprint instead of being merged.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, cast

Side = Literal["buy", "sell"]
SIDES: tuple[Side, Side] = ("buy", "sell")
MINUTE_NS = 60_000_000_000

# Every reason `OrderBookManager` can report for an ineligible book, in its
# evaluation order. Persisted as one column each so windows sum them in SQL.
INELIGIBLE_REASONS: tuple[str, ...] = (
    "missing",
    "disconnected",
    "uninitialized",
    "discontinuous",
    "too_old",
    "incomplete",
    "crossed",
)

FillRowKey = tuple[str, str, Decimal, Side]


@dataclass
class FillCounts:
    """Exact outcome counts for one (venue, pair, notional, side) row."""

    samples: int = 0
    filled: int = 0
    insufficient_depth: int = 0
    # Subset of `insufficient_depth` where the walked side held at least the
    # venue's subscribed level cap: the shortfall may be the cap, not liquidity.
    insufficient_at_depth_cap: int = 0
    ineligible: dict[str, int] = field(default_factory=lambda: dict.fromkeys(INELIGIBLE_REASONS, 0))

    def record_fill(self, *, filled: bool, at_depth_cap: bool) -> None:
        self.samples += 1
        if filled:
            self.filled += 1
        else:
            self.insufficient_depth += 1
            if at_depth_cap:
                self.insufficient_at_depth_cap += 1

    def record_ineligible(self, reason: str) -> None:
        if reason not in self.ineligible:
            raise ValueError(f"unknown ineligibility reason {reason!r}")
        self.samples += 1
        self.ineligible[reason] += 1

    def add(self, other: FillCounts) -> None:
        self.samples += other.samples
        self.filled += other.filled
        self.insufficient_depth += other.insufficient_depth
        self.insufficient_at_depth_cap += other.insufficient_at_depth_cap
        for reason, count in other.ineligible.items():
            self.ineligible[reason] = self.ineligible.get(reason, 0) + count

    def copy(self) -> FillCounts:
        clone = FillCounts()
        clone.add(self)
        return clone

    @property
    def observations(self) -> int:
        """Samples where the book was eligible and depth was actually walked."""
        return self.filled + self.insufficient_depth

    def payload(self) -> dict[str, object]:
        observations = self.observations
        return {
            "samples": self.samples,
            "observations": observations,
            "filled": self.filled,
            "insufficient_depth": self.insufficient_depth,
            "insufficient_at_depth_cap": self.insufficient_at_depth_cap,
            "ineligible_samples": sum(self.ineligible.values()),
            "ineligible": dict(self.ineligible),
            # Display ratios; the exact counts above are authoritative.
            "fill_rate": None if observations == 0 else self.filled / observations,
            "eligible_share": None if self.samples == 0 else observations / self.samples,
        }


@dataclass(frozen=True)
class FillRateConfig:
    """Everything that changes what a fill-rate count means."""

    roster: tuple[tuple[str, str], ...]
    notionals: tuple[Decimal, ...]
    interval_seconds: float
    depth_levels: tuple[tuple[str, int | None], ...]
    max_age_seconds: float | None

    @classmethod
    def build(
        cls,
        roster: Iterable[tuple[str, str]],
        notionals: Iterable[Decimal],
        interval_seconds: float,
        depth_levels: Mapping[str, int | None],
        max_age_seconds: float | None,
    ) -> FillRateConfig:
        if round(float(interval_seconds) * 1_000_000_000) <= 0:
            raise ValueError(
                f"fill-rate sample interval must be positive; got {interval_seconds!r}"
            )
        return cls(
            roster=tuple(sorted(set(roster))),
            notionals=tuple(sorted(set(notionals))),
            interval_seconds=float(interval_seconds),
            depth_levels=tuple(sorted(depth_levels.items())),
            max_age_seconds=None if max_age_seconds is None else float(max_age_seconds),
        )

    @property
    def interval_ns(self) -> int:
        # Rounded, not truncated: 1.001 s is 1_001_000_000 ns, not one short.
        return round(self.interval_seconds * 1_000_000_000)

    def depth_cap(self, exchange: str) -> int | None:
        return dict(self.depth_levels).get(exchange)

    def payload(self) -> dict[str, object]:
        return {
            "roster": [[exchange, pair] for exchange, pair in self.roster],
            "notionals": [str(notional) for notional in self.notionals],
            "interval_seconds": self.interval_seconds,
            "depth_levels": {exchange: levels for exchange, levels in self.depth_levels},
            "max_age_seconds": self.max_age_seconds,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> FillRateConfig:
        roster = cast(list[list[str]], payload["roster"])
        notionals = cast(list[str], payload["notionals"])
        depth_levels = cast(dict[str, int | None], payload["depth_levels"])
        max_age = cast(float | None, payload["max_age_seconds"])
        return cls.build(
            ((exchange, pair) for exchange, pair in roster),
            (Decimal(value) for value in notionals),
            cast(float, payload["interval_seconds"]),
            depth_levels,
            max_age,
        )

    def canonical_json(self) -> str:
        return json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


@dataclass(frozen=True)
class FillRateSession:
    """One uninterrupted sampling run: a process lifetime or one replay."""

    session_id: str
    started_wall_ns: int
    config: FillRateConfig

    @property
    def config_fingerprint(self) -> str:
        return self.config.fingerprint()

    def payload(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "started_wall_ns": str(self.started_wall_ns),
            "config_fingerprint": self.config_fingerprint,
        }


def session_id_for(started_wall_ns: int, config: FillRateConfig) -> str:
    """Deterministic, so replaying the same capture reproduces the same session."""
    return f"{started_wall_ns}-{config.fingerprint()[:16]}"


@dataclass
class FillRateMinute:
    """One session's counts for the samples scheduled inside one wall-clock minute.

    Each bucket carries its session, so storage can restore a session row that
    was pruned or dropped and no bucket is ever orphaned from its configuration.
    """

    session: FillRateSession
    minute_ns: int
    samples: int = 0
    missed_samples: int = 0
    rows: dict[FillRowKey, FillCounts] = field(default_factory=dict)

    @property
    def session_id(self) -> str:
        return self.session.session_id


FillRateItem = FillRateSession | FillRateMinute
FillRateSink = Callable[[FillRateItem], object]


class FillRateRecorder:
    """Accumulate one session's samples, emitting closed minute buckets to a sink."""

    def __init__(self, config: FillRateConfig, sink: FillRateSink | None = None) -> None:
        self.config = config
        self.sink = sink
        self.session: FillRateSession | None = None
        self.samples = 0
        self.missed_samples = 0
        self._totals: dict[FillRowKey, FillCounts] = {}
        self._open: FillRateMinute | None = None

    def start(self, anchor_wall_ns: int) -> FillRateSession:
        if self.session is not None:
            raise RuntimeError("a fill-rate session is already running")
        self.session = FillRateSession(
            session_id_for(anchor_wall_ns, self.config), anchor_wall_ns, self.config
        )
        self._emit(self.session)
        return self.session

    def sample_wall_ns(self, tick: int) -> int:
        assert self.session is not None
        return self.session.started_wall_ns + tick * self.config.interval_ns

    def _bucket(self, wall_ns: int) -> FillRateMinute:
        assert self.session is not None
        minute_ns = wall_ns - wall_ns % MINUTE_NS
        if self._open is not None and self._open.minute_ns != minute_ns:
            self.flush()
        if self._open is None:
            self._open = FillRateMinute(self.session, minute_ns)
        return self._open

    def record_missed(self, count: int, wall_ns: int) -> None:
        """Count skipped ticks in the bucket of the sample that ends the gap."""
        if count <= 0:
            return
        self.missed_samples += count
        self._bucket(wall_ns).missed_samples += count

    def record_sample(self, wall_ns: int, outcomes: Mapping[FillRowKey, FillCounts]) -> None:
        """Fold one sample's per-row outcomes (each with `samples == 1`) into the session."""
        bucket = self._bucket(wall_ns)
        bucket.samples += 1
        self.samples += 1
        for key, counts in outcomes.items():
            bucket.rows.setdefault(key, FillCounts()).add(counts)
            self._totals.setdefault(key, FillCounts()).add(counts)

    def flush(self) -> None:
        """Emit the open minute, complete or not; called on minute close and shutdown."""
        bucket, self._open = self._open, None
        if bucket is not None:
            self._emit(bucket)

    def _emit(self, item: FillRateItem) -> None:
        if self.sink is not None:
            self.sink(item)

    def totals(self) -> dict[FillRowKey, FillCounts]:
        return self._totals

    def rows(self) -> list[dict[str, object]]:
        return rows_payload(self._totals, self.config)


def rows_payload(
    counts: Mapping[FillRowKey, FillCounts], config: FillRateConfig | None
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for key in sorted(counts):
        exchange, pair, notional, side = key
        rows.append(
            {
                "exchange": exchange,
                "pair": pair,
                "notional": str(notional),
                "side": side,
                "subscribed_depth_levels": None if config is None else config.depth_cap(exchange),
                **counts[key].payload(),
            }
        )
    return rows


@dataclass
class SessionWindow:
    session: FillRateSession
    samples: int = 0
    missed_samples: int = 0


@dataclass
class FillRateWindow:
    """Exact sums of minute buckets inside `[effective_from_ns, effective_to_ns)`.

    With an exchange or pair filter, a session minute contributes its sample
    counts only when it holds a matching row, so `samples`, `coverage`, and the
    session list describe the sampling behind the rows actually returned.
    """

    from_ns: int
    to_ns: int
    effective_from_ns: int
    effective_to_ns: int
    sessions: dict[str, SessionWindow] = field(default_factory=dict)
    # config fingerprint -> row -> counts
    groups: dict[str, dict[FillRowKey, FillCounts]] = field(default_factory=dict)

    def payload(self) -> dict[str, object]:
        span_ns = max(0, self.effective_to_ns - self.effective_from_ns)
        configs: dict[str, FillRateConfig] = {}
        samples_by_config: dict[str, int] = {}
        sampled_ns = 0
        for entry in self.sessions.values():
            fingerprint = entry.session.config_fingerprint
            configs[fingerprint] = entry.session.config
            samples_by_config[fingerprint] = samples_by_config.get(fingerprint, 0) + entry.samples
            sampled_ns += entry.samples * entry.session.config.interval_ns
        return {
            "from_ns": str(self.from_ns),
            "to_ns": str(self.to_ns),
            "effective_from_ns": str(self.effective_from_ns),
            "effective_to_ns": str(self.effective_to_ns),
            "bucket_seconds": MINUTE_NS // 1_000_000_000,
            "samples": sum(entry.samples for entry in self.sessions.values()),
            "missed_samples": sum(entry.missed_samples for entry in self.sessions.values()),
            # Sampled time over window length; below 1 means downtime, restarts,
            # missed ticks, or partial edge minutes the bucket grid excludes.
            "coverage": None if span_ns == 0 else sampled_ns / span_ns,
            "sessions": [
                {
                    **self.sessions[session_id].session.payload(),
                    "samples": self.sessions[session_id].samples,
                    "missed_samples": self.sessions[session_id].missed_samples,
                }
                for session_id in sorted(
                    self.sessions,
                    key=lambda item: (self.sessions[item].session.started_wall_ns, item),
                )
            ],
            "configurations": [
                {
                    "config_fingerprint": fingerprint,
                    "config": configs[fingerprint].payload() if fingerprint in configs else None,
                    "samples": samples_by_config.get(fingerprint, 0),
                    "rows": rows_payload(self.groups[fingerprint], configs.get(fingerprint)),
                }
                for fingerprint in sorted(self.groups)
            ],
        }


def window_bounds(from_ns: int, to_ns: int) -> tuple[int, int]:
    """Whole minutes inside `[from_ns, to_ns)`; a bucket is the smallest unit."""
    start = from_ns if from_ns % MINUTE_NS == 0 else from_ns - from_ns % MINUTE_NS + MINUTE_NS
    end = to_ns - to_ns % MINUTE_NS
    return start, max(start, end)


def aggregate(
    items: Iterable[FillRateItem],
    from_ns: int,
    to_ns: int,
    *,
    exchange: str | None = None,
    pair: str | None = None,
) -> FillRateWindow:
    """Reduce minute buckets to one window, the reference reducer.

    Session items are ignored because every bucket carries its session. The
    store computes the same sums in SQL; tests hold the two equal.
    """
    effective_from, effective_to = window_bounds(from_ns, to_ns)
    window = FillRateWindow(from_ns, to_ns, effective_from, effective_to)
    for minute in items:
        if isinstance(minute, FillRateSession):
            continue
        if not effective_from <= minute.minute_ns < effective_to:
            continue
        matching = [
            (key, counts)
            for key, counts in minute.rows.items()
            if (exchange is None or key[0] == exchange) and (pair is None or key[1] == pair)
        ]
        if not matching:
            continue
        session = minute.session
        entry = window.sessions.setdefault(session.session_id, SessionWindow(session))
        entry.samples += minute.samples
        entry.missed_samples += minute.missed_samples
        group = window.groups.setdefault(session.config_fingerprint, {})
        for key, counts in matching:
            group.setdefault(key, FillCounts()).add(counts)
    return window


def minute_row(minute: FillRateMinute) -> dict[str, object]:
    """One file-backed bucket, as the research tool writes it."""
    return {
        "session_id": minute.session_id,
        "minute_ns": str(minute.minute_ns),
        "samples": minute.samples,
        "missed_samples": minute.missed_samples,
        "rows": rows_payload(minute.rows, None),
    }


def sample_outcomes(
    roster: Sequence[tuple[str, str]],
    notionals: Sequence[Decimal],
) -> dict[FillRowKey, FillCounts]:
    """Empty per-row counters for one sample over the given books."""
    return {
        (exchange, pair, notional, side): FillCounts()
        for exchange, pair in roster
        for notional in notionals
        for side in SIDES
    }


__all__ = [
    "INELIGIBLE_REASONS",
    "MINUTE_NS",
    "FillCounts",
    "FillRateConfig",
    "FillRateItem",
    "FillRateMinute",
    "FillRateRecorder",
    "FillRateSession",
    "FillRateSink",
    "FillRateWindow",
    "FillRowKey",
    "SessionWindow",
    "aggregate",
    "minute_row",
    "rows_payload",
    "sample_outcomes",
    "session_id_for",
    "window_bounds",
]
