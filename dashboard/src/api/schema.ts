export type EpisodeCloseReason = "spread_closed" | "book_ineligible" | "shutdown" | "orphaned";

export type PricingLedger = {
  notional: string;
  top_of_book_spread_pct: string;
  buy_vwap: string | null;
  sell_vwap: string | null;
  gross_executable_spread_pct: string | null;
  depth_impact_pct: string | null;
  buy_taker_fee_pct: string;
  sell_taker_fee_pct: string;
  fee_impact_pct: string | null;
  net_executable_spread_pct: string | null;
  insufficient_depth: boolean;
};

/**
 * One dislocation from appearance to disappearance on a (pair, buy, sell)
 * route. `start_ns` identifies it; a close for the same episode arrives as a
 * second message with the same `start_ns` and `end_ns` filled in. The plain
 * price, spread, size and profit fields are the values at open; `peak_*` are
 * the widest spread seen and the size and profit at that moment.
 * `close_reason` `"orphaned"` means a previous process died while the episode
 * was still open: `end_ns` and `duration_ns` stay null because the lifetime is
 * unknown, and the row must not be treated as currently open.
 */
export type Opportunity = {
  start_ns: string;
  end_ns: string | null;
  duration_ns: string | null;
  pair: string;
  quote_asset: string;
  buy_exchange: string;
  sell_exchange: string;
  buy_price: string;
  sell_price: string;
  spread_pct: string;
  max_size: string;
  theoretical_profit: string;
  peak_spread_pct: string;
  peak_size: string;
  peak_profit: string;
  pricing_ledgers: PricingLedger[];
  close_spread_pct: string | null;
  close_reason: EpisodeCloseReason | null;
};

export type Lifetime = {
  closed_count: number;
  p50_seconds: number;
  p90_seconds: number;
  max_seconds: number;
};

export type PairRecord = { exchange: string; pair: string };

export type Stats = {
  count: number;
  max_spread_pct: string;
  theoretical_profit_by_quote: Record<string, string>;
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

export type PeakMinute = { minute_start_ns: string; count: number };

export type SystemOverview = {
  started_at_ns: string;
  uptime_seconds: number;
  all_time_count: number;
  all_time_max_spread_pct: string;
  all_time_peak_minute: PeakMinute | null;
  open_count: number;
  all_time_lifetime: Lifetime | null;
};

export type WindowStats = {
  window: string;
  count: number;
  max_spread_pct: string;
  mean_spread_pct: string;
  theoretical_profit_by_quote: Record<string, string>;
  top_pair: string | null;
  peak_minute: PeakMinute | null;
  lifetime: Lifetime | null;
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

type LivePayload =
  | { type: "top_of_book"; payload: TopOfBook }
  | { type: "opportunity"; payload: Opportunity }
  | { type: "book_status"; payload: BookStatus }
  | {
      type: "state_snapshot";
      payload: { books: TopOfBook[]; statuses: BookStatus[] };
    };

export type LiveEnvelope = LivePayload & { stream_sequence: number };

export class PayloadValidationError extends Error {
  constructor(
    public readonly location: string,
    expected: string,
  ) {
    super(`${location} must be ${expected}`);
    this.name = "PayloadValidationError";
  }
}

type JsonObject = Record<string, unknown>;
type Decoder<T> = (value: unknown, location: string) => T;

const DECIMAL = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;
const NANOSECONDS = /^(?:0|[1-9]\d*)$/;

function object(value: unknown, location: string): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new PayloadValidationError(location, "an object");
  }
  return value as JsonObject;
}

function field(value: JsonObject, name: string, location: string): unknown {
  if (!(name in value)) {
    throw new PayloadValidationError(`${location}.${name}`, "present");
  }
  return value[name];
}

function text(value: unknown, location: string): string {
  if (typeof value !== "string") {
    throw new PayloadValidationError(location, "a string");
  }
  return value;
}

