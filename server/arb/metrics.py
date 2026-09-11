from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

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
adapter_reconnects_total = Counter(
    "arb_adapter_reconnects_total", "Adapter reconnect attempts", ["exchange", "reason"]
)
ws_clients = Gauge("arb_ws_clients", "Connected WebSocket dashboard clients")
ws_client_queue_overflows_total = Counter(
    "arb_ws_client_queue_overflows_total",
    "Dashboard clients disconnected because their outgoing queue filled",
)
reconcile_mismatches_total = Counter(
    "arb_reconcile_mismatches_total", "Snapshot reconciliation mismatches", ["exchange", "pair"]
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
background_task_failures_total = Counter(
    "arb_background_task_failures_total",
    "Unexpected background task exits",
    ["task"],
)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
