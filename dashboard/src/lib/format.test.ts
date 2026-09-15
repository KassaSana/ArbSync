import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DASH,
  age,
  clockTime,
  count,
  dateTime,
  eventTime,
  nsToMs,
  price,
  quoteAmount,
  spreadPct,
  uptime,
  usd,
} from "./format";

/** Nanosecond string for a local wall-clock instant, the shape the API sends. */
function ns(date: Date): string {
  return `${BigInt(date.getTime()) * 1_000_000n}`;
}

const pad = (value: number, width = 2): string => String(value).padStart(width, "0");

describe("quantity formatting", () => {
  it.each([
    [null, DASH],
    [undefined, DASH],
    ["0", DASH],
    ["abc", DASH],
    ["0.12345", "0.123%"],
    [1.5, "1.500%"],
  ])("spreadPct(%j) -> %s", (input, expected) => {
    expect(spreadPct(input)).toBe(expected);
  });

  it.each([
    [null, DASH],
    ["0", DASH],
    ["nope", DASH],
    ["2.5", "$2.50"],
    [1234.567, "$1,234.57"],
  ])("usd(%j) -> %s", (input, expected) => {
    expect(usd(input)).toBe(expected);
  });

  it("quoteAmount uses the dollar form for USD and suffixes other quotes", () => {
    expect(quoteAmount("2.5", "USD")).toBe("$2.50");
    expect(quoteAmount("3.5", "USDT")).toBe("3.50 USDT");
    expect(quoteAmount(null, "USDT")).toBe(DASH);
    expect(quoteAmount("0", "USDT")).toBe(DASH);
  });

  it("price keeps precision proportional to magnitude", () => {
    expect(price("65432.1")).toBe("65,432.10");
    expect(price("1.5")).toBe("1.5000");
    expect(price("0.000123456")).toBe("0.000123");
    expect(price(null)).toBe(DASH);
    expect(price("x")).toBe(DASH);
  });

  it("count separates thousands or dashes", () => {
    expect(count(1234567)).toBe("1,234,567");
    expect(count(null)).toBe(DASH);
    expect(count(Number.NaN)).toBe(DASH);
  });
});

describe("duration formatting", () => {
  it.each([
    [null, DASH],
    [Number.POSITIVE_INFINITY, DASH],
    [-50, "0ms"],
    [999, "999ms"],
    [1000, "1.0s"],
    [59_999, "60.0s"],
    [60_000, "1m"],
    [59 * 60_000, "59m"],
    [60 * 60_000, "1h"],
    [23 * 3_600_000, "23h"],
    [24 * 3_600_000, "1d"],
    [3 * 86_400_000, "3d"],
  ])("age(%j) -> %s", (input, expected) => {
    expect(age(input)).toBe(expected);
  });

  it.each([
    [-1, "0s"],
    [Number.NaN, "0s"],
    [45, "45s"],
    [125, "2m 5s"],
    [3_725, "1h 2m 5s"],
    [90_061, "1d 1h 1m"],
  ])("uptime(%j) -> %s", (input, expected) => {
    expect(uptime(input)).toBe(expected);
  });
});

describe("time formatting", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("converts nanosecond strings to milliseconds without float drift", () => {
    expect(nsToMs("1700000000123456789")).toBe(1700000000123);
  });

  it("clockTime renders local wall time with milliseconds", () => {
    const date = new Date(2026, 8, 15, 9, 5, 7, 42);
    expect(clockTime(ns(date))).toBe("09:05:07.042");
  });

  it("eventTime drops the date only for events from today", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 8, 15, 12, 0, 0));
    const today = new Date(2026, 8, 15, 8, 30, 0, 5);
    const yesterday = new Date(2026, 8, 14, 8, 30, 0, 5);

    expect(eventTime(ns(today))).toBe("08:30:00.005");
    const day = yesterday.toLocaleDateString([], { month: "short", day: "numeric" });
    expect(eventTime(ns(yesterday))).toBe(`${day} 08:30:00.005`);
  });

  it("dateTime uses the runtime locale for the full stamp", () => {
    const date = new Date(2026, 8, 15, 9, 5, 7);
    expect(dateTime(ns(date))).toBe(date.toLocaleString());
    expect(dateTime(ns(date))).toContain(pad(date.getMinutes()));
  });
});
