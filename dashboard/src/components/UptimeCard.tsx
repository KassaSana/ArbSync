import { DASH, nsToMs, uptime } from "../lib/format";
import { useLive } from "../state/live";
import { Panel } from "./Panel";

type Props = {
  startedAtNs: string | null;
};

export function UptimeCard({ startedAtNs }: Props) {
  // Borrows the one-second tick the live provider already runs.
  const { nowMs } = useLive();

  const startedAtMs = startedAtNs === null ? null : nsToMs(startedAtNs);
  const uptimeSeconds =
    startedAtMs === null ? 0 : Math.max(0, Math.floor((nowMs - startedAtMs) / 1_000));

  return (
    <Panel>
      <div className="flex flex-wrap items-end justify-between gap-4 px-4 py-3">
        <div>
          <p className="text-micro text-ink-3">System uptime</p>
          <p className="num mt-1.5 text-3xl font-medium text-ink">
            {startedAtMs === null ? DASH : uptime(uptimeSeconds)}
          </p>
          <p className="mt-1 text-micro text-ink-3">
            {startedAtMs === null
              ? "Start time unavailable"
              : `Running since ${new Date(startedAtMs).toLocaleString()}`}
          </p>
        </div>
      </div>
    </Panel>
  );
}
