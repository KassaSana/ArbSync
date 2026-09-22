import { FormEvent, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  EpisodeCloseReason,
  fetchOpportunityHistory,
  HistoryFilters,
  Opportunity,
  opportunityExportUrl,
} from "../api/client";
import {
  OpportunityRow,
  OpportunityTableHead,
  opportunityKey,
} from "../components/OpportunityFeed";
import { Panel } from "../components/Panel";
import { Placeholder } from "../components/Placeholder";
import { useLive } from "../state/live";

const PAGE_SIZE = 100;
const EXPORT_MAX_ROWS = 10_000;

const CLOSE_REASONS: { value: EpisodeCloseReason; label: string }[] = [
  { value: "spread_closed", label: "spread closed" },
  { value: "book_ineligible", label: "book dropped" },
  { value: "shutdown", label: "shutdown" },
  { value: "orphaned", label: "orphaned" },
];

type Draft = {
  pair: string;
  buy_exchange: string;
  sell_exchange: string;
  state: "" | "open" | "closed";
  close_reason: "" | EpisodeCloseReason;
  from: string;
  to: string;
};

const EMPTY_DRAFT: Draft = {
  pair: "",
  buy_exchange: "",
  sell_exchange: "",
  state: "",
  close_reason: "",
  from: "",
  to: "",
};

type Results = {
  status: "loading" | "ready" | "failed";
  items: Opportunity[];
  nextCursor: string | null;
  loadingMore: boolean;
  error: string | null;
};

/** A `datetime-local` value in the viewer's zone as Unix nanoseconds, or null if unusable. */
export function localInputToNs(value: string): string | null {
  const ms = new Date(value).getTime();
  if (!Number.isFinite(ms) || ms < 0) {
    return null;
  }
  return (BigInt(ms) * 1_000_000n).toString();
}

function toFilters(draft: Draft): HistoryFilters | string {
  const filters: HistoryFilters = {};
  if (draft.pair) filters.pair = draft.pair;
  if (draft.buy_exchange) filters.buy_exchange = draft.buy_exchange;
  if (draft.sell_exchange) filters.sell_exchange = draft.sell_exchange;
  if (draft.state) filters.state = draft.state;
  if (draft.close_reason) filters.close_reason = draft.close_reason;
  for (const [key, input] of [
    ["from_ns", draft.from],
    ["to_ns", draft.to],
  ] as const) {
    if (!input) continue;
    const ns = localInputToNs(input);
    if (ns === null) {
      return "Enter a valid date and time.";
    }
    filters[key] = ns;
  }
  if (
    filters.from_ns !== undefined &&
    filters.to_ns !== undefined &&
    BigInt(filters.from_ns) >= BigInt(filters.to_ns)
  ) {
    return "The start of the range must be before its end.";
  }
  return filters;
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : "Request failed";
}

