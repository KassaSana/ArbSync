/**
 * Single source of truth for how every quantity in the dashboard is rendered.
 * Precision lives here so the same number never appears in two formats.
 */

export const DASH = "\u2014";

export function nsToMs(ns: string): number {
  return Number(BigInt(ns) / 1_000_000n);
}

/** Spreads are always three decimals, everywhere, or a dash. */
export function spreadPct(value: string | number | null | undefined): string {
  if (value === null || value === undefined) {
    return DASH;
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed === 0) {
    return DASH;
  }
  return `${parsed.toFixed(3)}%`;
}

export function usd(value: string | number | null | undefined): string {
  if (value === null || value === undefined) {
    return DASH;
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed === 0) {
    return DASH;
  }
  return `$${parsed.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function quoteAmount(
  value: string | number | null | undefined,
  quoteAsset: string,
): string {
  if (quoteAsset === "USD") {
    return usd(value);
  }
  if (value === null || value === undefined) {
    return DASH;
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed === 0) {
    return DASH;
  }
  return `${parsed.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} ${quoteAsset}`;
}

/** Book prices keep precision proportional to magnitude so columns stay aligned. */
export function price(value: string | null | undefined): string {
  if (value === null || value === undefined) {
    return DASH;
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return DASH;
  }
  const digits = parsed >= 1000 ? 2 : parsed >= 1 ? 4 : 6;
  return parsed.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function count(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return DASH;
  }
  return value.toLocaleString();
}

/** Compact age for freshness readouts: 840ms, 4.2s, 7m, 3h, 2d. */
export function age(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) {
    return DASH;
  }
  const safe = Math.max(0, ms);
  if (safe < 1000) {
    return `${Math.round(safe)}ms`;
  }
  const seconds = safe / 1000;
  if (seconds < 60) {
    return `${seconds.toFixed(1)}s`;
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `${minutes}m`;
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return `${hours}h`;
  }
  return `${Math.floor(hours / 24)}d`;
}

export function uptime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) {
    return "0s";
  }
  const days = Math.floor(seconds / 86_400);
  const hours = Math.floor((seconds % 86_400) / 3_600);
  const minutes = Math.floor((seconds % 3_600) / 60);
  const secs = Math.floor(seconds % 60);
  if (days > 0) {
    return `${days}d ${hours}h ${minutes}m`;
  }
  if (hours > 0) {
    return `${hours}h ${minutes}m ${secs}s`;
  }
  if (minutes > 0) {
    return `${minutes}m ${secs}s`;
  }
  return `${secs}s`;
}

function pad(value: number, width = 2): string {
  return String(value).padStart(width, "0");
}

/**
 * 24-hour with milliseconds. Detections land several times a second, so
 * second-resolution stamps make distinct events look like duplicated rows.
 */
export function clockTime(ns: string): string {
  const date = new Date(nsToMs(ns));
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(
    date.getSeconds(),
  )}.${pad(date.getMilliseconds(), 3)}`;
}

/**
 * Same as clockTime, but carries the date when the event is not from today.
 * The feed holds days of history, so a bare wall clock is ambiguous.
 */
export function eventTime(ns: string): string {
  const date = new Date(nsToMs(ns));
  const time = clockTime(ns);
  if (date.toDateString() === new Date().toDateString()) {
    return time;
  }
  const day = date.toLocaleDateString([], { month: "short", day: "numeric" });
  return `${day} ${time}`;
}

export function dateTime(ns: string): string {
  return new Date(nsToMs(ns)).toLocaleString();
}
