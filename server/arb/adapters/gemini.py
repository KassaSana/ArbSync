from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

from arb.adapters.base import ExchangeAdapter
from arb.types import EventKind, MarketEvent, PriceLevel


def normalize_gemini_symbol(symbol: str) -> str:
    s = symbol.upper()
    if s.endswith("USDT"):
        return f"{s[:-4]}-USDT"
    if s.endswith("USD"):
        return f"{s[:-3]}-USD"
    return s


class GeminiAdapter(ExchangeAdapter):
    name = "gemini"
    ws_url = "wss://ws.gemini.com?snapshot=-1"
    snapshot_url = "https://api.gemini.com/v1/book"

    def __init__(self, pairs: list[str]) -> None:
        super().__init__(pairs)
        self._initialized: set[str] = set()
        self._local_sequence: dict[str, int] = {}
        self._last_exchange_update_id: dict[str, int] = {}

    async def reset_state(self) -> None:
        await super().reset_state()
        self._initialized.clear()
        self._local_sequence.clear()
        self._last_exchange_update_id.clear()

    async def subscribe(self, websocket: Any) -> None:
        streams = [f"{pair.lower().replace('-', '')}@depth" for pair in self.pairs]
        await websocket.send(self.encode({"id": 1, "method": "SUBSCRIBE", "params": streams}))

    async def parse_message(self, message: str) -> list[MarketEvent]:
        payload = json.loads(message)
        if payload.get("e") != "depthUpdate":
            return []

        symbol = payload.get("s")
        if not symbol or "U" not in payload or "u" not in payload:
            self.request_reconnect()
            return []

        pair = normalize_gemini_symbol(str(symbol))
        first_id = int(payload["U"])
        last_id = int(payload["u"])
        if first_id > last_id:
            self.gap_count += 1
            self._restart_sync(pair)
            return []

        bids = tuple(
            PriceLevel(price=Decimal(price), size=Decimal(size))
            for price, size in payload.get("b", [])
        )
        asks = tuple(
            PriceLevel(price=Decimal(price), size=Decimal(size))
            for price, size in payload.get("a", [])
        )
        timestamp_ns = int(payload.get("E", time.time_ns()))

        if pair not in self._initialized:
            self._initialized.add(pair)
            self._local_sequence[pair] = 1
            self._last_exchange_update_id[pair] = last_id
            self._last_sequence_by_pair[pair] = 1
            return [
                MarketEvent(
                    exchange=self.name,
                    pair=pair,
                    kind=EventKind.SNAPSHOT,
                    sequence=1,
                    timestamp_ns=timestamp_ns,
                    bids=bids,
                    asks=asks,
                    exchange_first_sequence=first_id,
                    exchange_last_sequence=last_id,
                )
            ]

        previous_id = self._last_exchange_update_id[pair]
        if last_id <= previous_id:
            return []
        if first_id > previous_id + 1:
            self.gap_count += 1
            self._restart_sync(pair)
            return []

        sequence = self._local_sequence[pair] + 1
        self._local_sequence[pair] = sequence
        self._last_exchange_update_id[pair] = last_id
        self._last_sequence_by_pair[pair] = sequence
        return [
            MarketEvent(
                exchange=self.name,
                pair=pair,
                kind=EventKind.DELTA,
                sequence=sequence,
                timestamp_ns=timestamp_ns,
                bids=bids,
                asks=asks,
                exchange_first_sequence=first_id,
                exchange_last_sequence=last_id,
            )
        ]

    def _restart_sync(self, pair: str) -> None:
        self._initialized.discard(pair)
        self._local_sequence.pop(pair, None)
        self._last_exchange_update_id.pop(pair, None)
        self._last_sequence_by_pair.pop(pair, None)
        self.request_reconnect()

    async def fetch_snapshot(self, pair: str, trigger_sequence: int) -> MarketEvent:
        """Fetch a comparison snapshot for SnapshotReconciler, not stream recovery."""
        symbol = pair.replace("-", "").lower()
        payload = await self.client_get_json(
            f"{self.snapshot_url}/{symbol}?limit_bids=100&limit_asks=100"
        )
        return MarketEvent(
            exchange=self.name,
            pair=pair,
            kind=EventKind.SNAPSHOT,
            sequence=trigger_sequence,
            timestamp_ns=time.time_ns(),
            bids=tuple(
                PriceLevel(price=Decimal(level["price"]), size=Decimal(level["amount"]))
                for level in payload["bids"]
            ),
            asks=tuple(
                PriceLevel(price=Decimal(level["price"]), size=Decimal(level["amount"]))
                for level in payload["asks"]
            ),
        )
