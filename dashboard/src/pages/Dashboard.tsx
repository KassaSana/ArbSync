import { AdapterStatusBanner } from "../components/AdapterStatus";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { LiveSpreads } from "../components/LiveSpreads";
import { OpportunityFeed } from "../components/OpportunityFeed";
import { Placeholder } from "../components/Placeholder";
import { StatsCards } from "../components/StatsCards";
import { VenueComparison } from "../components/VenueComparison";
import { age } from "../lib/format";
import { useLive } from "../state/live";

/**
 * When the socket drops we keep the last known values on screen and mark them
 * as old. Blanking the table would tell the user "no opportunities exist",
 * which is a different and much worse claim than "I lost the feed".
 */
function StaleNotice({ lastTickAgeMs }: { lastTickAgeMs: number | null }) {
  return (
    <div
      role="status"
      className="flex flex-wrap items-baseline gap-x-2 gap-y-1 rounded border border-warn/45 bg-warn/[0.06] px-4 py-2.5 text-xs"
    >
      <span className="font-medium text-warn">Live feed interrupted</span>
      <span className="text-ink-2">
        Showing the last values received
        {lastTickAgeMs === null ? "" : ` ${age(lastTickAgeMs)} ago`}. Reconnecting
        automatically.
      </span>
    </div>
  );
}

export default function Dashboard() {
  const live = useLive();

  return (
    <div className="space-y-3">
      {live.feedLive ? null : <StaleNotice lastTickAgeMs={live.lastTickAgeMs} />}

      <ErrorBoundary label="Exchange connectivity">
        <AdapterStatusBanner
          adapters={live.adapters}
          books={live.bookStatuses}
          feedLive={live.feedLive}
          onRetry={live.refreshAdapters}
        />
      </ErrorBoundary>

      <ErrorBoundary label="Hourly stats">
        <StatsCards stats={live.stats} onRetry={live.refreshStats} />
      </ErrorBoundary>

      <div className="grid items-start gap-3 xl:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
        <ErrorBoundary label="Live spreads">
          {live.pairs.state === "failed" ? (
            <Placeholder
              state="failed"
              title="Could not load the tracked pair list"
              detail={live.pairs.error}
              onRetry={live.refreshPairs}
            />
          ) : live.pairs.state === "ready" && live.pairs.data.length === 0 ? (
            // The backend publishes configured pairs before any market data
            // arrives, so an empty roster means it is still starting rather
            // than that nothing is tracked. Offer a way out of that state.
            <Placeholder
              state="empty"
              title="No pairs are being tracked yet"
              detail="The backend reported an empty roster. It normally lists configured pairs before the first market data arrives."
              onRetry={live.refreshPairs}
            />
          ) : (
            <LiveSpreads
              pairs={live.pairs.state === "ready" ? live.pairs.data : []}
              books={live.books}
              statuses={live.bookStatuses}
              nowMs={live.nowMs}
              feedLive={live.feedLive}
            />
          )}
        </ErrorBoundary>

        <ErrorBoundary label="Opportunity feed">
          <OpportunityFeed
            opportunities={live.opportunities}
            onRetry={live.refreshOpportunities}
          />
        </ErrorBoundary>
      </div>

      <ErrorBoundary label="Venue comparison">
        <VenueComparison
          pairs={live.pairs.state === "ready" ? live.pairs.data : []}
          statuses={live.bookStatuses}
          pricing={live.depthPricing}
          opportunities={live.opportunities}
          nowMs={live.nowMs}
          onRetry={live.refreshDepthPricing}
        />
      </ErrorBoundary>
    </div>
  );
}
