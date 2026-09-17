from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Literal


class EventKind(str, Enum):
    SNAPSHOT = "snapshot"
    DELTA = "delta"
    # Adapter-detected discontinuity: the book is unusable until the next
    # snapshot. Carries no levels.
    RESET = "reset"


class Side(str, Enum):
    BID = "bid"
    ASK = "ask"


@dataclass(frozen=True)
class PriceLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class MarketEvent:
    exchange: str
    pair: str
    kind: EventKind
    sequence: int
    timestamp_ns: int
    bids: tuple[PriceLevel, ...] = ()
    asks: tuple[PriceLevel, ...] = ()
    raw_timestamp_ms: int | None = None
    exchange_first_sequence: int | None = None
    exchange_last_sequence: int | None = None
    received_monotonic_ns: int | None = None


@dataclass(frozen=True)
class TopOfBook:
    exchange: str
    pair: str
    best_bid_price: Decimal
    best_bid_size: Decimal
    best_ask_price: Decimal
    best_ask_size: Decimal
    sequence: int
    timestamp_ns: int

    def as_payload(self) -> dict[str, object]:
        return {
            "exchange": self.exchange,
            "pair": self.pair,
            "best_bid_price": str(self.best_bid_price),
            "best_bid_size": str(self.best_bid_size),
            "best_ask_price": str(self.best_ask_price),
            "best_ask_size": str(self.best_ask_size),
            "sequence": self.sequence,
            "timestamp_ns": str(self.timestamp_ns),
        }


@dataclass(frozen=True)
class ArbitrageOpportunity:
    timestamp_ns: int
    pair: str
    quote_asset: str
    buy_exchange: str
    sell_exchange: str
    buy_price: Decimal
    sell_price: Decimal
    spread_pct: Decimal
    max_size: Decimal
    theoretical_profit: Decimal

    def as_payload(self) -> dict[str, object]:
        return {
            "timestamp_ns": str(self.timestamp_ns),
            "pair": self.pair,
            "quote_asset": self.quote_asset,
            "buy_exchange": self.buy_exchange,
            "sell_exchange": self.sell_exchange,
            "buy_price": str(self.buy_price),
            "sell_price": str(self.sell_price),
            "spread_pct": str(self.spread_pct),
            "max_size": str(self.max_size),
            "theoretical_profit": str(self.theoretical_profit),
        }


@dataclass(frozen=True)
class BookUpdateResult:
    accepted: bool
    reason: str | None = None
    top_of_book: TopOfBook | None = None
    stale: bool = False
    requires_resync: bool = False


@dataclass(frozen=True)
class BookEligibility:
    exchange: str
    pair: str
    initialized: bool
    continuous: bool
    connected: bool
    age_ns: int | None
    max_age_ns: int
    eligible: bool
    reason: str | None = None

    def display_signature(self) -> tuple[object, ...]:
        """What this status actually shows, for suppressing unchanged repeats.

        Deliberately excludes `age_ns`, which changes on every event even when
        nothing about the book has; comparing it would defeat suppression
        entirely. Reading these fields directly means an unchanged status never
        pays for building its payload.
        """
        return (self.initialized, self.continuous, self.connected, self.eligible, self.reason)

    def as_payload(self) -> dict[str, object]:
        return {
            "exchange": self.exchange,
            "pair": self.pair,
            "initialized": self.initialized,
            "continuous": self.continuous,
            "connected": self.connected,
            "age_ms": None if self.age_ns is None else self.age_ns // 1_000_000,
            "max_age_ms": self.max_age_ns // 1_000_000,
            "eligible": self.eligible,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class LiveMessage:
    type: Literal["top_of_book", "opportunity", "book_status", "state_snapshot"]
    payload: dict[str, object]
