import { Component, ErrorInfo, ReactNode } from "react";

type Props = {
  label: string;
  children: ReactNode;
};

type State = {
  error: Error | null;
};

/** Contains render failures to one panel; network payloads are validated separately. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error(`[${this.props.label}] render failed`, error, info.componentStack);
  }

  private readonly handleReset = (): void => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    const { error } = this.state;
    if (error === null) {
      return this.props.children;
    }
    return (
      <div className="rounded border border-crit/50 bg-crit/[0.04] px-4 py-6">
        <p className="text-sm text-crit">{this.props.label} stopped rendering</p>
        <p className="mt-1 text-micro text-ink-3">{error.message}</p>
        <button
          type="button"
          onClick={this.handleReset}
          className="mt-3 rounded border border-line px-2.5 py-1 text-micro text-ink-2 transition-colors hover:border-ink-3 hover:text-ink"
        >
          Retry this panel
        </button>
      </div>
    );
  }
}
