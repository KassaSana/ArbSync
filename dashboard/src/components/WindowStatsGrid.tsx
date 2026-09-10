import { WindowStats } from "../api/client";
import { Async } from "../lib/async";
import { count, DASH, dateTime, quoteAmount, spreadPct } from "../lib/format";
import { Panel } from "./Panel";
import { Placeholder } from "./Placeholder";
import { Stat } from "./Stat";

type Props = {
  stats: Async<WindowStats>;
  windowLabel: string;
  onRetry: () => void;
};

export function WindowStatsGrid({ stats, windowLabel, onRetry }: Props) {
  if (stats.state === "failed") {
    return (
      <Placeholder
        state="failed"
        title={`Could not load stats for the last ${windowLabel}`}
        detail={stats.error}
        onRetry={onRetry}
      />
    );
  }

  if (stats.state === "loading") {
    return (
      <Panel className="overflow-hidden">
        <p className="px-4 py-6 text-xs text-ink-3">Loading window stats.</p>
      </Panel>
    );
  }

  const data = stats.data;

  if (data.count === 0) {
    return (
      <Placeholder
        state="empty"
        title={`No opportunities in the last ${windowLabel}`}
        detail="Stats will populate as the detector keeps running."
      />
    );
  }

  const peak = data.peak_minute;

  return (
    <Panel className="overflow-hidden">
      <div className="grid gap-px bg-line-soft sm:grid-cols-2 lg:grid-cols-3">
        <Stat label="Opportunities" value={count(data.count)} />
        <Stat label="Top pair" value={data.top_pair ?? DASH} />
        <Stat label="Max spread" value={spreadPct(data.max_spread_pct)} />
        <Stat label="Mean spread" value={spreadPct(data.mean_spread_pct)} />
        {Object.entries(data.theoretical_profit_by_quote).map(([quoteAsset, profit]) => (
          <Stat
            key={quoteAsset}
            label={`Theoretical profit (${quoteAsset})`}
            value={quoteAmount(profit, quoteAsset)}
          />
        ))}
        <Stat
          label="Peak minute"
          value={peak === null ? DASH : count(peak.count)}
          sub={peak === null ? "Not enough history yet" : dateTime(peak.minute_start_ns)}
        />
      </div>
    </Panel>
  );
}
