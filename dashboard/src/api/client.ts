export type Opportunity = {
  timestamp_ns: string;
  pair: string;
  buy_exchange: string;
  sell_exchange: string;
  buy_price: string;
  sell_price: string;
  spread_pct: string;
  max_size: string;
  theoretical_profit_usd: string;
};

export type PairRecord = {
  exchange: string;
  pair: string;
};

export type Stats = {
  count: number;
  max_spread_pct: string;
  total_theoretical_profit_usd: string;
};

export type AdapterStatus = {
  exchange: string;
  connected: boolean;
  last_message_age_ms: number | null;
  gap_count: number;
  reconnect_count: number;
  last_error: string | null;
};

export type BookStatus = {
  exchange: string;
  pair: string;
  initialized: boolean;
  continuous: boolean;
  connected: boolean;
  age_ms: number | null;
  max_age_ms: number;
  eligible: boolean;
  reason: string | null;
};

export type TopOfBook = {
  exchange: string;
  pair: string;
  best_bid_price: string;
  best_bid_size: string;
  best_ask_price: string;
  best_ask_size: string;
  sequence: number;
  timestamp_ns: string;
};

export type WindowKey = "1h" | "4h" | "1d" | "1w";

export type PeakMinute = {
  minute_start_ns: string;
  count: number;
};

export type SystemOverview = {
  started_at_ns: string;
  uptime_seconds: number;
  all_time_count: number;
  all_time_max_spread_pct: string;
  all_time_peak_minute: PeakMinute | null;
};

export type WindowStats = {
  window: string;
  count: number;
  max_spread_pct: string;
  mean_spread_pct: string;
  total_theoretical_profit_usd: string;
  top_pair: string | null;
  peak_minute: PeakMinute | null;
};

export type TimeseriesPoint = {
  bucket_start_ns: string;
  count: number;
  max_spread_pct: string;
};

export type Timeseries = {
  window: string;
  bucket_seconds: number;
  points: TimeseriesPoint[];
};

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
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    const suffix = detail ? `: ${detail}` : "";
    throw new ApiError(response.status, path, `Request failed (${response.status})${suffix}`);
  }
  return (await response.json()) as T;
}

export async function fetchRecentOpportunities(): Promise<Opportunity[]> {
  return requestJson<Opportunity[]>("/api/opportunities/recent?limit=50");
}

export async function fetchStats(): Promise<Stats> {
  return requestJson<Stats>("/api/stats?window=1h");
}

export async function fetchPairs(): Promise<PairRecord[]> {
  return requestJson<PairRecord[]>("/api/pairs");
}

export async function fetchAdapterStatus(): Promise<AdapterStatus[]> {
  return requestJson<AdapterStatus[]>("/api/adapters");
}

export async function fetchBookStatus(): Promise<BookStatus[]> {
  return requestJson<BookStatus[]>("/api/book-status");
}

export async function fetchSystemOverview(): Promise<SystemOverview> {
  return requestJson<SystemOverview>("/api/system/overview");
}

export async function fetchSystemStats(window: WindowKey): Promise<WindowStats> {
  return requestJson<WindowStats>(`/api/system/stats?window=${window}`);
}

export async function fetchSystemTimeseries(
  window: WindowKey,
  bucketSeconds = 60,
): Promise<Timeseries> {
  return requestJson<Timeseries>(
    `/api/system/timeseries?window=${window}&bucket_seconds=${bucketSeconds}`,
  );
}