function boolean(value: unknown, location: string): boolean {
  if (typeof value !== "boolean") {
    throw new PayloadValidationError(location, "a boolean");
  }
  return value;
}

function nonnegativeNumber(value: unknown, location: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new PayloadValidationError(location, "a finite non-negative number");
  }
  return value;
}

function nonnegativeInteger(value: unknown, location: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw new PayloadValidationError(location, "a non-negative safe integer");
  }
  return value as number;
}

function positiveInteger(value: unknown, location: string): number {
  const result = nonnegativeInteger(value, location);
  if (result === 0) {
    throw new PayloadValidationError(location, "a positive safe integer");
  }
  return result;
}

function decimal(value: unknown, location: string): string {
  const result = text(value, location);
  if (!DECIMAL.test(result)) {
    throw new PayloadValidationError(location, "a finite decimal string");
  }
  return result;
}

function nanoseconds(value: unknown, location: string): string {
  const result = text(value, location);
  if (!NANOSECONDS.test(result)) {
    throw new PayloadValidationError(location, "a non-negative nanosecond integer string");
  }
  return result;
}

function nullable<T>(value: unknown, location: string, decode: Decoder<T>): T | null {
  return value === null ? null : decode(value, location);
}

function array<T>(value: unknown, location: string, decode: Decoder<T>): T[] {
  if (!Array.isArray(value)) {
    throw new PayloadValidationError(location, "an array");
  }
  return value.map((entry, index) => decode(entry, `${location}[${index}]`));
}

function decimalRecord(value: unknown, location: string): Record<string, string> {
  const source = object(value, location);
  return Object.fromEntries(
    Object.entries(source).map(([key, entry]) => [key, decimal(entry, `${location}.${key}`)]),
  );
}

const CLOSE_REASONS: readonly EpisodeCloseReason[] = [
  "spread_closed",
  "book_ineligible",
  "shutdown",
  "orphaned",
];

function closeReason(value: unknown, location: string): EpisodeCloseReason {
  const result = text(value, location);
  if (!(CLOSE_REASONS as readonly string[]).includes(result)) {
    throw new PayloadValidationError(location, `one of ${CLOSE_REASONS.join(", ")}`);
  }
  return result as EpisodeCloseReason;
}

/** A signed decimal: the spread at close may be negative. */
function signedDecimal(value: unknown, location: string): string {
  return decimal(value, location);
}

function decodePricingLedger(value: unknown, location: string): PricingLedger {
  const source = object(value, location);
  const optionalDecimal = (name: string) =>
    nullable(field(source, name, location), `${location}.${name}`, signedDecimal);
  return {
    notional: decimal(field(source, "notional", location), `${location}.notional`),
    top_of_book_spread_pct: signedDecimal(
      field(source, "top_of_book_spread_pct", location),
      `${location}.top_of_book_spread_pct`,
    ),
    buy_vwap: optionalDecimal("buy_vwap"),
    sell_vwap: optionalDecimal("sell_vwap"),
    gross_executable_spread_pct: optionalDecimal("gross_executable_spread_pct"),
    depth_impact_pct: optionalDecimal("depth_impact_pct"),
    buy_taker_fee_pct: decimal(
      field(source, "buy_taker_fee_pct", location),
      `${location}.buy_taker_fee_pct`,
    ),
    sell_taker_fee_pct: decimal(
      field(source, "sell_taker_fee_pct", location),
      `${location}.sell_taker_fee_pct`,
    ),
    fee_impact_pct: optionalDecimal("fee_impact_pct"),
    net_executable_spread_pct: optionalDecimal("net_executable_spread_pct"),
    insufficient_depth: boolean(
      field(source, "insufficient_depth", location),
      `${location}.insufficient_depth`,
    ),
  };
}

