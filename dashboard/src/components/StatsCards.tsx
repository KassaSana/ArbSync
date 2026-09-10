import { Stats } from "../api/client";
import { Async } from "../lib/async";
import { count, quoteAmount, spreadPct } from "../lib/format";
import { Panel } from "./Panel";
import { Placeholder } from "./Placeholder";
import { Stat } from "./Stat";

type Props = {
  stats: Async<Stats>;
  onRetry: () => void;
};

export function StatsCards({ stats, onRetry }: Props) {
  if (stats.state === "failed") {
    return (
      <Placeholder
        state="failed"
        title="Could not load hourly stats"
        detail={stats.error}
        onRetry={onRetry}
      />
    );
  }

  const data = stats.state === "ready" ? stats.data : null;
  const items = [
    { label: "Opportunities (1h)", value: data === null ? null : count(data.count) },
    { label: "Max spread (1h)", value: data === null ? null : spreadPct(data.max_spread_pct) },
    ...(data === null
      ? [{ label: "Theoretical profit (1h)", value: null }]
      : Object.entries(data.theoretical_profit_by_quote).map(([quoteAsset, profit]) => ({
          label: `Theoretical profit (${quoteAsset}, 1h)`,
          value: quoteAmount(profit, quoteAsset),
        }))),
  ];

  return (
    <Panel className="overflow-hidden">
      <div className="grid gap-px bg-line-soft sm:grid-cols-3">
        {items.map((item) => (
          <Stat
            key={item.label}
            label={item.label}
            value={
              item.value === null ? <span className="text-base text-ink-3">Loading</span> : item.value
            }
          />
        ))}
      </div>
    </Panel>
  );
}
