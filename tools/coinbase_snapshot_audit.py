"""Short Coinbase audit against a fresh single-product Level 2 subscription.

For each configured product, the script maintains the incremental book, then
unsubscribes and resubscribes that product on the same socket. It compares the
old book with the replacement WebSocket snapshot and a nearby REST snapshot.
These comparisons are time-separated, unlike Binance.US exact-id checks.

    python tools/coinbase_snapshot_audit.py --max-seconds 300
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import tomllib
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import websockets
from arb.adapters.base import parse_levels
from arb.adapters.coinbase import CoinbaseAdapter
from arb.orderbook import OrderBookManager
from arb.types import EventKind, PriceLevel
from binance_snapshot_audit import compare

Levels = tuple[list[PriceLevel], list[PriceLevel]]
DEPTH = 20


def _message(kind: str, pair: str, channel: str) -> str:
    return json.dumps({"type": kind, "product_ids": [pair], "channel": channel})


async def probe(pair: str, deadline: float) -> dict[str, Any]:
    adapter = CoinbaseAdapter([pair])
    manager = OrderBookManager(max_age_seconds=60)
    manager.set_exchange_connected("coinbase", True)
    trade_prices: list[Decimal] = []
    trade_cutoff_wall_ns = 0
    last_action: dict[tuple[int, Decimal], tuple[int, bool]] = {}
    snapshots = 0
    last_snapshot_ns = 0
    sequence_gaps = 0

    async with websockets.connect(adapter.ws_url, max_size=10_000_000) as socket:
        for channel in ("level2", "heartbeats", "market_trades"):
            await socket.send(_message("subscribe", pair, channel))

        async def receive() -> bool:
            nonlocal snapshots, last_snapshot_ns, sequence_gaps
            timeout = min(12.0, deadline - time.monotonic())
            if timeout <= 0:
                raise TimeoutError("audit deadline reached")
            raw = await asyncio.wait_for(socket.recv(), timeout)
            if isinstance(raw, bytes):
                raw = raw.decode()
            now_ns = time.monotonic_ns()
            payload = json.loads(raw)
            if payload.get("channel") == "market_trades":
                for entry in payload.get("events", []):
                    for trade in entry.get("trades", []):
                        if trade.get("product_id") == pair:
                            stamp = datetime.fromisoformat(
                                str(trade["time"]).replace("Z", "+00:00")
                            )
                            if int(stamp.timestamp() * 1_000_000_000) >= trade_cutoff_wall_ns:
                                trade_prices.append(Decimal(str(trade["price"])))
            for event in await adapter.parse_message(raw):
                result = manager.apply(event)
                if not result.accepted:
                    raise RuntimeError(f"{pair} book rejected {event.kind.value}: {result.reason}")
                if event.kind is EventKind.DELTA:
                    for side, levels in enumerate((event.bids, event.asks)):
                        for level in levels:
                            last_action[(side, level.price)] = (now_ns, level.size == 0)
                if event.kind is EventKind.SNAPSHOT:
                    snapshots += 1
                    last_snapshot_ns = now_ns
            if adapter._reconnect_requested:
                sequence_gaps += 1
                raise RuntimeError(f"{pair} stream requested reconnect")
            return snapshots > 0

        while not await receive():
            pass
        settle_until = min(deadline, time.monotonic() + 2.0)
        while time.monotonic() < settle_until:
            try:
                await asyncio.wait_for(receive(), min(0.5, settle_until - time.monotonic()))
            except TimeoutError:
                pass
        old = manager.level_snapshot("coinbase", pair, DEPTH)
        if old is None:
            raise RuntimeError(f"{pair} has no initialized book")
        old_ns = time.monotonic_ns()
        trade_cutoff_wall_ns = time.time_ns()
        await socket.send(_message("unsubscribe", pair, "level2"))
        await asyncio.sleep(0.1)
        await socket.send(_message("subscribe", pair, "level2"))
        while snapshots < 2:
            await receive()
        fresh = manager.level_snapshot("coinbase", pair, DEPTH)
        if fresh is None:
            raise RuntimeError(f"{pair} replacement snapshot was rejected")
        gap_ms = (last_snapshot_ns - old_ns) / 1_000_000

        before_rest = manager.level_snapshot("coinbase", pair, DEPTH)
        assert before_rest is not None
        rest_request_ns = time.monotonic_ns()
        async with httpx.AsyncClient(timeout=10) as client:
            request = asyncio.create_task(client.get(f"{adapter.snapshot_url}/{pair}/book?level=2"))
            while not request.done():
                try:
                    await asyncio.wait_for(receive(), 0.2)
                except TimeoutError:
                    pass
            response = await request
            response.raise_for_status()
            rest_payload = response.json()
        rest_ns = time.monotonic_ns()
        after_rest = manager.level_snapshot("coinbase", pair, DEPTH)
        assert after_rest is not None
        rest: Levels = (
            list(parse_levels(rest_payload["bids"])[:DEPTH]),
            list(parse_levels(rest_payload["asks"])[:DEPTH]),
        )
        comparison = compare(old, fresh)
        rest_only = {
            (side, level.price)
            for side in range(2)
            for level in rest[side]
            if level.price not in {item.price for item in before_rest[side]}
            and level.price not in {item.price for item in after_rest[side]}
        }
        deleted_before_request = sum(
            1
            for key in rest_only
            if key in last_action and last_action[key][1] and last_action[key][0] <= rest_request_ns
        )
        return {
            "pair": pair,
            "old_vs_fresh": dict(comparison),
            "rest_vs_before_request": dict(compare(before_rest, rest)),
            "rest_vs_after_response": dict(compare(after_rest, rest)),
            "old_to_fresh_gap_ms": round(gap_ms, 3),
            "rest_request_ms": round((rest_ns - rest_request_ns) / 1_000_000, 3),
            "rest_only_at_both_boundaries": len(rest_only),
            "rest_only_last_stream_action_deleted": deleted_before_request,
            "trades_since_resubscribe": len(trade_prices),
            "rest_only_prices_traded_during_probe": len(
                {price for _side, price in rest_only} & set(trade_prices)
            ),
            "sequence_gaps": sequence_gaps,
        }


async def analyze(config: Path, max_seconds: float) -> dict[str, Any]:
    with config.open("rb") as stream:
        settings = tomllib.load(stream)
    pairs = [str(pair) for pair in settings["exchanges"]["coinbase"]]
    deadline = time.monotonic() + min(max_seconds, 300)
    outcomes: list[dict[str, Any]] = []
    for pair in pairs:
        if deadline - time.monotonic() < 15:
            break
        try:
            outcomes.append(await probe(pair, deadline))
        except (
            OSError,
            RuntimeError,
            TimeoutError,
            httpx.HTTPError,
            websockets.WebSocketException,
        ) as exc:
            outcomes.append({"pair": pair, "error": repr(exc)})
    summary: Counter[str] = Counter()
    for result in outcomes:
        summary["pairs_probed" if "error" not in result else "pairs_failed"] += 1
        if "error" not in result:
            summary["rest_only_at_both_boundaries"] += result["rest_only_at_both_boundaries"]
            summary["rest_only_last_stream_action_deleted"] += result[
                "rest_only_last_stream_action_deleted"
            ]
    return {
        "method": "single-product resubscription; old/fresh/REST comparisons are time-separated",
        "max_seconds": min(max_seconds, 300),
        "summary": dict(summary),
        "pairs": outcomes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--max-seconds", type=float, default=300)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(analyze(args.config, args.max_seconds)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