export function decodeOpportunity(value: unknown, location = "opportunity"): Opportunity {
  const source = object(value, location);
  const endNs = nullable(field(source, "end_ns", location), `${location}.end_ns`, nanoseconds);
  const durationNs = nullable(
    field(source, "duration_ns", location),
    `${location}.duration_ns`,
    nanoseconds,
  );
  if ((endNs === null) !== (durationNs === null)) {
    throw new PayloadValidationError(location, "closed with both end_ns and duration_ns or open");
  }
  const reason = nullable(
    field(source, "close_reason", location),
    `${location}.close_reason`,
    closeReason,
  );
  if (reason === "orphaned" && (endNs !== null || durationNs !== null)) {
    throw new PayloadValidationError(location, "orphaned without end_ns or duration_ns");
  }
  return {
    start_ns: nanoseconds(field(source, "start_ns", location), `${location}.start_ns`),
    end_ns: endNs,
    duration_ns: durationNs,
    pair: text(field(source, "pair", location), `${location}.pair`),
    quote_asset: text(field(source, "quote_asset", location), `${location}.quote_asset`),
    buy_exchange: text(field(source, "buy_exchange", location), `${location}.buy_exchange`),
    sell_exchange: text(field(source, "sell_exchange", location), `${location}.sell_exchange`),
    buy_price: decimal(field(source, "buy_price", location), `${location}.buy_price`),
    sell_price: decimal(field(source, "sell_price", location), `${location}.sell_price`),
    spread_pct: decimal(field(source, "spread_pct", location), `${location}.spread_pct`),
    max_size: decimal(field(source, "max_size", location), `${location}.max_size`),
    theoretical_profit: decimal(
      field(source, "theoretical_profit", location),
      `${location}.theoretical_profit`,
    ),
    peak_spread_pct: decimal(
      field(source, "peak_spread_pct", location),
      `${location}.peak_spread_pct`,
    ),
    peak_size: decimal(field(source, "peak_size", location), `${location}.peak_size`),
    peak_profit: decimal(field(source, "peak_profit", location), `${location}.peak_profit`),
    pricing_ledgers: array(
      field(source, "pricing_ledgers", location),
      `${location}.pricing_ledgers`,
      decodePricingLedger,
    ),
    close_spread_pct: nullable(
      field(source, "close_spread_pct", location),
      `${location}.close_spread_pct`,
      signedDecimal,
    ),
    close_reason: reason,
  };
}

export function decodeLifetime(value: unknown, location = "lifetime"): Lifetime {
  const source = object(value, location);
  return {
    closed_count: positiveInteger(
      field(source, "closed_count", location),
      `${location}.closed_count`,
    ),
    p50_seconds: nonnegativeNumber(
      field(source, "p50_seconds", location),
      `${location}.p50_seconds`,
    ),
    p90_seconds: nonnegativeNumber(
      field(source, "p90_seconds", location),
      `${location}.p90_seconds`,
    ),
    max_seconds: nonnegativeNumber(
      field(source, "max_seconds", location),
      `${location}.max_seconds`,
    ),
  };
}

export function decodePair(value: unknown, location = "pair"): PairRecord {
  const source = object(value, location);
  return {
    exchange: text(field(source, "exchange", location), `${location}.exchange`),
    pair: text(field(source, "pair", location), `${location}.pair`),
  };
}

export function decodeStats(value: unknown, location = "stats"): Stats {
  const source = object(value, location);
  return {
    count: nonnegativeInteger(field(source, "count", location), `${location}.count`),
    max_spread_pct: decimal(
      field(source, "max_spread_pct", location),
      `${location}.max_spread_pct`,
    ),
    theoretical_profit_by_quote: decimalRecord(
      field(source, "theoretical_profit_by_quote", location),
      `${location}.theoretical_profit_by_quote`,
    ),
  };
}

