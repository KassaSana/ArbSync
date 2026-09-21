"""Browser regression check for batched quotes, invalidation and reconnection.

Run after building dashboard/dist-perf (profile_pipeline.py builds it).

REST mocks and emitted live messages come from the pinned wire fixtures in
server/tests/fixtures/wire/, so this exercises the current wire contract
instead of hand-written payloads. Only the fields each scenario needs vary
from the fixtures.
"""

from __future__ import annotations

import asyncio
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
WIRE = ROOT / "server" / "tests" / "fixtures" / "wire"


def wire(name: str) -> Any:
    return json.loads((WIRE / name).read_text())


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


async def check(url: str) -> None:
    quote = dict(wire("live_top_of_book.json")["payload"])
    status = next(
        entry
        for entry in wire("book_status.json")
        if entry["exchange"] == "gemini" and entry["pair"] == "BTC-USD"
    )
    opportunity = dict(wire("live_opportunity.json")["payload"])
    responses = {
        "/api/pairs": wire("pairs.json"),
        "/api/book-status": wire("book_status.json"),
        "/api/adapters": wire("adapters.json"),
        "/api/opportunities/recent": wire("opportunities_recent.json"),
        "/api/stats": wire("stats.json"),
        "/api/pricing/depth": wire("pricing_depth.json"),
    }
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        try:
            page = await browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            async def route_api(route: Any) -> None:
                path = "/" + route.request.url.split("/", 3)[-1].split("?")[0]
                await route.fulfill(json=responses[path])

            await page.route("**/api/**", route_api)
            await page.add_init_script("""window.WebSocket = class {
              constructor() {
                window.testSocket = this;
                setTimeout(() => this.onopen?.(), 0);
              }
              send() {}
              close() { this.onclose?.(); }
              emit(message) { this.onmessage?.({data: JSON.stringify(message)}); }
            };""")
            await page.goto(url)
            await expect(page.get_by_text("Live", exact=True)).to_be_visible()

            async def emit(messages: list[dict[str, Any]]) -> None:
                await page.evaluate(
                    "messages => messages.forEach(m => window.testSocket.emit(m))", messages
                )

            def message(kind: str, payload: Any, sequence: int) -> dict[str, Any]:
                return {"type": kind, "payload": payload, "stream_sequence": sequence}

            spreads = page.locator("section", has=page.get_by_role("heading", name="Live spreads"))
            coinbase_quote = {
                **quote,
                "exchange": "coinbase",
                "best_bid_price": "95",
                "best_ask_price": "96",
            }
            coinbase_status = next(
                entry
                for entry in wire("book_status.json")
                if entry["exchange"] == "coinbase" and entry["pair"] == "BTC-USD"
            )

            await emit(
                [
                    message(
                        "state_snapshot",
                        {
                            "books": [quote, coinbase_quote],
                            "statuses": [status, coinbase_status],
                        },
                        1,
                    )
                ]
            )
            # Best bid 99 (gemini) over best ask 96 (coinbase): a 3.125% spread.
            await expect(spreads.get_by_text("99.0000", exact=True)).to_be_visible()
            await expect(spreads.get_by_text("3.125%", exact=True)).to_be_visible()
            # The /api/stats mock is the pinned stats.json fixture, whose
            # theoretical_profit_by_quote is {"USD": "9.69"}.
            await expect(page.get_by_text("$9.69", exact=True)).to_be_visible()

            # Invalidation clears the pending quote: the gemini update lands first,
            # then the ineligible status evicts the book in the same batch.
            changed = {**quote, "best_bid_price": "101", "best_ask_price": "102"}
            await emit(
                [
                    message("top_of_book", changed, 2),
                    message("book_status", {**status, "eligible": False}, 3),
                ]
            )
            await page.wait_for_timeout(120)
            await expect(spreads.get_by_text("99.0000", exact=True)).to_have_count(0)
            await expect(spreads.get_by_text("101.0000", exact=True)).to_have_count(0)

            # A new snapshot supersedes both buffered quotes and stale sequences.
            changed2 = {**quote, "best_bid_price": "105", "best_ask_price": "106"}
            restored = {**quote, "best_bid_price": "104", "best_ask_price": "105"}
            await emit(
                [
                    message("top_of_book", changed2, 4),
                    message(
                        "state_snapshot",
                        {
                            "books": [restored, coinbase_quote],
                            "statuses": [status, coinbase_status],
                        },
                        5,
                    ),
                    message("top_of_book", quote, 2),
                ]
            )
            await page.wait_for_timeout(120)
            await expect(spreads.get_by_text("104.0000", exact=True)).to_be_visible()
            await expect(spreads.get_by_text("105.0000", exact=True)).to_have_count(0)
            await expect(spreads.get_by_text("99.0000", exact=True)).to_have_count(0)

            base_ns = 1720000000000000000
            opportunities = [
                message(
                    "opportunity",
                    {
                        **opportunity,
                        "start_ns": str(base_ns + i),
                        "peak_profit": str(i),
                    },
                    i + 10,
                )
                for i in range(120)
            ]
            await emit(opportunities)
            await expect(page.get_by_text("50 episodes")).to_be_visible()
            await expect(page.get_by_text("$119.00", exact=True)).to_be_visible()
            await expect(page.get_by_text("$69.00", exact=True)).to_have_count(0)

            # The batched quote applies, then the drop surfaces as reconnecting
            # state with the last values kept on screen and marked old.
            final = {**quote, "best_bid_price": "110", "best_ask_price": "111"}
            await emit([message("top_of_book", final, 1000)])
            await page.evaluate("window.testSocket.close()")
            await page.wait_for_timeout(120)
            await expect(spreads.get_by_text("110.0000", exact=True)).to_be_visible()
            await expect(page.get_by_text("Reconnecting", exact=True)).to_be_visible()
            await expect(page.get_by_text("Live feed interrupted")).to_be_visible()
            assert not errors, errors
            print(
                json.dumps(
                    {
                        "passed": [
                            "invalidation clears pending quotes",
                            "snapshot supersedes buffered state",
                            "old stream sequence ignored",
                            "opportunity burst retains newest 50",
                            "disconnect surfaces reconnecting state",
                        ]
                    }
                )
            )
        finally:
            await browser.close()


if __name__ == "__main__":
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(QuietHandler, directory=str(ROOT / "dashboard" / "dist-perf"))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        asyncio.run(check(f"http://127.0.0.1:{server.server_port}"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
