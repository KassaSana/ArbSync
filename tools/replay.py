from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from arb.adapters.binance import BinanceAdapter
from arb.adapters.coinbase import CoinbaseAdapter
from arb.adapters.gemini import GeminiAdapter
from arb.orderbook import OrderBookManager


def adapter_for_path(path: Path):
    name = path.stem.split("_", 1)[0]
    if name == "gemini":
        return GeminiAdapter(["btcusd"])
    if name == "coinbase":
        return CoinbaseAdapter(["BTC-USD"])
    if name == "binance":
        return BinanceAdapter(["BTCUSDT"])
    raise ValueError(f"Unsupported fixture name: {path.name}")


async def replay_capture(path: Path) -> dict[str, int | str]:
    adapter = adapter_for_path(path)
    manager = OrderBookManager()
    crashes = 0
    crossed_or_gap_events = 0
    event_count = 0
    parsed_pair: str | None = None

    for line in path.read_text().splitlines():
        payload = json.loads(line)
        message = payload["message"] if isinstance(payload, dict) and "message" in payload else line
        try:
            events = await adapter.parse_message(message)
        except Exception:
            crashes += 1
            continue
        for event in events:
            event_count += 1
            parsed_pair = event.pair
            result = manager.apply(event)
            if result.reason in {"crossed_book", "sequence_gap"}:
                crossed_or_gap_events += 1

    top = (
        manager.snapshot(adapter.name, parsed_pair).top_of_book if parsed_pair is not None else None
    )
    return {
        "exchange": adapter.name,
        "pair": parsed_pair or "",
        "messages": len(path.read_text().splitlines()),
        "events": event_count,
        "crashes": crashes,
        "crossed_or_gap_events": crossed_or_gap_events,
        "has_top_of_book": int(top is not None),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay recorded websocket messages.")
    parser.add_argument(
        "path", type=Path, help="Path to a JSONL capture file or directory of capture files"
    )
    args = parser.parse_args()

    paths = [args.path] if args.path.is_file() else sorted(args.path.glob("*.jsonl"))
    if not paths:
        raise SystemExit("No capture files found.")

    import asyncio

    for path in paths:
        print(json.dumps(asyncio.run(replay_capture(path)), indent=2))


if __name__ == "__main__":
    main()
