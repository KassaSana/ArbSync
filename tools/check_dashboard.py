"""Browser regression check for batched quotes, invalidation and reconnection.

Run after building dashboard/dist-perf (profile_pipeline.py builds it).
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


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


async def check(url: str) -> None:
    quote = {
        "exchange": "gemini",
        "pair": "BTC-USD",
        "best_bid_price": "100",
        "best_bid_size": "1",
        "best_ask_price": "101",
        "best_ask_size": "1",
        "sequence": 1,
        "timestamp_ns": "1",
    }
    status = {
        "exchange": "gemini",
        "pair": "BTC-USD",
        "eligible": True,
        "initialized": True,
        "continuous": True,
        "connected": True,
        "age_ms": 0,
        "max_age_ms": 60000,
        "reason": None,
    }
    responses = {
        "/api/pairs": [{"exchange": "gemini", "pair": "BTC-USD"}],
        "/api/book-status": [status],
        "/api/adapters": [],
        "/api/opportunities/recent": [],
        "/api/stats": {"count": 0, "max_spread_pct": "0", "total_theoretical_profit_usd": "0"},
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
            await expect(page.get_by_text("Dashboard stream: connected")).to_be_visible()

            async def emit(messages: list[dict[str, Any]]) -> None:
                await page.evaluate(
                    "messages => messages.forEach(m => window.testSocket.emit(m))", messages
                )

            def message(kind: str, payload: Any, sequence: int) -> dict[str, Any]:
                return {"type": kind, "payload": payload, "stream_sequence": sequence}

            await emit([message("state_snapshot", {"books": [quote], "statuses": [status]}, 1)])
            await expect(page.get_by_text("gemini: 100 / 101")).to_be_visible()
            changed = {**quote, "best_bid_price": "102", "best_ask_price": "103"}
            await emit(
                [
                    message("top_of_book", changed, 2),
                    message("book_status", {**status, "eligible": False}, 3),
                ]
            )
            await page.wait_for_timeout(120)
            await expect(page.locator("tbody")).not_to_contain_text("gemini:")

            # A new snapshot supersedes both buffered quotes and stale sequences.
            restored = {**quote, "best_bid_price": "104", "best_ask_price": "105"}
            await emit(
                [
                    message("top_of_book", changed, 4),
                    message("state_snapshot", {"books": [restored], "statuses": [status]}, 5),
                    message("top_of_book", quote, 2),
                ]
            )
            await page.wait_for_timeout(120)
            await expect(page.get_by_text("gemini: 104 / 105")).to_be_visible()
            await expect(page.locator("tbody")).not_to_contain_text("gemini: 102")

            opportunities = [
                message(
                    "opportunity",
                    {
                        "timestamp_ns": str(i),
                        "pair": "BTC-USD",
                        "buy_exchange": "gemini",
                        "sell_exchange": "coinbase",
                        "buy_price": "100",
                        "sell_price": "101",
                        "spread_pct": "1",
                        "max_size": "1",
                        "theoretical_profit_usd": str(i),
                    },
                    i + 10,
                )
                for i in range(120)
            ]
            await emit(opportunities)
            await expect(page.get_by_text("50 tracked")).to_be_visible()
            await expect(page.get_by_text("Theoretical profit: $119", exact=True)).to_be_visible()
            await expect(page.get_by_text("Theoretical profit: $69", exact=True)).to_have_count(0)
            await emit([message("top_of_book", changed, 1000)])
            await page.evaluate("window.testSocket.close()")
            await page.wait_for_timeout(120)
            await expect(page.locator("tbody")).not_to_contain_text("gemini:")
            assert not errors, errors
            print(
                json.dumps(
                    {
                        "passed": [
                            "invalidation clears pending quotes",
                            "snapshot supersedes buffered state",
                            "old stream sequence ignored",
                            "opportunity burst retains newest 50",
                            "disconnect hides pending quotes",
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
