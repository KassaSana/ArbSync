"""Exercise the benchmark backend's start and shutdown path many times, quickly.

This is a lifecycle probe, not a capacity benchmark. It runs no browser, no
warmup and no dashboard build, so its CPU, latency and throughput are not
comparable with anything else in `artifacts/benchmarks/performance` and must
never be quoted as performance evidence. It answers one question: does the
backend start and exit cleanly, and if not, what did it hang on.

`tools/profile_pipeline.py` remains the measurement harness. A shutdown costs
roughly five seconds here against roughly twenty-five there, which is what makes
a rare shutdown fault worth sampling repeatedly.

Install: uv sync --locked --extra dev --extra perf
Run: python tools/shutdown_probe.py --cycles 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import websockets
from perf_feed import Feed

ROOT = Path(__file__).resolve().parents[1]
# Matches the measurement harness, so a forced exit means the same thing in both.
SHUTDOWN_TIMEOUT_SECONDS = 60


async def wait_ready(client: httpx.AsyncClient, process: subprocess.Popen[bytes]) -> None:
    for _ in range(300):
        if process.poll() is not None:
            raise RuntimeError("Backend exited before serving; see backend.log")
        try:
            if (await client.get("/readyz")).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.05)
    raise RuntimeError("Backend never became ready; see backend.log")


async def drain(live: Any) -> None:
    """Consume broadcasts so delivery and client teardown still run.

    A shutdown with no client attached would not exercise the broadcaster's
    disconnect path, which is part of what this probe is watching.
    """
    try:
        async for _ in live:
            pass
    except websockets.exceptions.ConnectionClosed:
        pass


async def cycle(args: argparse.Namespace, index: int, output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    feed = Feed(args.depth)
    server = await websockets.serve(
        feed.connect, "127.0.0.1", 0, process_request=feed.http, max_size=10_000_000
    )
    feed_port = server.sockets[0].getsockname()[1]
    log = (output / "backend.log").open("w")
    process = subprocess.Popen(
        [
            sys.executable,
            "tools/perf_backend.py",
            "--port",
            str(port),
            "--feed-port",
            str(feed_port),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    reader: asyncio.Task[None] | None = None
    ready_seconds = 0.0
    shutdown_seconds = 0.0
    forced = False
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
            await wait_ready(client, process)
            ready_seconds = time.perf_counter() - started
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws/live") as live:
                reader = asyncio.create_task(drain(live))
                (await client.post("/__bench/start")).raise_for_status()
                await feed.run(args.rate, args.seconds, record=True)
                for _ in range(200):
                    progress = (await client.get("/__bench/progress")).json()
                    if (
                        progress["processed"] >= len(feed.sent)
                        and progress["persistence_queue"] == 0
                    ):
                        break
                    await asyncio.sleep(0.05)
                processed = progress["processed"]
                (await client.post("/__bench/stop")).raise_for_status()
            await client.post("/__bench/shutdown")
        shutdown_started = time.perf_counter()
        try:
            await asyncio.to_thread(process.wait, SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            forced = True
            process.kill()
            await asyncio.to_thread(process.wait, 15)
        shutdown_seconds = time.perf_counter() - shutdown_started
    finally:
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, 10)
        log.close()
        server.close()
        await server.wait_closed()
    diagnostics = output / "shutdown.json"
    return {
        "cycle": index,
        "ready_seconds": round(ready_seconds, 3),
        "sent_events": len(feed.sent),
        "processed_events": processed,
        "shutdown_seconds": round(shutdown_seconds, 3),
        "shutdown_forced": forced,
        "exit_code": process.returncode,
        "shutdown_diagnostics": (
            json.loads(diagnostics.read_text()) if diagnostics.exists() else None
        ),
    }


async def main(args: argparse.Namespace) -> None:
    os.chdir(ROOT)
    # The backend mounts the built dashboard at startup and exits without it.
    # Say so here rather than leaving a readiness timeout to explain itself.
    if not (ROOT / "dashboard" / "dist-perf" / "index.html").exists():
        raise SystemExit(
            "Missing dashboard/dist-perf. Build it once with "
            "tools/profile_pipeline.py, or: npm run build -- --outDir dist-perf"
        )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"{args.label}-lifecycle-{stamp}"
    root = ROOT / "artifacts" / "benchmarks" / "performance"
    semaphore = asyncio.Semaphore(args.concurrency)

    async def guarded(index: int) -> dict[str, Any]:
        async with semaphore:
            print(f"Cycle {index}/{args.cycles}", flush=True)
            return await cycle(args, index, root / "raw" / name / str(index))

    started = time.perf_counter()
    outcomes = await asyncio.gather(
        *(guarded(index + 1) for index in range(args.cycles)), return_exceptions=True
    )
    elapsed = time.perf_counter() - started
    results: list[dict[str, Any]] = [
        {"cycle": index + 1, "error": repr(outcome)}
        if isinstance(outcome, BaseException)
        else outcome
        for index, outcome in enumerate(outcomes)
    ]
    report = {
        "label": args.label,
        "mode": "lifecycle",
        "created_utc": stamp,
        "command": sys.argv,
        "machine": {
            "platform": platform.platform(),
            "python": sys.version,
            "logical_cpus": os.cpu_count(),
        },
        "workload": {
            "cycles": args.cycles,
            "seconds": args.seconds,
            "rate_per_second": args.rate,
            "initial_levels_per_side": args.depth,
            "concurrency": args.concurrency,
        },
        "limits": (
            "Lifecycle probe, not a capacity benchmark: no browser, no warmup, no "
            "dashboard build. Reports startup and shutdown behavior only; its CPU, "
            "latency and throughput are not comparable with the measurement harness."
        ),
        "raw_directory": str((root / "raw" / name).relative_to(ROOT)),
        "elapsed_seconds": round(elapsed, 1),
        "results": results,
    }
    target = root / f"{name}.json"
    target.write_text(json.dumps(report, indent=2))
    forced = [r for r in results if r.get("shutdown_forced")]
    errors = [r for r in results if "error" in r]
    print(
        json.dumps(
            {
                "cycles": len(results),
                "forced_shutdowns": len(forced),
                "errors": len(errors),
                "seconds_per_cycle": round(elapsed / max(1, len(results)), 1),
                "max_shutdown_seconds": max(
                    (r.get("shutdown_seconds", 0.0) for r in results), default=0.0
                ),
            }
        ),
        flush=True,
    )
    print(f"Report: {target}", flush=True)
    if forced or errors:
        raise SystemExit("One or more cycles failed to start or exit cleanly")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="lifecycle")
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument("--seconds", type=float, default=2.0)
    parser.add_argument("--rate", type=int, default=1100)
    parser.add_argument("--depth", type=int, default=500)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="cycles to run at once; ports are allocated per cycle",
    )
    arguments = parser.parse_args()
    if arguments.cycles < 1 or arguments.seconds <= 0 or arguments.concurrency < 1:
        parser.error("cycles >= 1; seconds > 0; concurrency >= 1")
    if arguments.rate <= 0 or arguments.depth < 2:
        parser.error("rate must be positive; depth >= 2")
    if not arguments.label.replace("-", "").replace("_", "").isalnum():
        parser.error("label must contain only letters, digits, hyphens and underscores")
    asyncio.run(main(arguments))
