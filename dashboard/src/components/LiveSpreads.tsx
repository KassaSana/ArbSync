import { PairRecord, TopOfBook } from "../api/client";
import { age, DASH, price, spreadPct } from "../lib/format";
import { contributingAgeMs, TrackedBookStatus } from "../state/live";
import { Panel } from "./Panel";

type Props = {
  pairs: PairRecord[];
  books: Record<string, TopOfBook>;
  statuses: Record<string, TrackedBookStatus>;
  nowMs: number;
  feedLive: boolean;
};

const STALE_AFTER_MS = 5_000;

type Venue = {
  exchange: string;
  eligible: boolean;
};

type Row = {
  pair: string;
  entries: TopOfBook[];
  /** Canonical ages of the books contributing to this row, one per entry. */
  ages: number[];
  venues: Venue[];
  bestBid: number | null;
  bestAsk: number | null;
  spread: number | null;
  oldestAgeMs: number | null;
};

const VENUE_CODE: Record<string, string> = {
  binance: "BIN",
  coinbase: "CBS",
  gemini: "GEM",
};

function venueCode(exchange: string): string {
  return VENUE_CODE[exchange] ?? exchange.slice(0, 3).toUpperCase();
}

function emptyRow(pair: string): Row {
  return {
    pair,
    entries: [],
    ages: [],
    venues: [],
    bestBid: null,
    bestAsk: null,
    spread: null,
    oldestAgeMs: null,
  };
}

function buildRows(
  pairs: PairRecord[],
  books: Record<string, TopOfBook>,
  statuses: Record<string, TrackedBookStatus>,
  nowMs: number,
): Row[] {
  const byPair = new Map<string, Row>();

  for (const record of pairs) {
    const row = byPair.get(record.pair) ?? emptyRow(record.pair);

    const key = `${record.exchange}:${record.pair}`;
    const status = statuses[key];
    const eligible = status?.eligible === true;
    row.venues.push({ exchange: record.exchange, eligible });
    const book = books[key];
    // Only eligible books may contribute. The backend already made this call in
    // OrderBookManager; recomputing a spread from every book we hold would give a
    // second, weaker answer. Books are never evicted from the live store, so a
    // disconnected venue keeps its last quote here long after it stopped counting.
    if (book !== undefined && eligible) {
      row.entries.push(book);
      // Age comes from the canonical status, not the exchange timestamp on the
      // quote: the backend judges freshness by monotonic receipt time precisely
      // so venue clock skew cannot make a stale book look current.
      row.ages.push(contributingAgeMs(status, nowMs));
    }
    byPair.set(record.pair, row);
  }

  for (const row of byPair.values()) {
    if (row.ages.length > 0) {
      row.oldestAgeMs = Math.max(...row.ages);
    }
    if (row.entries.length >= 2) {
      const bid = Math.max(...row.entries.map((entry) => Number(entry.best_bid_price)));
      const ask = Math.min(...row.entries.map((entry) => Number(entry.best_ask_price)));
      row.bestBid = bid;
      row.bestAsk = ask;
      row.spread = bid > ask ? ((bid - ask) / ask) * 100 : null;
    }
  }

  return [...byPair.values()].sort(
    (left, right) =>
      (right.spread ?? -1) - (left.spread ?? -1) || left.pair.localeCompare(right.pair),
  );
}

/** Magnitude is sequential: brighter green means a wider spread, nothing else. */
function spreadTone(spread: number | null): string {
  if (spread === null) {
    return "text-ink-3";
  }
  if (spread >= 0.25) {
    return "text-signal-hi";
  }
  if (spread >= 0.1) {
    return "text-signal";
  }
  return "text-ink-2";
}

export function LiveSpreads({ pairs, books, statuses, nowMs, feedLive }: Props) {
  const rows = buildRows(pairs, books, statuses, nowMs);

  return (
    <Panel
      title="Live spreads"
      meta={`${rows.length} pairs · cross-venue top of book`}
      className="overflow-hidden"
    >
      <div className="overflow-x-auto">
        <table className="min-w-full border-collapse text-xs">
          <caption className="sr-only">
            Best bid and ask across venues for each tracked pair, with the resulting
            cross-exchange spread and the age of the oldest contributing book.
          </caption>
          <thead>
            <tr className="border-b border-line-soft text-left text-micro text-ink-3">
              <th scope="col" className="px-4 py-2 font-normal">
                Pair
              </th>
              <th scope="col" className="px-4 py-2 font-normal">
                Venues
              </th>
              <th scope="col" className="px-4 py-2 text-right font-normal">
                Best bid
              </th>
              <th scope="col" className="px-4 py-2 text-right font-normal">
                Best ask
              </th>
              <th scope="col" className="px-4 py-2 text-right font-normal">
                Spread
              </th>
              <th scope="col" className="px-4 py-2 text-right font-normal">
                Age
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={6} className="px-4 py-6 text-ink-3">
                  Waiting for the first book snapshot.
                </td>
              </tr>
            ) : null}
            {rows.map((row) => {
              const stale =
                !feedLive || (row.oldestAgeMs !== null && row.oldestAgeMs > STALE_AFTER_MS);
              return (
                <tr
                  key={row.pair}
                  className={`border-b border-line-soft/60 last:border-0 ${
                    stale ? "opacity-45" : ""
                  }`}
                >
                  <th scope="row" className="px-4 py-1.5 text-left font-medium text-ink">
                    {row.pair}
                  </th>
                  <td className="px-4 py-1.5">
                    <span className="num flex gap-2">
                      {row.venues.map((venue) => (
                        <span
                          key={venue.exchange}
                          className={venue.eligible ? "text-ink-3" : "text-warn"}
                          title={`${venue.exchange}: ${
                            venue.eligible ? "eligible" : "not contributing"
                          }`}
                        >
                          {venueCode(venue.exchange)}
                        </span>
                      ))}
                    </span>
                  </td>
                  <td className="num px-4 py-1.5 text-right text-ink-2">
                    {row.bestBid === null ? DASH : price(String(row.bestBid))}
                  </td>
                  <td className="num px-4 py-1.5 text-right text-ink-2">
                    {row.bestAsk === null ? DASH : price(String(row.bestAsk))}
                  </td>
                  <td className={`num px-4 py-1.5 text-right ${spreadTone(row.spread)}`}>
                    {spreadPct(row.spread)}
                  </td>
                  <td className="num px-4 py-1.5 text-right text-ink-3">{age(row.oldestAgeMs)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}
