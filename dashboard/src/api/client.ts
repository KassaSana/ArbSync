import {
  decodeAdapterStatuses,
  decodeBookStatuses,
  decodeDepthPricing,
  decodeOpportunities,
  decodeOpportunityHistoryPage,
  decodePairs,
  decodeStats,
  decodeSystemOverview,
  decodeTimeseries,
  decodeWindowStats,
  type AdapterStatus,
  type BookStatus,
  type DepthPricing,
  type EpisodeCloseReason,
  type Opportunity,
  type OpportunityHistoryPage,
  type PairRecord,
  type Stats,
  type SystemOverview,
  type Timeseries,
  type WindowKey,
  type WindowStats,
} from "./schema";

export type {
  AdapterStatus,
  BookStatus,
  EpisodeCloseReason,
  Opportunity,
  OpportunityHistoryPage,
  PairRecord,
  PeakMinute,
  Stats,
  SystemOverview,
  Timeseries,
  TimeseriesPoint,
  TopOfBook,
  WindowKey,
  WindowStats,
} from "./schema";

export type { DepthPricing, DepthQuote, ExecutableRoute } from "./schema";

const HOSTED_API_URL = "https://arb-detector-api.onrender.com";
const API_BASE = (
  import.meta.env.VITE_API_URL ?? (import.meta.env.DEV ? "" : HOSTED_API_URL)
).replace(/\/+$/, "");
const WS_BASE = (API_BASE || window.location.origin).replace(/^http/, "ws");

export { WS_BASE };

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly path: string,
    message: string,
    public readonly cause?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function requestJson<T>(
  path: string,
  decode: (value: unknown, location?: string) => T,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    const suffix = detail ? `: ${detail}` : "";
    throw new ApiError(response.status, path, `Request failed (${response.status})${suffix}`);
  }
  let payload: unknown;
  try {
    payload = await response.json();
  } catch (cause) {
    throw new ApiError(response.status, path, `Invalid JSON response from ${path}`, cause);
  }
  try {
    return decode(payload, "response");
  } catch (cause) {
    throw new ApiError(response.status, path, `Invalid response payload from ${path}`, cause);
  }
}

export async function fetchRecentOpportunities(): Promise<Opportunity[]> {
  return requestJson("/api/opportunities/recent?limit=50", decodeOpportunities);
}

export async function fetchStats(): Promise<Stats> {
  return requestJson("/api/stats?window=1h", decodeStats);
}

export async function fetchPairs(): Promise<PairRecord[]> {
  return requestJson("/api/pairs", decodePairs);
}

export async function fetchAdapterStatus(): Promise<AdapterStatus[]> {
  return requestJson("/api/adapters", decodeAdapterStatuses);
}

export async function fetchBookStatus(): Promise<BookStatus[]> {
  return requestJson("/api/book-status", decodeBookStatuses);
}

export async function fetchDepthPricing(): Promise<DepthPricing> {
  return requestJson("/api/pricing/depth", decodeDepthPricing);
}

export async function fetchSystemOverview(): Promise<SystemOverview> {
  return requestJson("/api/system/overview", decodeSystemOverview);
}

export async function fetchSystemStats(window: WindowKey): Promise<WindowStats> {
  return requestJson(`/api/system/stats?window=${window}`, decodeWindowStats);
}

export async function fetchSystemTimeseries(
  window: WindowKey,
  bucketSeconds = 60,
): Promise<Timeseries> {
  return requestJson(
    `/api/system/timeseries?window=${window}&bucket_seconds=${bucketSeconds}`,
    decodeTimeseries,
  );
}

/**
 * Filters for `/api/opportunities` and its export. Times are Unix-nanosecond
 * decimal strings on the episode start: `from_ns` inclusive, `to_ns` exclusive.
 * `closed` includes orphaned episodes, whose lifetime is unknown.
 */
export type HistoryFilters = {
  pair?: string;
  buy_exchange?: string;
  sell_exchange?: string;
  close_reason?: EpisodeCloseReason;
  state?: "open" | "closed";
  from_ns?: string;
  to_ns?: string;
};

function historyQuery(filters: HistoryFilters, extra: Record<string, string>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries({ ...filters, ...extra })) {
    if (value !== undefined && value !== "") {
      params.set(key, value);
    }
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

export async function fetchOpportunityHistory(
  filters: HistoryFilters,
  cursor: string | null,
  limit = 100,
): Promise<OpportunityHistoryPage> {
  const extra: Record<string, string> = { limit: String(limit) };
  if (cursor !== null) {
    extra.cursor = cursor;
  }
  return requestJson(
    `/api/opportunities${historyQuery(filters, extra)}`,
    decodeOpportunityHistoryPage,
  );
}

/** JSON Lines download; the last line is an `end` record that reports truncation. */
export function opportunityExportUrl(filters: HistoryFilters, maxRows = 10_000): string {
  return `${API_BASE}/api/opportunities/export${historyQuery(filters, {
    max_rows: String(maxRows),
  })}`;
}
