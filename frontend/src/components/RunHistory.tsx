import { useEffect, useState } from 'react';
import { fetchRuns } from '../lib/api';
import type { RunLog } from '../types';

const STATUS_CLASS: Record<string, string> = {
  running: 'compliance-badge-neutral',
  success: 'compliance-badge-good',
  failed: 'compliance-badge-bad'
};

/** Human-readable meaning of the machine `errorCode` a failed run carries
 * (backend/runs.py, scheduler.py). Unknown codes fall back to a generic
 * message -- the raw code is still shown so it can be quoted to an operator. */
const FAILURE_EXPLANATIONS: Record<string, string> = {
  writer_failed: 'The draft could not be generated, so no briefing was published.',
  no_briefing_saved: 'The run finished but saved no briefing.',
  timeout_recovered: 'The run was interrupted (server restart or timeout) and was marked failed on recovery.',
  TimeoutError: 'The run took too long and was stopped.',
  demo_run_failed: 'The demo briefing could not be saved.'
};

function explainFailure(code: string | null): string {
  return (code && FAILURE_EXPLANATIONS[code]) || 'The run failed before producing a briefing.';
}

function formatDuration(ms: number | null): string {
  if (ms == null) return '—';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

interface RunHistoryProps {
  clientId: string;
  getToken?: () => Promise<string | null>;
  onSelectBriefing?: (briefingId: string) => void;
  refreshToken?: number;
  /** Dead-letter retry: starts a fresh run for this client. It goes through
   * the same admission checks (rate limit, lock, budget) as the main
   * "Run briefing" button, so a rejection surfaces here as an error. */
  onRetry?: () => Promise<void>;
}

/** Run history: status, duration, trigger, and a link to the resulting
 * briefing, newest first -- GET /api/runs?client_id=... */
export function RunHistory({ clientId, getToken, onSelectBriefing, refreshToken, onRetry }: RunHistoryProps) {
  const [runLogs, setRunLogs] = useState<RunLog[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);

  const handleRetry = async () => {
    if (!onRetry) return;
    setRetrying(true);
    setRetryError(null);
    try {
      await onRetry();
    } catch (err) {
      setRetryError(err instanceof Error ? err.message : 'Retry failed.');
    } finally {
      setRetrying(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const rows = await fetchRuns(clientId, getToken);
      if (!cancelled) {
        setRunLogs(rows);
        setLoaded(true);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clientId, refreshToken]);

  if (!loaded) return null;
  if (runLogs.length === 0) {
    return <p className="state-message">No runs yet for this client.</p>;
  }

  return (
    <div className="run-history">
      <h3 className="run-history-heading">Run history</h3>
      <ul className="run-history-list">
        {runLogs.map((r, i) => (
          <li key={r.id} className="run-history-item">
            <span className={`compliance-badge run-history-status ${STATUS_CLASS[r.status]}`}>{r.status}</span>
            <span className="run-history-trigger">{r.trigger}</span>
            <span className="run-history-time">{new Date(r.startedAt).toLocaleString()}</span>
            <span className="run-history-duration">{formatDuration(r.durationMs)}</span>
            {r.briefingId ? (
              <button type="button" className="run-history-link" onClick={() => onSelectBriefing?.(r.briefingId!)}>
                View briefing
              </button>
            ) : (
              <span className="run-history-link run-history-link-disabled">
                {r.errorCode ?? (r.status === 'running' ? 'In progress' : 'No briefing')}
              </span>
            )}
            {r.status === 'failed' && (
              <div className="run-history-failure" role="alert">
                <span>
                  {explainFailure(r.errorCode)}
                  {r.errorCode ? <code className="run-history-failure-code"> {r.errorCode}</code> : null}
                </span>
                {onRetry && i === 0 && (
                  <button type="button" className="run-history-retry" onClick={handleRetry} disabled={retrying}>
                    {retrying ? 'Retrying…' : 'Retry run'}
                  </button>
                )}
              </div>
            )}
          </li>
        ))}
      </ul>
      {retryError && (
        <p className="state-message" role="alert">
          {retryError}
        </p>
      )}
    </div>
  );
}
