import { ConnectionStatus } from "../hooks/useWebSocket";
import { age } from "../lib/format";
import { HealthMark, Health } from "./HealthMark";
import { Nav } from "./Nav";

type Props = {
  status: ConnectionStatus;
  lastTickAgeMs: number | null;
  invalidFrameCount: number;
};

const STATUS_HEALTH: Record<ConnectionStatus, Health> = {
  connected: "ok",
  connecting: "stale",
  reconnecting: "down",
};

const STATUS_LABEL: Record<ConnectionStatus, string> = {
  connected: "Live",
  connecting: "Connecting",
  reconnecting: "Reconnecting",
};

/**
 * 48px of chrome instead of a masthead. The stream indicator lives here
 * because "is this thing still receiving data" is the question a monitor
 * has to answer before any other.
 */
export function TopBar({ status, lastTickAgeMs, invalidFrameCount }: Props) {
  return (
    <header className="sticky top-0 z-40 border-b border-line bg-ground/90 backdrop-blur">
      <div className="mx-auto flex h-12 max-w-[1600px] items-center gap-4 px-4">
        <div className="flex items-baseline gap-2.5">
          <span className="text-sm font-semibold tracking-tight text-ink">ArbSync</span>
          <span className="hidden text-micro text-ink-3 sm:inline">Cross-exchange monitor</span>
        </div>

        <div className="ml-auto flex items-center gap-4">
          <Nav />
          <div
            className="flex items-center gap-2 border-l border-line pl-4"
            aria-live="polite"
            aria-atomic="true"
          >
            <HealthMark health={STATUS_HEALTH[status]} />
            <span className="text-xs text-ink-2">{STATUS_LABEL[status]}</span>
            {/* Freshness only earns space once it is worth worrying about. */}
            {status === "connected" && lastTickAgeMs !== null && lastTickAgeMs >= 2_000 ? (
              <span className="num text-micro text-warn">{age(lastTickAgeMs)}</span>
            ) : null}
            {invalidFrameCount > 0 ? (
              <span className="num text-micro text-warn" title="Malformed live frames ignored">
                {invalidFrameCount} invalid
              </span>
            ) : null}
          </div>
        </div>
      </div>
    </header>
  );
}
