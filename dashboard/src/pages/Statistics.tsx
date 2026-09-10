import { useCallback, useEffect, useState } from "react";
import {
  fetchSystemOverview,
  fetchSystemStats,
  fetchSystemTimeseries,
  SystemOverview,
  Timeseries,
  WindowKey,
  WindowStats,
} from "../api/client";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { OpportunitiesChart } from "../components/OpportunitiesChart";
import { PeakCard } from "../components/PeakCard";
import { Placeholder } from "../components/Placeholder";
import { UptimeCard } from "../components/UptimeCard";
import { WindowStatsGrid } from "../components/WindowStatsGrid";
import { Async, failed, loading, ready } from "../lib/async";

const WINDOWS: { key: WindowKey; label: string; bucketSeconds: number }[] = [
  { key: "1h", label: "1 hour", bucketSeconds: 60 },
  { key: "4h", label: "4 hours", bucketSeconds: 240 },
  { key: "1d", label: "1 day", bucketSeconds: 900 },
  { key: "1w", label: "1 week", bucketSeconds: 3600 },
];

const POLL_MS = 5_000;

export default function Statistics() {
  const [overview, setOverview] = useState<Async<SystemOverview>>(loading);
  const [stats, setStats] = useState<Async<WindowStats>>(loading);
  const [timeseries, setTimeseries] = useState<Async<Timeseries>>(loading);
  const [windowKey, setWindowKey] = useState<WindowKey>("1h");

  const active = WINDOWS.find((entry) => entry.key === windowKey) ?? WINDOWS[0];

  const refresh = useCallback(async (key: WindowKey, bucketSeconds: number) => {
    const [nextOverview, nextStats, nextSeries] = await Promise.all([
      fetchSystemOverview().then(ready).catch(failed<SystemOverview>),
      fetchSystemStats(key).then(ready).catch(failed<WindowStats>),
      fetchSystemTimeseries(key, bucketSeconds).then(ready).catch(failed<Timeseries>),
    ]);
    setOverview(nextOverview);
    setStats(nextStats);
    setTimeseries(nextSeries);
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- polling pattern: refresh awaits the network before it ever sets state
    void refresh(active.key, active.bucketSeconds);
    const id = window.setInterval(
      () => void refresh(active.key, active.bucketSeconds),
      POLL_MS,
    );
    return () => window.clearInterval(id);
  }, [active.key, active.bucketSeconds, refresh]);

  return (
    <div className="space-y-3">
      {overview.state === "failed" ? (
        <Placeholder
          state="failed"
          title="Could not load the system overview"
          detail={overview.error}
          onRetry={() => void refresh(active.key, active.bucketSeconds)}
        />
      ) : (
        <>
          <UptimeCard
            startedAtNs={overview.state === "ready" ? overview.data.started_at_ns : null}
          />
          <PeakCard
            allTimeCount={overview.state === "ready" ? overview.data.all_time_count : 0}
            allTimeMaxSpread={
              overview.state === "ready" ? overview.data.all_time_max_spread_pct : "0"
            }
            allTimePeakMinute={
              overview.state === "ready" ? overview.data.all_time_peak_minute : null
            }
          />
        </>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3 rounded border border-line bg-panel px-4 py-2.5">
        <p className="text-sm font-medium text-ink">Stats over time</p>
        <div
          role="group"
          aria-label="Time window"
          className="flex items-center gap-1 rounded border border-line p-0.5"
        >
          {WINDOWS.map((entry) => {
            const selected = entry.key === windowKey;
            return (
              <button
                key={entry.key}
                type="button"
                aria-pressed={selected}
                onClick={() => setWindowKey(entry.key)}
                className={`rounded px-3 py-1 text-xs transition-colors ${
                  selected ? "bg-raised text-ink" : "text-ink-3 hover:text-ink-2"
                }`}
              >
                {entry.label}
              </button>
            );
          })}
        </div>
      </div>

      <ErrorBoundary label="Window stats">
        <WindowStatsGrid
          stats={stats}
          windowLabel={active.label}
          onRetry={() => void refresh(active.key, active.bucketSeconds)}
        />
      </ErrorBoundary>

      <ErrorBoundary label="Opportunities chart">
        <OpportunitiesChart
          data={timeseries}
          window={active.key}
          windowLabel={active.label}
          onRetry={() => void refresh(active.key, active.bucketSeconds)}
        />
      </ErrorBoundary>
    </div>
  );
}
