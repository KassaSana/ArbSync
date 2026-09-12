"""Deterministic local exchange feed; modeled load, not a captured market trace."""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from decimal import Decimal
from http import HTTPStatus
from urllib.parse import parse_qs, urlparse

ASSETS = ("BTC", "ETH", "SOL", "AVAX", "LINK", "LTC", "UNI", "DOT", "AAVE")
EXCHANGES = ("gemini", "coinbase", "binance")
# Approximately the venue mix in the committed five-minute smoke run.
VENUE_CYCLE = ("coinbase",) * 88 + ("gemini",) * 6 + ("binance",) * 6


def market_pair(exchange: str, asset: str) -> str:
    return f"{asset}-{'USDT' if exchange == 'binance' else 'USD'}"


def levels(exchange: str, asset: str, depth: int) -> tuple[list, list]:
    # Narrow markets with one persistently crossed *cross-venue* pair. This
    # deliberately exercises opportunities and SQLite, unlike the zero-opp soak.
    center = Decimal(100 + ASSETS.index(asset) * 100)
    if asset == "BTC" and exchange == "coinbase":
        center += Decimal("0.4")
    bids = [[str(center - Decimal("0.1") - Decimal(i) / 100), "2"] for i in range(depth)]
    asks = [[str(center + Decimal("0.1") + Decimal(i) / 100), "2"] for i in range(depth)]
    return bids, asks


class Feed:
    def __init__(self, depth: int = 500) -> None:
        self.depth = depth
        self.clients: dict = {}
        self.sequences: dict = defaultdict(lambda: 1)
        self.coinbase_sequence = 0
        self.counter = 0
        self.asset_counters: dict = defaultdict(int)
        self.sent: list = []
        self.schedule_lag_ms: list[float] = []

    def message(self, exchange: str, asset: str, *, snapshot: bool = False) -> tuple[str, int]:
        if snapshot:
            bids, asks = levels(exchange, asset, self.depth)
        else:
            # 80% deeper updates; 20% touch the best size. Insert/delete at
            # half ticks as well as replacing sizes so list maintenance is tested.
            base_bids, base_asks = levels(exchange, asset, 1)
            counter = self.asset_counters[(exchange, asset)]
            self.asset_counters[(exchange, asset)] += 1
            index = 0 if counter % 5 == 0 else 1 + counter % (self.depth - 1)
            offset = Decimal(index) / 100
            if index and counter % 4 in (0, 1):
                offset += Decimal("0.005")
                size = "0" if counter % 4 == 1 else "1.5"
            else:
                size = str(Decimal("1") + Decimal(counter % 10) / 10)
            bids = [[str(Decimal(base_bids[0][0]) - offset), size]]
            asks = [[str(Decimal(base_asks[0][0]) + offset), size]]
        if exchange == "coinbase":
            sequence = self.coinbase_sequence
            self.coinbase_sequence += 1
            payload = {
                "channel": "l2_data",
                "sequence_num": sequence,
                "events": [
                    {
                        "type": "snapshot" if snapshot else "update",
                        "product_id": f"{asset}-USD",
                        "updates": [
                            {"side": side, "price_level": p, "new_quantity": s}
                            for side, rows in (("bid", bids), ("offer", asks))
                            for p, s in rows
                        ],
                    }
                ],
            }
        else:
            key = exchange, asset
            if not snapshot:
                self.sequences[key] += 1
            sequence = self.sequences[key]
            payload = {
                "e": "depthUpdate",
                "E": time.time_ns(),
                "s": f"{asset}USDT" if exchange == "binance" else f"{asset.lower()}usd",
                "U": sequence,
                "u": sequence,
                "b": bids,
                "a": asks,
            }
        return json.dumps(payload, separators=(",", ":")), sequence

    async def connect(self, websocket) -> None:
        exchange = websocket.request.path.strip("/")
        if exchange not in EXCHANGES:
            await websocket.close()
            return
        await websocket.recv()  # subscription
        if exchange == "coinbase":
            await websocket.recv()  # heartbeat subscription
        for asset in ASSETS:
            message, _ = self.message(exchange, asset, snapshot=True)
            await websocket.send(message)
        self.clients[exchange] = websocket
        await websocket.wait_closed()

    def http(self, connection, request):
        parsed = urlparse(request.path)
        if parsed.path != "/snapshot":
            return None
        asset = parse_qs(parsed.query)["symbol"][0].removesuffix("USDT")
        bids, asks = levels("binance", asset, self.depth)
        return connection.respond(
            HTTPStatus.OK,
            json.dumps(
                {
                    "lastUpdateId": 1,
                    "bids": bids,
                    "asks": asks,
                }
            ),
        )

    async def run(self, rate: int, seconds: float, *, record: bool, burst_ms: int = 100) -> None:
        """Send fixed-size microbursts against an open-loop schedule.

        Every fifth second has 4x traffic; the other four seconds have 0.25x.
        Average rate is the requested rate, with no per-event acknowledgments.
        """
        tick_seconds = burst_ms / 1000
        ticks = round(seconds / tick_seconds)
        started = time.perf_counter()
        credit = 0.0
        for tick in range(ticks):
            target = started + tick * tick_seconds
            await asyncio.sleep(max(0, target - time.perf_counter()))
            if record:
                self.schedule_lag_ms.append(max(0, time.perf_counter() - target) * 1000)
            multiplier = 4 if int(tick * tick_seconds) % 5 == 4 else 0.25
            credit += rate * tick_seconds * multiplier
            count = int(credit)
            credit -= count
            for _ in range(count):
                exchange = VENUE_CYCLE[self.counter % len(VENUE_CYCLE)]
                asset = ASSETS[(self.counter // len(VENUE_CYCLE) + self.counter) % len(ASSETS)]
                message, sequence = self.message(exchange, asset)
                sent_ns = time.perf_counter_ns()
                await self.clients[exchange].send(message)
                if record:
                    self.sent.append([exchange, market_pair(exchange, asset), sequence, sent_ns])
                self.counter += 1
        await asyncio.sleep(max(0, started + ticks * tick_seconds - time.perf_counter()))
