from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from arb.types import RouteAgeEvent

events_ingested_total = Counter("arb_events_ingested_total", "Market events ingested", ["exchange"])
book_updates_total = Counter(
    "arb_book_updates_total", "Accepted order book updates", ["exchange", "pair"]
)
opportunities_total = Counter(
    "arb_opportunities_total", "Arbitrage opportunities emitted", ["pair"]
)
detection_latency_seconds = Histogram(
    "arb_detection_latency_seconds", "Detection latency in seconds"
)
book_staleness_seconds = Gauge(
    "arb_book_staleness_seconds", "Age of last accepted book update", ["exchange", "pair"]
)
book_eligible = Gauge(
    "arb_book_eligible", "Whether an order book is eligible for detection", ["exchange", "pair"]
)
# Route-leg age diagnostics at episode open, on the local monotonic clock.
# Observed, not gating: eligibility stays a per-book decision. Buckets run from
# 10 ms (below the ~15.6 ms Windows monotonic tick, so the lowest ones fill only
# on finer clocks) to the default 30 s book age limit and beyond.
_AGE_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
route_open_leg_age_seconds = Histogram(
    "arb_route_open_leg_age_seconds",
    "Receipt age of each leg's book when a route episode opened",
    ["pair", "exchange", "leg"],
    buckets=_AGE_BUCKETS,
)
route_open_age_skew_seconds = Histogram(
    "arb_route_open_age_skew_seconds",
    "Absolute difference between the two legs' receipt ages when a route episode opened",
    ["pair", "buy_exchange", "sell_exchange"],
    buckets=_AGE_BUCKETS,
)
adapter_reconnects_total = Counter(
    "arb_adapter_reconnects_total", "Adapter reconnect attempts", ["exchange", "reason"]
)
adapter_pair_resyncs_total = Counter(
    "arb_adapter_pair_resyncs_total",
    "Single-pair resynchronizations that kept the shared connection",
    ["exchange", "trigger"],
)
ws_clients = Gauge("arb_ws_clients", "Connected WebSocket dashboard clients")
ws_client_queue_overflows_total = Counter(
    "arb_ws_client_queue_overflows_total",
    "Dashboard clients disconnected because their outgoing queue filled",
)
ws_sender_failures_total = Counter(
    "arb_ws_sender_failures_total",
    "Unexpected failures while sending messages to dashboard clients",
)
reconcile_mismatches_total = Counter(
    "arb_reconcile_mismatches_total", "Snapshot reconciliation mismatches", ["exchange", "pair"]
)
reconcile_evidence_total = Counter(
    "arb_reconcile_evidence_total",
    "Corroborated reconciliation mismatch evidence by cause",
    ["exchange", "pair", "kind"],
)
reconcile_confirmations_total = Counter(
    "arb_reconcile_confirmations_total",
    "Snapshot reconciliation mismatches that reached the confirmation threshold",
    ["exchange", "pair"],
)
reconcile_recoveries_total = Counter(
    "arb_reconcile_recoveries_total",
    "Reconciliation-triggered recovery lifecycle events",
    ["exchange", "pair", "outcome"],
)
reconcile_failures_total = Counter(
    "arb_reconcile_failures_total",
    "Failures while reconciling or requesting recovery",
    ["exchange", "pair", "phase"],
)
persistence_queue_drops_total = Counter(
    "arb_persistence_queue_drops_total",
    "Opportunities not accepted for persistence",
    ["reason"],
)
persistence_unflushed_rows = Gauge(
    "arb_persistence_unflushed_rows",
    "Accepted opportunities not yet committed to SQLite",
)
capture_frames_total = Counter(
    "arb_capture_frames_total",
    "Capture frames accepted for writing",
    ["exchange", "kind"],
)
capture_drops_total = Counter(
    "arb_capture_drops_total",
    "Capture frames not accepted for writing",
    ["reason"],
)
capture_unflushed_frames = Gauge(
    "arb_capture_unflushed_frames",
    "Accepted capture frames not yet written to disk",
)
background_task_failures_total = Counter(
    "arb_background_task_failures_total",
    "Unexpected background task exits",
    ["task"],
)


@dataclass(frozen=True)
class BookMetrics:
    """The per-book metric children resolved once instead of per event.

    `labels()` re-validates and re-hashes its arguments on every call, which the
    ingestion path would otherwise pay several times for every accepted update
    even though a book's label values never change.
    """

    ingested: Counter
    updates: Counter
    eligible: Gauge
    staleness: Gauge


@cache
def book_metrics(exchange: str, pair: str) -> BookMetrics:
    return BookMetrics(
        ingested=events_ingested_total.labels(exchange=exchange),
        updates=book_updates_total.labels(exchange=exchange, pair=pair),
        eligible=book_eligible.labels(exchange=exchange, pair=pair),
        staleness=book_staleness_seconds.labels(exchange=exchange, pair=pair),
    )


@cache
def opportunity_counter(pair: str) -> Counter:
    return opportunities_total.labels(pair=pair)


@dataclass(frozen=True)
class RouteMetrics:
    buy_age: Histogram
    sell_age: Histogram
    skew: Histogram


@cache
def route_metrics(pair: str, buy_exchange: str, sell_exchange: str) -> RouteMetrics:
    return RouteMetrics(
        buy_age=route_open_leg_age_seconds.labels(pair=pair, exchange=buy_exchange, leg="buy"),
        sell_age=route_open_leg_age_seconds.labels(pair=pair, exchange=sell_exchange, leg="sell"),
        skew=route_open_age_skew_seconds.labels(
            pair=pair, buy_exchange=buy_exchange, sell_exchange=sell_exchange
        ),
    )


def observe_route_open(event: RouteAgeEvent) -> None:
    """Record leg ages for an episode that just opened; other event kinds are ignored."""
    if event.kind != "open":
        return
    metrics = route_metrics(event.pair, event.buy_exchange, event.sell_exchange)
    ages = event.ages
    if ages.buy_age_ns is not None:
        metrics.buy_age.observe(ages.buy_age_ns / 1e9)
    if ages.sell_age_ns is not None:
        metrics.sell_age.observe(ages.sell_age_ns / 1e9)
    if ages.skew_ns is not None:
        metrics.skew.observe(ages.skew_ns / 1e9)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
