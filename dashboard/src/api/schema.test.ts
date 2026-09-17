import { describe, expect, it } from "vitest";
import {
  decodeAdapterStatuses,
  decodeBookStatuses,
  decodeLiveEnvelope,
  decodeOpportunities,
  decodePairs,
  decodeStats,
  decodeSystemOverview,
  decodeTimeseries,
  decodeWindowStats,
  PayloadValidationError,
} from "./schema";

const opportunity = {
  start_ns: "1720000000000000000",
  end_ns: null,
  duration_ns: null,
  pair: "BTC-USD",
  quote_asset: "USD",
  buy_exchange: "gemini",
  sell_exchange: "coinbase",
  buy_price: "60000.125",
  sell_price: "6.01E+4",
  spread_pct: ".001",
  max_size: "1",
  theoretical_profit: "+7.50",
  peak_spread_pct: ".002",
  peak_size: "0.5",
  peak_profit: "7.50",
  pricing_ledgers: [
    {
      notional: "1000",
      top_of_book_spread_pct: "1.2",
      buy_vwap: "100.1",
      sell_vwap: "100.8",
      gross_executable_spread_pct: "0.6993",
      depth_impact_pct: "-0.5007",
      buy_taker_fee_pct: "0.4",
      sell_taker_fee_pct: "0.6",
      fee_impact_pct: "-1.006",
      net_executable_spread_pct: "-0.3067",
      insufficient_depth: false,
    },
  ],
  close_spread_pct: null,
  close_reason: null,
};

const closedOpportunity = {
  ...opportunity,
  end_ns: "1720000003000000000",
  duration_ns: "3000000000",
  close_spread_pct: "-0.4",
  close_reason: "spread_closed",
};

const orphanedOpportunity = {
  ...opportunity,
  close_reason: "orphaned",
};

const lifetime = { closed_count: 3, p50_seconds: 4, p90_seconds: 30.5, max_seconds: 30.5 };

const book = {
  exchange: "gemini",
  pair: "BTC-USD",
  best_bid_price: "60000.125",
  best_bid_size: "1",
  best_ask_price: "60001.25",
  best_ask_size: "2",
  sequence: 42,
  timestamp_ns: "1720000000000000000",
};

const status = {
  exchange: "gemini",
  pair: "BTC-USD",
  initialized: true,
  continuous: true,
  connected: true,
  age_ms: 12,
  max_age_ms: 60_000,
  eligible: true,
  reason: null,
};

describe("network payload schemas", () => {
  it.each([
    ["opportunities", decodeOpportunities, [opportunity, closedOpportunity, orphanedOpportunity]],
    ["pairs", decodePairs, [{ exchange: "gemini", pair: "BTC-USD" }]],
    [
      "statistics",
      decodeStats,
      { count: 1, max_spread_pct: "0.1", theoretical_profit_by_quote: { USD: "2.5" } },
    ],
    [
      "adapter status",
      decodeAdapterStatuses,
      [
        {
          exchange: "gemini",
          connected: true,
          last_message_age_ms: null,
          gap_count: 0,
          reconnect_count: 1,
          last_error: null,
        },
      ],
    ],
    ["book status", decodeBookStatuses, [status]],
    [
      "system overview",
      decodeSystemOverview,
      {
        started_at_ns: "1720000000000000000",
        uptime_seconds: 10,
        all_time_count: 1,
        all_time_max_spread_pct: "0.1",
        all_time_peak_minute: { minute_start_ns: "1720000000000000000", count: 1 },
        open_count: 2,
        all_time_lifetime: lifetime,
      },
    ],
    [
      "window statistics",
      decodeWindowStats,
      {
        window: "1h",
        count: 1,
        max_spread_pct: "0.1",
        mean_spread_pct: "0.05",
        theoretical_profit_by_quote: { USD: "2.5" },
        top_pair: "BTC-USD",
        peak_minute: null,
        lifetime: null,
      },
    ],
    [
      "timeseries",
      decodeTimeseries,
      {
        window: "1h",
        bucket_seconds: 60,
        points: [
          { bucket_start_ns: "1720000000000000000", count: 1, max_spread_pct: "0.1" },
        ],
      },
    ],
  ])("accepts a valid %s response", (_name, decode, payload) => {
    expect(() => decode(payload)).not.toThrow();
  });

  it("accepts every valid live message type", () => {
    const frames = [
      { type: "top_of_book", stream_sequence: 1, payload: book },
      { type: "opportunity", stream_sequence: 2, payload: opportunity },
      { type: "book_status", stream_sequence: 3, payload: status },
      {
        type: "state_snapshot",
        stream_sequence: 4,
        payload: { books: [book], statuses: [status] },
      },
    ];

    expect(frames.map((frame) => decodeLiveEnvelope(frame).type)).toEqual([
      "top_of_book",
      "opportunity",
      "book_status",
      "state_snapshot",
    ]);
  });

  it.each([
    ["missing fields", { type: "top_of_book", stream_sequence: 1, payload: {} }],
    [
      "invalid Decimal strings",
      { type: "top_of_book", stream_sequence: 1, payload: { ...book, best_bid_price: "NaN" } },
    ],
    [
      "invalid nanosecond timestamps",
      { type: "opportunity", stream_sequence: 1, payload: { ...opportunity, start_ns: "1.5" } },
    ],
    [
      "an episode with an end but no duration",
      {
        type: "opportunity",
        stream_sequence: 1,
        payload: { ...closedOpportunity, duration_ns: null },
      },
    ],
    [
      "an unknown close reason",
      {
        type: "opportunity",
        stream_sequence: 1,
        payload: { ...closedOpportunity, close_reason: "evaporated" },
      },
    ],
    [
      "an orphaned episode with a fabricated lifetime",
      {
        type: "opportunity",
        stream_sequence: 1,
        payload: { ...closedOpportunity, close_reason: "orphaned" },
      },
    ],
    ["unknown message types", { type: "unknown", stream_sequence: 1, payload: {} }],
  ])("rejects %s", (_name, frame) => {
    expect(() => decodeLiveEnvelope(frame)).toThrow(PayloadValidationError);
  });
});