export function decodeAdapterStatus(
  value: unknown,
  location = "adapter_status",
): AdapterStatus {
  const source = object(value, location);
  return {
    exchange: text(field(source, "exchange", location), `${location}.exchange`),
    connected: boolean(field(source, "connected", location), `${location}.connected`),
    last_message_age_ms: nullable(
      field(source, "last_message_age_ms", location),
      `${location}.last_message_age_ms`,
      nonnegativeNumber,
    ),
    gap_count: nonnegativeInteger(field(source, "gap_count", location), `${location}.gap_count`),
    reconnect_count: nonnegativeInteger(
      field(source, "reconnect_count", location),
      `${location}.reconnect_count`,
    ),
    last_error: nullable(field(source, "last_error", location), `${location}.last_error`, text),
  };
}

export function decodeBookStatus(value: unknown, location = "book_status"): BookStatus {
  const source = object(value, location);
  return {
    exchange: text(field(source, "exchange", location), `${location}.exchange`),
    pair: text(field(source, "pair", location), `${location}.pair`),
    initialized: boolean(field(source, "initialized", location), `${location}.initialized`),
    continuous: boolean(field(source, "continuous", location), `${location}.continuous`),
    connected: boolean(field(source, "connected", location), `${location}.connected`),
    age_ms: nullable(field(source, "age_ms", location), `${location}.age_ms`, nonnegativeNumber),
    max_age_ms: nonnegativeNumber(
      field(source, "max_age_ms", location),
      `${location}.max_age_ms`,
    ),
    eligible: boolean(field(source, "eligible", location), `${location}.eligible`),
    reason: nullable(field(source, "reason", location), `${location}.reason`, text),
  };
}

export function decodeTopOfBook(value: unknown, location = "top_of_book"): TopOfBook {
  const source = object(value, location);
  return {
    exchange: text(field(source, "exchange", location), `${location}.exchange`),
    pair: text(field(source, "pair", location), `${location}.pair`),
    best_bid_price: decimal(
      field(source, "best_bid_price", location),
      `${location}.best_bid_price`,
    ),
    best_bid_size: decimal(field(source, "best_bid_size", location), `${location}.best_bid_size`),
    best_ask_price: decimal(
      field(source, "best_ask_price", location),
      `${location}.best_ask_price`,
    ),
    best_ask_size: decimal(field(source, "best_ask_size", location), `${location}.best_ask_size`),
    sequence: nonnegativeInteger(field(source, "sequence", location), `${location}.sequence`),
    timestamp_ns: nanoseconds(field(source, "timestamp_ns", location), `${location}.timestamp_ns`),
  };
}

function decodePeakMinute(value: unknown, location = "peak_minute"): PeakMinute {
  const source = object(value, location);
  return {
    minute_start_ns: nanoseconds(
      field(source, "minute_start_ns", location),
      `${location}.minute_start_ns`,
    ),
    count: nonnegativeInteger(field(source, "count", location), `${location}.count`),
  };
}

export function decodeSystemOverview(
  value: unknown,
  location = "system_overview",
): SystemOverview {
  const source = object(value, location);
  return {
    started_at_ns: nanoseconds(
      field(source, "started_at_ns", location),
      `${location}.started_at_ns`,
    ),
    uptime_seconds: nonnegativeNumber(
      field(source, "uptime_seconds", location),
      `${location}.uptime_seconds`,
    ),
    all_time_count: nonnegativeInteger(
      field(source, "all_time_count", location),
      `${location}.all_time_count`,
    ),
    all_time_max_spread_pct: decimal(
      field(source, "all_time_max_spread_pct", location),
      `${location}.all_time_max_spread_pct`,
    ),
    all_time_peak_minute: nullable(
      field(source, "all_time_peak_minute", location),
      `${location}.all_time_peak_minute`,
      decodePeakMinute,
    ),
    open_count: nonnegativeInteger(
      field(source, "open_count", location),
      `${location}.open_count`,
    ),
    all_time_lifetime: nullable(
      field(source, "all_time_lifetime", location),
      `${location}.all_time_lifetime`,
      decodeLifetime,
    ),
  };
}

