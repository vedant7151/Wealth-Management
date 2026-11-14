import { Component, type ErrorInfo, type ReactNode } from 'react';

interface State {
  failed: boolean;
}

/** Last-resort boundary: a render error shows a message and a reload button
 * instead of a blank white page. Error details are logged to the console only
 * (never shown to the user). */
export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('UI error', error, info.componentStack);
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="signed-out-gate" role="alert">
        <h1>Something went wrong</h1>
        <p>The page hit an unexpected error. Reloading usually fixes it.</p>
        <button type="button" className="run-briefing-button" onClick={() => window.location.reload()}>
          Reload
        </button>
      </div>
    );
  }
}