export default function History() {
  const { pairs } = useLive();
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [applied, setApplied] = useState<HistoryFilters>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [results, setResults] = useState<Results>({
    status: "loading",
    items: [],
    nextCursor: null,
    loadingMore: false,
    error: null,
  });
  // Each filter application is a new traversal. Responses from an older one,
  // including a slow "load more", must not append to or replace the current rows.
  const generation = useRef(0);

  const options = useMemo(() => {
    const records = pairs.state === "ready" ? pairs.data : [];
    return {
      pairs: [...new Set(records.map((record) => record.pair))].sort(),
      exchanges: [...new Set(records.map((record) => record.exchange))].sort(),
    };
  }, [pairs]);

  const fetchFirstPage = useCallback((filters: HistoryFilters, current: number) => {
    fetchOpportunityHistory(filters, null, PAGE_SIZE)
      .then((page) => {
        if (generation.current !== current) return;
        setResults({
          status: "ready",
          items: page.items,
          nextCursor: page.next_cursor,
          loadingMore: false,
          error: null,
        });
      })
      .catch((error: unknown) => {
        if (generation.current !== current) return;
        setResults({
          status: "failed",
          items: [],
          nextCursor: null,
          loadingMore: false,
          error: message(error),
        });
      });
  }, []);

  useEffect(() => {
    // Results start in the loading state, so the first traversal only fetches.
    fetchFirstPage({}, ++generation.current);
    return () => {
      generation.current += 1;
    };
  }, [fetchFirstPage]);

  const load = (filters: HistoryFilters) => {
    const current = ++generation.current;
    setResults({ status: "loading", items: [], nextCursor: null, loadingMore: false, error: null });
    fetchFirstPage(filters, current);
  };

  const loadMore = () => {
    if (results.nextCursor === null || results.loadingMore) return;
    const current = generation.current;
    setResults((previous) => ({ ...previous, loadingMore: true, error: null }));
    fetchOpportunityHistory(applied, results.nextCursor, PAGE_SIZE)
      .then((page) => {
        if (generation.current !== current) return;
        setResults((previous) => ({
          ...previous,
          items: [...previous.items, ...page.items],
          nextCursor: page.next_cursor,
          loadingMore: false,
        }));
      })
      .catch((error: unknown) => {
        if (generation.current !== current) return;
        // Keep the rows already shown; the same cursor can be retried.
        setResults((previous) => ({ ...previous, loadingMore: false, error: message(error) }));
      });
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const filters = toFilters(draft);
    if (typeof filters === "string") {
      setFormError(filters);
      return;
    }
    setFormError(null);
    setApplied(filters);
    load(filters);
  };

  const reset = () => {
    setDraft(EMPTY_DRAFT);
    setFormError(null);
    setApplied({});
    load({});
  };

  const set =
    <K extends keyof Draft>(key: K) =>
    (event: { target: { value: string } }) =>
      setDraft((previous) => ({ ...previous, [key]: event.target.value as Draft[K] }));

  const fieldClass =
    "rounded border border-line bg-ground px-2 py-1 text-xs text-ink focus:border-ink-3 focus:outline-none";
  const labelClass = "flex flex-col gap-1 text-micro text-ink-3";
  const idPrefix = useId();

  return (
    <div className="flex flex-col gap-4">
      <Panel title="Opportunity history" meta="Stored episodes, newest first">
        <form
          aria-label="History filters"
          onSubmit={submit}
          className="flex flex-wrap items-end gap-3 px-4 py-3"
        >
          <label className={labelClass} htmlFor={`${idPrefix}-pair`}>
            Pair
            <select
              id={`${idPrefix}-pair`}
              className={fieldClass}
              value={draft.pair}
              onChange={set("pair")}
            >
              <option value="">Any</option>
              {options.pairs.map((pair) => (
                <option key={pair} value={pair}>
                  {pair}
                </option>
              ))}
            </select>
          </label>
          <label className={labelClass} htmlFor={`${idPrefix}-buy`}>
            Buy venue
            <select
              id={`${idPrefix}-buy`}
              className={fieldClass}
              value={draft.buy_exchange}
              onChange={set("buy_exchange")}
            >
              <option value="">Any</option>
              {options.exchanges.map((exchange) => (
                <option key={exchange} value={exchange}>
                  {exchange}
                </option>
              ))}
            </select>
          </label>
          <label className={labelClass} htmlFor={`${idPrefix}-sell`}>
            Sell venue
            <select
              id={`${idPrefix}-sell`}
              className={fieldClass}
              value={draft.sell_exchange}
              onChange={set("sell_exchange")}
            >
              <option value="">Any</option>
              {options.exchanges.map((exchange) => (
                <option key={exchange} value={exchange}>
                  {exchange}
                </option>
              ))}
            </select>
          </label>
          <label className={labelClass} htmlFor={`${idPrefix}-state`}>
            State
            <select
              id={`${idPrefix}-state`}
              className={fieldClass}
              value={draft.state}
              onChange={set("state")}
            >
              <option value="">Any</option>
              <option value="open">Open</option>
              <option value="closed">Closed (incl. orphaned)</option>
            </select>
          </label>
          <label className={labelClass} htmlFor={`${idPrefix}-reason`}>
            Close reason
            <select
              id={`${idPrefix}-reason`}
              className={fieldClass}
              value={draft.close_reason}
              onChange={set("close_reason")}
            >
              <option value="">Any</option>
              {CLOSE_REASONS.map((reason) => (
                <option key={reason.value} value={reason.value}>
                  {reason.label}
                </option>
              ))}
            </select>
          </label>
          <label className={labelClass} htmlFor={`${idPrefix}-from`}>
            Started from
            <input
              id={`${idPrefix}-from`}
              type="datetime-local"
              className={fieldClass}
              value={draft.from}
              onChange={set("from")}
            />
          </label>
          <label className={labelClass} htmlFor={`${idPrefix}-to`}>
            Started before
            <input
              id={`${idPrefix}-to`}
              type="datetime-local"
              className={fieldClass}
              value={draft.to}
              onChange={set("to")}
            />
          </label>
          <div className="flex gap-2">
            <button
              type="submit"
              className="rounded border border-line bg-raised px-3 py-1 text-xs text-ink hover:border-ink-3"
            >
              Apply
            </button>
            <button
              type="button"
              onClick={reset}
              className="rounded px-3 py-1 text-xs text-ink-3 hover:text-ink-2"
            >
              Reset
            </button>
          </div>
          {formError ? (
            <p role="alert" className="w-full text-xs text-signal-hi">
              {formError}
            </p>
          ) : null}
        </form>
      </Panel>

      {results.status === "failed" ? (
        <Placeholder
          state="failed"
          title="Could not load opportunity history"
          detail={results.error ?? undefined}
          onRetry={() => load(applied)}
        />
      ) : (
        <Panel
          title="Episodes"
          meta={
            results.status === "loading"
              ? "Loading"
              : `${results.items.length} shown${results.nextCursor === null ? "" : ", more available"}`
          }
          className="overflow-hidden"
        >
          <div className="flex items-center justify-between gap-3 border-b border-line-soft px-4 py-2 text-micro text-ink-3">
            <span>
              Peak spread and profit are top-of-book gross values; net executable is the first
              configured notional after depth and taker fees. Times are episode starts. The export is
              JSON Lines, up to 10,000 rows, ending in a record that reports truncation and a
              resume cursor.
            </span>
            <a
              href={opportunityExportUrl(applied, EXPORT_MAX_ROWS)}
              download
              className="shrink-0 rounded border border-line px-3 py-1 text-xs text-ink-2 hover:text-ink"
            >
              Export JSONL
            </a>
          </div>
          <table className="min-w-full border-collapse text-xs">
            <caption className="sr-only">
              Stored theoretical arbitrage episodes matching the filters, newest first, with the
              venue to buy on, the venue to sell on, the widest spread seen, the theoretical
              profit at that moment, the first configured notional's net executable spread, and
              how long the spread lasted.
            </caption>
            <OpportunityTableHead />
            <tbody>
              {results.status === "loading" ? (
                <tr>
                  <td colSpan={7} className="px-4 py-6 text-ink-3">
                    Loading opportunity history.
                  </td>
                </tr>
              ) : null}
              {results.status === "ready" && results.items.length === 0 ? (
                <tr>
                  <td colSpan={7} className="px-4 py-6 text-ink-3">
                    No stored episodes match these filters.
                  </td>
                </tr>
              ) : null}
              {results.items.map((row) => (
                <OpportunityRow key={opportunityKey(row)} row={row} />
              ))}
            </tbody>
          </table>
          {results.error ? (
            <p role="alert" className="px-4 py-2 text-xs text-signal-hi">
              Could not load more: {results.error}
            </p>
          ) : null}
          {results.nextCursor !== null ? (
            <div className="border-t border-line-soft px-4 py-2">
              <button
                type="button"
                onClick={loadMore}
                disabled={results.loadingMore}
                className="rounded border border-line px-3 py-1 text-xs text-ink-2 hover:text-ink disabled:opacity-50"
              >
                {results.loadingMore ? "Loading" : "Load more"}
              </button>
            </div>
          ) : null}
        </Panel>
      )}
    </div>
  );
}
