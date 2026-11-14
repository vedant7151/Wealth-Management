import { useEffect, useRef, useState } from 'react';
import { motion } from 'motion/react';
import { fetchRun, runEventsUrl } from '../lib/api';
import type { RunEvent, RunLogStatus } from '../types';

const NODE_LABEL: Record<string, string> = {
  run: 'Run started',
  supervisor: 'Supervisor',
  researcher: 'Researcher',
  retriever: 'Retriever',
  writer: 'Writer',
  critic: 'Compliance critic',
  save: 'Saved'
};

const MAX_RECONNECT_DELAY_MS = 10_000;

interface RunProgressProps {
  runId: string;
  getToken?: () => Promise<string | null>;
  onDone: (result: { status: RunLogStatus; briefingId: string | null }) => void;
}

/** Live timeline of a run's supervisor-pattern node transitions, driven by
 * `GET /api/runs/{id}/events` (SSE). Reconnects with backoff on a dropped
 * connection (resuming via `Last-Event-ID`), and falls back to polling
 * `GET /api/runs/{id}` if EventSource itself never manages to open. */
export function RunProgress({ runId, getToken, onDone }: RunProgressProps) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [connectionState, setConnectionState] = useState<'connecting' | 'live' | 'polling'>('connecting');
  const doneRef = useRef(false);
  const lastEventIdRef = useRef(0);

  useEffect(() => {
    let cancelled = false;
    let es: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let pollTimer: ReturnType<typeof setTimeout> | null = null;
    let reconnectAttempts = 0;

    const finish = (status: RunLogStatus, briefingId: string | null) => {
      if (doneRef.current) return;
      doneRef.current = true;
      onDone({ status, briefingId });
    };

    const startPollingFallback = () => {
      setConnectionState('polling');
      const poll = async () => {
        if (cancelled || doneRef.current) return;
        const run = await fetchRun(runId, getToken);
        if (run && (run.status === 'success' || run.status === 'failed')) {
          finish(run.status, run.briefingId);
          return;
        }
        pollTimer = setTimeout(poll, 4000);
      };
      poll();
    };

    const connect = async () => {
      if (cancelled) return;
      const token = getToken ? await getToken().catch(() => null) : null;
      const url = runEventsUrl(runId, token) + (lastEventIdRef.current ? `&last_event_id=${lastEventIdRef.current}` : '');

      try {
        es = new EventSource(url);
      } catch {
        startPollingFallback();
        return;
      }

      es.onopen = () => {
        reconnectAttempts = 0;
        setConnectionState('live');
      };

      es.onmessage = (ev) => {
        if (ev.lastEventId) lastEventIdRef.current = Number(ev.lastEventId) || lastEventIdRef.current;
        let parsed: RunEvent;
        try {
          parsed = JSON.parse(ev.data) as RunEvent;
        } catch {
          return;
        }
        setEvents((prev) => [...prev, parsed]);
        if (parsed.type === 'done') {
          es?.close();
          finish(parsed.status ?? 'failed', parsed.briefing_id ?? null);
        }
      };

      es.onerror = () => {
        es?.close();
        if (doneRef.current || cancelled) return;
        reconnectAttempts += 1;
        if (reconnectAttempts >= 5) {
          startPollingFallback();
          return;
        }
        const delay = Math.min(500 * 2 ** reconnectAttempts, MAX_RECONNECT_DELAY_MS);
        setConnectionState('connecting');
        reconnectTimer = setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      cancelled = true;
      es?.close();
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (pollTimer) clearTimeout(pollTimer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  const nodeEvents = events.filter((e) => e.type === 'node');

  return (
    <div className="run-progress">
      <div className="run-progress-header">
        <span className={`run-progress-dot run-progress-dot-${connectionState}`} aria-hidden="true" />
        <span className="run-progress-status-text">
          {connectionState === 'live' && 'Watching run live'}
          {connectionState === 'connecting' && 'Connecting…'}
          {connectionState === 'polling' && 'Live stream unavailable — polling for updates'}
        </span>
      </div>
      <ol className="run-progress-timeline">
        {nodeEvents.map((e) => (
          <motion.li
            key={e.seq}
            className="run-progress-step"
            initial={{ opacity: 0, x: -6 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: 0.2 }}
          >
            <span className="run-progress-step-node">{NODE_LABEL[e.node ?? ''] ?? e.node}</span>
            {e.reason && <span className="run-progress-step-reason">{e.reason}</span>}
          </motion.li>
        ))}
        {nodeEvents.length === 0 && <li className="run-progress-step run-progress-step-waiting">Waiting for the run to start…</li>}
      </ol>
    </div>
  );
}
