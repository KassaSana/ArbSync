from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

from arb.adapters.base import ExchangeAdapter
from arb.types import EventKind, MarketEvent, PriceLevel


def normalize_coinbase_symbol(symbol: str) -> str:
    return symbol.upper()


class CoinbaseAdapter(ExchangeAdapter):
    name = "coinbase"
    ws_url = "wss://advanced-trade-ws.coinbase.com"
    snapshot_url = "https://api.exchange.coinbase.com/products"
    normalize_symbol = staticmethod(normalize_coinbase_symbol)

    def __init__(self, pairs: list[str]) -> None:
        super().__init__(pairs)
        self._awaiting_snapshot: set[str] = set()
        self._initialized: set[str] = set()
        self._last_envelope_sequence: int | None = None

    async def reset_state(self) -> None:
        await super().reset_state()
        self._awaiting_snapshot.clear()
        self._initialized.clear()
        self._last_envelope_sequence = None

    async def subscribe(self, websocket: Any) -> None:
        await websocket.send(
            self.encode(
                {
                    "type": "subscribe",
                    "channel": "level2",
                    "product_ids": self.pairs,
                }
            )
        )
        await websocket.send(
            self.encode(
                {
                    "type": "subscribe",
                    "channel": "heartbeats",
                }
            )
        )

    async def parse_message(self, message: str) -> list[MarketEvent]:
        payload = json.loads(message)
        if payload.get("type") in {"snapshot", "update"} and payload.get("product_id"):
            return self._finalize_stream_events([self._build_event(payload)])

        channel = payload.get("channel")
        envelope_sequence = payload.get("sequence_num")
        if envelope_sequence is None:
            if channel == "l2_data":
                self.request_reconnect()
            return []
        if not self._accept_envelope_sequence(int(envelope_sequence)):
            return []
        if channel != "l2_data":
            return []

        grouped: dict[str, list[dict[str, Any]]] = {}
        for channel_event in payload.get("events", []):
            if channel_event.get("type") not in {"snapshot", "update"}:
                continue
            product_id = channel_event.get("product_id")
            if not product_id or normalize_coinbase_symbol(product_id) not in self.pairs:
                continue
            grouped.setdefault(product_id, []).append(channel_event)

        next_local_sequences = dict(self._last_sequence_by_pair)
        events: list[MarketEvent] = []
        for product_events in grouped.values():
            pair = normalize_coinbase_symbol(product_events[0]["product_id"])
            first_kind = product_events[0]["type"]
            if first_kind != "snapshot" and pair not in self._initialized:
                self._awaiting_snapshot.add(pair)
                self.request_reconnect()
                return []
            local_sequence = next_local_sequences.get(pair, 0) + 1
            event = self._combine_product_events(
                product_events, local_sequence, int(envelope_sequence)
            )
            if event is None:
                self.request_reconnect()
                return []
            events.append(event)
            next_local_sequences[pair] = local_sequence

        for event in events:
            self._last_sequence_by_pair[event.pair] = event.sequence
            if event.kind is EventKind.SNAPSHOT:
                self._initialized.add(event.pair)
                self._awaiting_snapshot.discard(event.pair)
        return events

    def _accept_envelope_sequence(self, sequence: int) -> bool:
        previous = self._last_envelope_sequence
        if previous is None:
            self._last_envelope_sequence = sequence
            return True
        if sequence <= previous:
            return False
        if sequence != previous + 1:
            self.gap_count += 1
            self._awaiting_snapshot.update(normalize_coinbase_symbol(pair) for pair in self.pairs)
            self.request_reconnect()
            return False
        self._last_envelope_sequence = sequence
        return True

    def _combine_product_events(
        self, events: list[dict[str, Any]], local_sequence: int, exchange_sequence: int
    ) -> MarketEvent | None:
        kinds = [event["type"] for event in events]
        if "snapshot" in kinds and kinds[0] != "snapshot":
            return None
        combined = {
            "type": "snapshot" if kinds[0] == "snapshot" else "update",
            "product_id": events[0]["product_id"],
            "updates": [update for event in events for update in event.get("updates", [])],
        }
        return self._build_event(combined, local_sequence, exchange_sequence)

    def _build_event(
        self,
        payload: dict[str, Any],
        local_sequence: int | None = None,
        exchange_sequence: int | None = None,
    ) -> MarketEvent:
        pair = normalize_coinbase_symbol(payload["product_id"])
        bids = tuple(
            PriceLevel(price=Decimal(update["price_level"]), size=Decimal(update["new_quantity"]))
            for update in payload.get("updates", [])
            if update["side"] == "bid"
        )
        asks = tuple(
            PriceLevel(price=Decimal(update["price_level"]), size=Decimal(update["new_quantity"]))
            for update in payload.get("updates", [])
            if update["side"] == "offer"
        )
        return MarketEvent(
            exchange=self.name,
            pair=pair,
            kind=EventKind.SNAPSHOT if payload.get("type") == "snapshot" else EventKind.DELTA,
            sequence=(
                local_sequence
                if local_sequence is not None
                else int(payload.get("sequence_num", 0))
            ),
            timestamp_ns=time.time_ns(),
            bids=bids,
            asks=asks,
            exchange_first_sequence=exchange_sequence,
            exchange_last_sequence=exchange_sequence,
        )

    def _finalize_stream_events(self, events: list[MarketEvent]) -> list[MarketEvent]:
        """Accept contiguous WebSocket events without mixing REST sequences."""
        accepted: list[MarketEvent] = []
        next_sequences = dict(self._last_sequence_by_pair)
        finalized: list[MarketEvent] = []
        for event in events:
            pair = event.pair
            if event.kind is EventKind.SNAPSHOT:
                next_sequences[pair] = event.sequence
                self._initialized.add(pair)
                accepted.append(event)
                continue

            if pair in self._awaiting_snapshot:
                self.request_reconnect()
                return []
            last_sequence = next_sequences.get(pair)
            if last_sequence is None:
                self._awaiting_snapshot.add(pair)
                self.request_reconnect()
                return []
            if event.sequence <= last_sequence:
                continue
            if event.sequence != last_sequence + 1:
                self.gap_count += 1
                self._awaiting_snapshot.add(pair)
                self.request_reconnect()
                return []
            next_sequences[pair] = event.sequence
            accepted.append(event)

        for event in accepted:
            self._last_sequence_by_pair[event.pair] = event.sequence
            if event.kind is EventKind.SNAPSHOT:
                self._awaiting_snapshot.discard(event.pair)
            finalized.append(event)
        return finalized

    async def fetch_snapshot(self, pair: str, trigger_sequence: int) -> MarketEvent:
        payload = await self.client_get_json(f"{self.snapshot_url}/{pair}/book?level=2")
        return MarketEvent(
            exchange=self.name,
            pair=pair,
            kind=EventKind.SNAPSHOT,
            sequence=max(int(payload.get("sequence", trigger_sequence)), trigger_sequence),
            timestamp_ns=time.time_ns(),
            bids=tuple(
                PriceLevel(price=Decimal(price), size=Decimal(size))
                for price, size, *_ in payload["bids"]
            ),
            asks=tuple(
                PriceLevel(price=Decimal(price), size=Decimal(size))
                for price, size, *_ in payload["asks"]
            ),
        )
