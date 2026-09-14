"""Measure the production pipeline with an actual Chromium dashboard connected.

Install: uv sync --locked --extra dev --extra perf
Run: python tools/profile_pipeline.py --label baseline
Use --profile for a separate cProfile + React production profiling run.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psutil
import websockets
from perf_feed import Feed
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


def source_fingerprint() -> str:
    digest = hashlib.sha256()
    files = [
        *ROOT.glob("server/arb/**/*.py"),
        *ROOT.glob("dashboard/src/**/*.tsx"),
        *ROOT.glob("dashboard/src/**/*.ts"),
        ROOT / "dashboard/vite.config.ts",
        *[
            ROOT / "tools" / name
            for name in ("perf_feed.py", "perf_backend.py", "perf_stages.py", "profile_pipeline.py")
        ],
    ]
    for path in sorted(files):
        digest.update(str(path.relative_to(ROOT)).replace("\\", "/").encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


BROWSER_INIT = """() => {
  const state = {active: false, messages: 0, bytes: 0, connections: 0,
    closes: [], frames: [], longTasks: [], lastFrame: null};
  const NativeWebSocket = window.WebSocket;
  window.WebSocket = class extends NativeWebSocket {
    constructor(...args) {
      super(...args);
      this.addEventListener('open', () => { if (state.active) state.connections++; });
      this.addEventListener('close', e => { if (state.active) state.closes.push(e.code); });
      this.addEventListener('message', e => {
        if (state.active) { state.messages++; state.bytes += e.data.length; }
      });
    }
  };
  new PerformanceObserver(list => {
    if (state.active) for (const e of list.getEntries()) state.longTasks.push(e.duration);
  }).observe({type: 'longtask', buffered: false});
  function frame(now) {
    if (state.active && state.lastFrame !== null) state.frames.push(now - state.lastFrame);
    state.lastFrame = now;
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
  window.__arbBrowser = {
    start() {
      Object.assign(state, {active: true, messages: 0, bytes: 0, connections: 0,
        closes: [], frames: [], longTasks: [], lastFrame: null});
      window.__arbProfile = {samples: [], dropped: 0};
    },
    stop() { state.active = false; return {...state, profile: window.__arbProfile}; }
  };
}"""


def distribution(values) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "p99": None, "max": None}
    return {
        "count": len(ordered),
        "mean": sum(ordered) / len(ordered),
        **{f"p{p}": ordered[max(0, math.ceil(len(ordered) * p / 100) - 1)] for p in (50, 95, 99)},
        "max": ordered[-1],
    }


def summarize(backend, feed, browser, resources, cdp_start, cdp_end, elapsed, database):
    sent = {(e, p, seq): ns for e, p, seq, ns in feed.sent}
    received_ms, detected_ms, completed_ms, sender_ms = [], [], [], []
    unmatched = 0
    for e, p, seq, receipt, detected, completed in backend["rows"]:
        send = sent.get((e, p, seq))
        if send is None:
            unmatched += 1
            continue
        if detected is not None:
            received_ms.append((detected - receipt) / 1e6)
            sender_ms.append((detected - send) / 1e6)
        completed_ms.append((completed - receipt) / 1e6)
        detected_ms.append((receipt - send) / 1e6)
    queues = backend["queue_samples"]
    profiles: dict = defaultdict(list)
    for name, actual, _base, _commit in browser["profile"]["samples"]:
        profiles[name].append(actual)
    with sqlite3.connect(database) as db:
        rows = db.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    return {
        "sent_events": len(sent),
        "processed_events": len(backend["rows"]),
        "unmatched_events": unmatched,
        "missing_events": len(sent.keys() - {(r[0], r[1], r[2]) for r in backend["rows"]}),
        "measurement_seconds_including_drain": elapsed,
        "latency_ms": {
            "receive_to_detection": distribution(received_ms),
            "receive_to_handler_complete": distribution(completed_ms),
            "send_to_receive": distribution(detected_ms),
            "send_to_detection": distribution(sender_ms),
            "event_loop_lag": distribution(backend["event_loop_lag_ms"]),
            "generator_schedule_lag": distribution(feed.schedule_lag_ms),
        },
        "backend_cpu_percent_one_core": distribution([r[1] for r in resources]),
        "backend_rss_mib": distribution([r[2] for r in resources]),
        "backend_rss_change_mib": resources[-1][2] - resources[0][2] if resources else None,
        "browser_cpu_percent_one_core": distribution([r[3] for r in resources]),
        "browser_rss_sum_mib": distribution([r[4] for r in resources]),
        "queue_drops": backend["counters"],
        "sampled_queue_max": {
            "persistence": max((q[0] for q in queues), default=0),
            "dashboard": max((q[1] for q in queues), default=0),
        },
        "connected_clients_min": min((q[2] for q in queues), default=0),
        "persisted_rows_including_warmup": rows,
        "database_mib": database.stat().st_size / 1024**2,
        "browser": {
            "messages": browser["messages"],
            "payload_bytes": browser["bytes"],
            "reconnections": browser["connections"],
            "close_codes": browser["closes"],
            "frame_interval_ms": distribution(browser["frames"]),
            "frames_over_50ms": sum(v > 50 for v in browser["frames"]),
            "long_task_ms": distribution(browser["longTasks"]),
            "react_render_ms": {
                k: {**distribution(v), "total": sum(v)} for k, v in profiles.items()
            },
            "profile_samples_dropped": browser["profile"]["dropped"],
            "cdp_duration_seconds": {
                k: cdp_end[k] - cdp_start[k]
                for k in ("TaskDuration", "ScriptDuration", "LayoutDuration", "RecalcStyleDuration")
            },
            "js_heap_start_mib": cdp_start["JSHeapUsedSize"] / 1024**2,
            "js_heap_end_mib": cdp_end["JSHeapUsedSize"] / 1024**2,
        },
        "failures": backend["failures"],
        "adapters": backend["adapters"],
    }


async def resource_sampler(pid, rows, stop):
    backend = psutil.Process(pid)
    tracked = {}
    backend.cpu_percent()
    while not stop.is_set():
        cpu, rss = 0.0, 0
        for child in psutil.Process().children(recursive=True):
            try:
                if child.name().lower() not in ("chrome.exe", "msedge.exe", "chrome", "chromium"):
                    continue
                proc = tracked.setdefault(child.pid, child)
                cpu += proc.cpu_percent()
                rss += proc.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        rows.append(
            [
                time.monotonic(),
                backend.cpu_percent(),
                backend.memory_info().rss / 1024**2,
                cpu,
                rss / 1024**2,
            ]
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.25)
        except TimeoutError:
            pass


async def wait_ready(client, process):
    for _ in range(150):
        if process.poll() is not None:
            raise RuntimeError("Backend exited; see backend.log")
        try:
            response = await client.get("/readyz")
            if response.status_code == 200:
                worker_pid = (await client.get("/__bench/progress")).json()["pid"]
                ancestry = {p.pid for p in psutil.Process(worker_pid).parents()}
                if worker_pid != process.pid and process.pid not in ancestry:
                    raise RuntimeError(
                        "Port belongs to a different backend; refusing to measure it"
                    )
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.2)
    raise RuntimeError("Backend never became ready; see backend.log")


async def scenario(args, rate, browser, output):
    output.mkdir(parents=True, exist_ok=False)
    # Allocate a fresh port by default; never talk to a pre-existing application.
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", args.port))
        port = reservation.getsockname()[1]
    feed = Feed(args.depth)
    server = await websockets.serve(
        feed.connect, "127.0.0.1", 0, process_request=feed.http, max_size=10_000_000
    )
    feed_port = server.sockets[0].getsockname()[1]
    command = [
        sys.executable,
        "tools/perf_backend.py",
        "--port",
        str(port),
        "--feed-port",
        str(feed_port),
        "--output",
        str(output),
    ]
    if args.profile:
        command.append("--profile")
    log = (output / "backend.log").open("w")
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    context = await browser.new_context(viewport={"width": 1440, "height": 1000})
    await context.add_init_script(f"({BROWSER_INIT})()")
    page = await context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    stop = asyncio.Event()
    sampler = None
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
            await wait_ready(client, process)
            await page.goto(f"http://127.0.0.1:{port}")
            await page.locator("header").get_by_text("Live", exact=True).wait_for()
            roster = (await client.get("/api/pairs")).json()
            spread_rows = (
                page.locator("section")
                .filter(has=page.get_by_role("heading", name="Live spreads", exact=True))
                .locator("tbody tr")
            )
            await expect(spread_rows).to_have_count(len({record["pair"] for record in roster}))
            await feed.run(110, 5, record=False)
            await asyncio.sleep(1.5)
            cdp = await context.new_cdp_session(page)
            await cdp.send("Performance.enable")
            cdp_start = {
                m["name"]: m["value"] for m in (await cdp.send("Performance.getMetrics"))["metrics"]
            }
            await page.evaluate("window.__arbBrowser.start()")
            resources = []
            # On Windows the venv executable can be a launcher for another PID.
            worker_pid = (await client.get("/__bench/progress")).json()["pid"]
            sampler = asyncio.create_task(resource_sampler(worker_pid, resources, stop))
            (await client.post("/__bench/start")).raise_for_status()
            started = time.perf_counter()
            await feed.run(rate, args.seconds, record=True, burst_ms=args.burst_ms)
            # Drain to account for queued work rather than silently discarding the tail.
            for _ in range(100):
                progress = (await client.get("/__bench/progress")).json()
                if progress["processed"] >= len(feed.sent) and progress["persistence_queue"] == 0:
                    break
                await asyncio.sleep(0.1)
            await asyncio.sleep(1.5)  # writer's idle flush and browser delivery
            elapsed = time.perf_counter() - started
            (await client.post("/__bench/stop")).raise_for_status()
            browser_data = await page.evaluate("window.__arbBrowser.stop()")
            cdp_end = {
                m["name"]: m["value"] for m in (await cdp.send("Performance.getMetrics"))["metrics"]
            }
            stop.set()
            await sampler
            await page.screenshot(path=str(output / "dashboard.png"), full_page=True)
            (output / "browser.json").write_text(json.dumps(browser_data))
            (output / "resources.json").write_text(json.dumps(resources))
            (output / "sent.json").write_text(json.dumps(feed.sent))
            await context.close()
            await client.post("/__bench/shutdown")
        # The backend writes backend.json before exiting, so a slow exit is not
        # lost measurement. Forcing it is still recorded rather than swallowed:
        # shutdown ordering is production behavior a run can regress.
        shutdown_started = time.perf_counter()
        shutdown_forced = False
        try:
            await asyncio.to_thread(process.wait, 60)
        except subprocess.TimeoutExpired:
            if not (output / "backend.json").exists():
                raise
            shutdown_forced = True
            process.kill()
            await asyncio.to_thread(process.wait, 15)
        shutdown_seconds = time.perf_counter() - shutdown_started
        backend = json.loads((output / "backend.json").read_text())
        result = summarize(
            backend,
            feed,
            browser_data,
            resources[1:],
            cdp_start,
            cdp_end,
            elapsed,
            output / "opportunities.sqlite3",
        )
        result["browser"]["errors"] = errors
        result["browser"]["react_profile_available"] = bool(result["browser"]["react_render_ms"])
        if args.profile:
            result["self_time_attribution"] = json.loads((output / "stages.json").read_text())
        result["rate_per_second"] = rate
        result["profile_enabled"] = args.profile
        result["shutdown_seconds"] = shutdown_seconds
        result["shutdown_forced"] = shutdown_forced
        # Absent when the backend never reached the end of its shutdown, which is
        # itself the finding; the phase that hung is the one with no duration.
        diagnostics = output / "shutdown.json"
        result["shutdown_diagnostics"] = (
            json.loads(diagnostics.read_text()) if diagnostics.exists() else None
        )
        result["valid"] = (
            not errors
            and not result["failures"]
            and result["missing_events"] == 0
            and result["unmatched_events"] == 0
            and all(a["gap_count"] == 0 and a["reconnect_count"] == 0 for a in result["adapters"])
        )
        (output / "summary.json").write_text(json.dumps(result, indent=2))
        return result
    finally:
        stop.set()
        if sampler is not None:
            await asyncio.gather(sampler, return_exceptions=True)
        await context.close()
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, 10)
        log.close()
        server.close()
        await server.wait_closed()


async def main(args):
    os.chdir(ROOT)
    source_before = source_fingerprint()
    mode = "profile" if args.profile else "perf"
    env = {**os.environ, "VITE_API_URL": ""}
    build = await asyncio.create_subprocess_exec(
        shutil.which("node"),
        "node_modules/vite/bin/vite.js",
        "build",
        "--mode",
        mode,
        "--outDir",
        "dist-perf",
        cwd=ROOT / "dashboard",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    build_output, _ = await build.communicate()
    if build.returncode:
        raise RuntimeError(build_output.decode())
    root = ROOT / "artifacts" / "benchmarks" / "performance"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"{args.label}-{mode}-{stamp}"
    output = root / "raw" / name
    results = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            channel=args.channel,
            headless=True,
            args=["--disable-background-timer-throttling", "--disable-renderer-backgrounding"],
        )
        try:
            for rate in args.rates:
                for repetition in range(args.repeats):
                    print(f"Measuring {name}: {rate}/s, repetition {repetition + 1}", flush=True)
                    result = await scenario(
                        args, rate, browser, output / f"{rate}-{repetition + 1}"
                    )
                    results.append(result)
                    print(
                        json.dumps(
                            {
                                "rate": rate,
                                "valid": result["valid"],
                                "cpu_mean": result["backend_cpu_percent_one_core"]["mean"],
                                "receive_p99_ms": result["latency_ms"]["receive_to_detection"][
                                    "p99"
                                ],
                                "send_p99_ms": result["latency_ms"]["send_to_detection"]["p99"],
                                "browser_long_tasks": result["browser"]["long_task_ms"]["count"],
                                "drops": result["queue_drops"],
                            }
                        ),
                        flush=True,
                    )
        finally:
            version = browser.version
            await browser.close()
    report = {
        "label": args.label,
        "mode": mode,
        "created_utc": stamp,
        "command": sys.argv,
        "source_sha256_before": source_before,
        "source_sha256_after": source_fingerprint(),
        "clock": "perf_counter_ns for sender and worker receipt/freshness/detection; benchmark-only substitution",
        "machine": {
            "platform": platform.platform(),
            "python": sys.version,
            "cpu": platform.processor(),
            "logical_cpus": os.cpu_count(),
            "memory_gib": psutil.virtual_memory().total / 1024**3,
            "browser": version,
        },
        "workload": {
            "initial_levels_per_side": args.depth,
            "books": 27,
            "seconds": args.seconds,
            "burst_ms": args.burst_ms,
            "pattern": "4 seconds at 0.25x followed by 1 second at 4x",
            "warmup_seconds": 5,
            "repeats": args.repeats,
        },
        "raw_directory": str(output.relative_to(ROOT)),
        "results": results,
    }
    target = root / f"{name}.json"
    target.write_text(json.dumps(report, indent=2))
    print(f"Report: {target}", flush=True)
    if report["source_sha256_after"] != source_before:
        raise SystemExit("Source changed during measurement; do not use this run for comparisons")
    if not all(r["valid"] for r in results):
        raise SystemExit("One or more runs failed completeness or correctness checks")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--rates", type=int, nargs="+", default=[110, 1100, 5500])
    parser.add_argument("--seconds", type=int, default=20)
    parser.add_argument("--depth", type=int, default=500)
    parser.add_argument("--burst-ms", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--port", type=int, default=0, help="0 selects a fresh localhost port")
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()
    if args.seconds <= 0 or args.seconds % 5 or args.depth < 2 or args.repeats < 1:
        parser.error("seconds must be a positive multiple of 5; depth >= 2; repeats >= 1")
    if any(rate <= 0 for rate in args.rates) or args.burst_ms <= 0 or 1000 % args.burst_ms:
        parser.error("rates must be positive; burst-ms must divide 1000")
    if not args.label.replace("-", "").replace("_", "").isalnum():
        parser.error("label must contain only letters, digits, hyphens and underscores")
    asyncio.run(main(args))
