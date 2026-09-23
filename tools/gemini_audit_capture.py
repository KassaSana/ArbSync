"""Record a normal capture whose Gemini socket also carries top-20 snapshots and trades.

The whole pipeline runs as in `arbsync capture` (every venue, reconciler,
detector); only the Gemini subscription adds `{symbol}@depth20` and
`{symbol}@trade`. Gemini's `@depth20` frames carry a `lastUpdateId` in the same
id space as `@depth`, so `tools/gemini_book_audit.py` can compare the
incremental book with Gemini's top 20 at the same update id. The production
adapter ignores both extra frame shapes, so the capture replays unchanged.

    uv run python tools/gemini_audit_capture.py --config var/arb047.toml \\
        --duration 2700 --output var/capture-arb047.jsonl.gz
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

from arb.adapters import ADAPTER_TYPES
from arb.adapters.gemini import GeminiAdapter
from arb.main import run_capture


class GeminiAuditAdapter(GeminiAdapter):
    """Gemini adapter that also subscribes each pair's top-20 snapshots and trades."""

    async def subscribe(self, websocket: Any) -> None:
        streams: list[str] = []
        for pair in self.pairs:
            symbol = pair.lower().replace("-", "")
            streams.extend((f"{symbol}@depth", f"{symbol}@depth20", f"{symbol}@trade"))
        await websocket.send(self.encode({"id": 1, "method": "SUBSCRIBE", "params": streams}))


def audit_adapter_types() -> list[type[Any]]:
    return [
        GeminiAuditAdapter if adapter_type is GeminiAdapter else adapter_type
        for adapter_type in ADAPTER_TYPES
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--duration", type=float, required=True, help="seconds")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(
        run_capture(args.config, args.duration, args.output, adapter_types=audit_adapter_types())
    )


if __name__ == "__main__":
    main()
