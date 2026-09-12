import {
  decodeAdapterStatuses,
  decodeBookStatuses,
  decodeOpportunities,
  decodePairs,
  decodeStats,
  decodeSystemOverview,
  decodeTimeseries,
  decodeWindowStats,
  type AdapterStatus,
  type BookStatus,
  type Opportunity,
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
  Opportunity,
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