export function decodeWindowStats(value: unknown, location = "window_stats"): WindowStats {
  const source = object(value, location);
  return {
    window: text(field(source, "window", location), `${location}.window`),
    count: nonnegativeInteger(field(source, "count", location), `${location}.count`),
    max_spread_pct: decimal(
      field(source, "max_spread_pct", location),
      `${location}.max_spread_pct`,
    ),
    mean_spread_pct: decimal(
      field(source, "mean_spread_pct", location),
      `${location}.mean_spread_pct`,
    ),
    theoretical_profit_by_quote: decimalRecord(
      field(source, "theoretical_profit_by_quote", location),
      `${location}.theoretical_profit_by_quote`,
    ),
    top_pair: nullable(field(source, "top_pair", location), `${location}.top_pair`, text),
    peak_minute: nullable(
      field(source, "peak_minute", location),
      `${location}.peak_minute`,
      decodePeakMinute,
    ),
    lifetime: nullable(field(source, "lifetime", location), `${location}.lifetime`, decodeLifetime),
  };
}

function decodeTimeseriesPoint(
  value: unknown,
  location = "timeseries_point",
): TimeseriesPoint {
  const source = object(value, location);
  return {
    bucket_start_ns: nanoseconds(
      field(source, "bucket_start_ns", location),
      `${location}.bucket_start_ns`,
    ),
    count: nonnegativeInteger(field(source, "count", location), `${location}.count`),
    max_spread_pct: decimal(
      field(source, "max_spread_pct", location),
      `${location}.max_spread_pct`,
    ),
  };
}

export function decodeTimeseries(value: unknown, location = "timeseries"): Timeseries {
  const source = object(value, location);
  return {
    window: text(field(source, "window", location), `${location}.window`),
    bucket_seconds: positiveInteger(
      field(source, "bucket_seconds", location),
      `${location}.bucket_seconds`,
    ),
    points: array(
      field(source, "points", location),
      `${location}.points`,
      decodeTimeseriesPoint,
    ),
  };
}

export const decodeOpportunities = (value: unknown, location = "opportunities"): Opportunity[] =>
  array(value, location, decodeOpportunity);
export const decodePairs = (value: unknown, location = "pairs"): PairRecord[] =>
  array(value, location, decodePair);
export const decodeAdapterStatuses = (
  value: unknown,
  location = "adapter_statuses",
): AdapterStatus[] => array(value, location, decodeAdapterStatus);
export const decodeBookStatuses = (value: unknown, location = "book_statuses"): BookStatus[] =>
  array(value, location, decodeBookStatus);

export function decodeLiveEnvelope(value: unknown, location = "live_frame"): LiveEnvelope {
  const source = object(value, location);
  const type = text(field(source, "type", location), `${location}.type`);
  const streamSequence = positiveInteger(
    field(source, "stream_sequence", location),
    `${location}.stream_sequence`,
  );
  const payload = field(source, "payload", location);

  switch (type) {
    case "top_of_book":
      return { type, stream_sequence: streamSequence, payload: decodeTopOfBook(payload) };
    case "opportunity":
      return { type, stream_sequence: streamSequence, payload: decodeOpportunity(payload) };
    case "book_status":
      return { type, stream_sequence: streamSequence, payload: decodeBookStatus(payload) };
    case "state_snapshot": {
      const snapshot = object(payload, `${location}.payload`);
      return {
        type,
        stream_sequence: streamSequence,
        payload: {
          books: array(
            field(snapshot, "books", `${location}.payload`),
            `${location}.payload.books`,
            decodeTopOfBook,
          ),
          statuses: array(
            field(snapshot, "statuses", `${location}.payload`),
            `${location}.payload.statuses`,
            decodeBookStatus,
          ),
        },
      };
    }
    default:
      throw new PayloadValidationError(`${location}.type`, "a known message type");
  }
}
