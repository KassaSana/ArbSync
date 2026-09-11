/**
 * Three states, never two. An empty result and a failed request look nothing
 * alike, because telling a user "no data" when the backend is unreachable is
 * the fastest way to lose their trust in a monitor.
 */
type Props = {
  state: "loading" | "empty" | "failed";
  title: string;
  detail?: string;
  onRetry?: () => void;
};

export function Placeholder({ state, title, detail, onRetry }: Props) {
  const failed = state === "failed";
  // An empty result is usually final and needs no button. It is offered one only
  // where the caller knows emptiness is recoverable and passes a retry.
  const retryable = (failed || state === "empty") && onRetry !== undefined;
  return (
    <div
      className={`flex flex-col items-start gap-2 rounded border border-dashed px-4 py-6 ${
        failed ? "border-crit/50 bg-crit/[0.04]" : "border-line bg-panel"
      }`}
    >
      <p className={`text-sm ${failed ? "text-crit" : "text-ink-2"}`}>
        {state === "loading" ? `${title}\u2026` : title}
      </p>
      {detail !== undefined ? <p className="text-micro text-ink-3">{detail}</p> : null}
      {retryable ? (
        <button
          type="button"
          onClick={onRetry}
          className="mt-1 rounded border border-line px-2.5 py-1 text-micro text-ink-2 transition-colors hover:border-ink-3 hover:text-ink"
        >
          Try again
        </button>
      ) : null}
    </div>
  );
}
