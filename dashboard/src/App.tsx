import { lazy, Suspense } from "react";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { TopBar } from "./components/TopBar";
import Dashboard from "./pages/Dashboard";
import { LiveProvider, useLive } from "./state/live";

const Statistics = lazy(() => import("./pages/Statistics"));

function Chrome() {
  const { status, lastTickAgeMs, invalidFrameCount } = useLive();
  return (
    <TopBar
      status={status}
      lastTickAgeMs={lastTickAgeMs}
      invalidFrameCount={invalidFrameCount}
    />
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <LiveProvider>
        <div className="min-h-screen bg-ground text-ink">
          <a
            href="#content"
            className="sr-only focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-50 focus:rounded focus:border focus:border-line focus:bg-raised focus:px-3 focus:py-1.5 focus:text-xs focus:text-ink"
          >
            Skip to content
          </a>
          <Chrome />
          <main id="content" className="mx-auto max-w-[1600px] px-4 py-4">
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route
                path="/stats"
                element={
                  <Suspense
                    fallback={<p className="px-1 py-6 text-xs text-ink-3">Loading statistics.</p>}
                  >
                    <Statistics />
                  </Suspense>
                }
              />
            </Routes>
          </main>
        </div>
      </LiveProvider>
    </BrowserRouter>
  );
}
