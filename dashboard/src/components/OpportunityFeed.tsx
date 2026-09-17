import { Opportunity } from "../api/client";
import { Async } from "../lib/async";
import { age, eventTime, nsToMs, quoteAmount, spreadPct } from "../lib/format";
import { Panel } from "./Panel";
import { Placeholder } from "./Placeholder";

type Props = {
  opportunities: Async<Opportunity[]>;
  onRetry: () => void;
};

function spreadTone(value: string): string {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return "text-ink-3";
  }
  if (parsed >= 0.25) {
    return "text-signal-hi";
  }
  if (parsed >= 0.1) {
    return "text-signal";
  }
  return "text-ink-2";
}

function lifetime(row: Opportunity): string {
  if (row.duration_ns === null) {
    return "open";
  }
  return age(nsToMs(row.duration_ns));
}

const CLOSE_LABEL: Record<NonNullable<Opportunity["close_reason"]>, string> = {
  spread_closed: "spread closed",
  book_ineligible: "book dropped",
  shutdown: "shutdown",
};

export function OpportunityFeed({ opportunities, onRetry }: Props) {
  if (opportunities.state === "failed") {
    return (
      <Placeholder
        state="failed"
        title="Could not load the opportunity feed"
        detail={opportunities.error}
        onRetry={onRetry}
      />
    );
  }

  const rows = opportunities.state === "ready" ? opportunities.data : [];

  return (
    <Panel
      title="Recent opportunities"
      meta={opportunities.state === "loading" ? "Loading" : `${rows.length} episodes`}
      className="overflow-hidden"
    >
      <div className="max-h-[28rem] overflow-y-auto">
        <table className="min-w-full border-collapse text-xs">
          <caption className="sr-only">
            Most recent theoretical arbitrage episodes, newest first, with the venue to
            buy on, the venue to sell on, the widest spread seen, the theoretical profit at
            that moment, the first configured notional's net executable spread, and how long
            the spread lasted.
          </caption>
          <thead className="sticky top-0 bg-panel">
            <tr className="border-b border-line-soft text-left text-micro text-ink-3">
              <th scope="col" className="px-4 py-2 font-normal">
                Time
              </th>
              <th scope="col" className="px-4 py-2 font-normal">
                Pair
              </th>
              <th scope="col" className="px-4 py-2 font-normal">
                Route
              </th>
              <th scope="col" className="px-4 py-2 text-right font-normal">
                Peak spread
              </th>
              <th scope="col" className="px-4 py-2 text-right font-normal">
                Peak profit
              </th>
              <th scope="col" className="px-4 py-2 text-right font-normal">
                Net executable
              </th>
              <th scope="col" className="px-4 py-2 text-right font-normal">
                Lifetime
              </th>
            </tr>
          </thead>
          <tbody>
            {opportunities.state === "loading" ? (
              <tr>
                <td colSpan={7} className="px-4 py-6 text-ink-3">
                  Loading recent opportunities.
                </td>
              </tr>
            ) : null}
            {opportunities.state === "ready" && rows.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-4 py-6 text-ink-3">
                  No opportunities detected yet. Spreads this tight are the normal state
                  for liquid pairs.
                </td>
              </tr>
            ) : null}
            {rows.map((row) => (
              <tr
                key={`${row.start_ns}-${row.buy_exchange}-${row.sell_exchange}-${row.pair}`}
                className="border-b border-line-soft/60 last:border-0"
              >
                <td className="num px-4 py-1.5 text-ink-3">{eventTime(row.start_ns)}</td>
                <th scope="row" className="px-4 py-1.5 text-left font-medium text-ink">
                  {row.pair}
                </th>
                <td className="px-4 py-1.5 text-ink-2">
                  {row.buy_exchange} <span className="text-ink-3">&rarr;</span>{" "}
                  {row.sell_exchange}
                </td>
                <td className={`num px-4 py-1.5 text-right ${spreadTone(row.peak_spread_pct)}`}>
                  {spreadPct(row.peak_spread_pct)}
                </td>
                <td className="num px-4 py-1.5 text-right text-ink-2">
                  {quoteAmount(row.peak_profit, row.quote_asset)}
                </td>
                <td
                  className={`num px-4 py-1.5 text-right ${
                    row.pricing_ledgers[0]?.net_executable_spread_pct === null ||
                    row.pricing_ledgers[0] === undefined
                      ? "text-ink-3"
                      : spreadTone(row.pricing_ledgers[0].net_executable_spread_pct)
                  }`}
                  title={
                    row.pricing_ledgers[0] === undefined
                      ? "No stored executable-price ledger"
                      : `${row.pricing_ledgers[0].notional} ${row.quote_asset}; includes measured depth impact and configured taker fees`
                  }
                >
                  {row.pricing_ledgers[0] === undefined
                    ? "not recorded"
                    : row.pricing_ledgers[0].net_executable_spread_pct === null
                      ? "insufficient depth"
                    : spreadPct(row.pricing_ledgers[0].net_executable_spread_pct)}
                </td>
                <td
                  className={`num px-4 py-1.5 text-right ${
                    row.close_reason === null ? "text-signal" : "text-ink-3"
                  }`}
                  title={row.close_reason === null ? "Still open" : CLOSE_LABEL[row.close_reason]}
                >
                  {lifetime(row)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}
