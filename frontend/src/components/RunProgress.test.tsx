import { act, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { RunProgress } from './RunProgress';

type Listener = (ev: { data: string; lastEventId?: string }) => void;

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: Listener | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  close() {
    this.closed = true;
  }

  emit(event: Record<string, unknown>, lastEventId?: string) {
    this.onmessage?.({ data: JSON.stringify(event), lastEventId });
  }
}

describe('RunProgress', () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    // @ts-expect-error -- test double, not a real EventSource
    globalThis.EventSource = FakeEventSource;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders node events as they arrive over the fake EventSource', async () => {
    const onDone = vi.fn();
    render(<RunProgress runId="run-1" onDone={onDone} />);

    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    const es = FakeEventSource.instances[0];
    expect(es.url).toContain('/api/runs/run-1/events');

    act(() => {
      es.onopen?.();
      es.emit({ seq: 1, type: 'node', node: 'supervisor', reason: 'no research yet', ts: 'now' }, '1');
      es.emit({ seq: 2, type: 'node', node: 'researcher', reason: null, ts: 'now' }, '2');
    });

    expect(screen.getByText('Supervisor')).toBeInTheDocument();
    expect(screen.getByText('no research yet')).toBeInTheDocument();
    expect(screen.getByText('Researcher')).toBeInTheDocument();
    expect(screen.getByText('Watching run live')).toBeInTheDocument();
  });

  it('calls onDone with the final status and briefing id when a done event arrives', async () => {
    const onDone = vi.fn();
    render(<RunProgress runId="run-2" onDone={onDone} />);

    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    const es = FakeEventSource.instances[0];

    act(() => {
      es.emit({ seq: 1, type: 'done', status: 'success', briefing_id: 'b-42', duration_ms: 1200, ts: 'now' });
    });

    expect(onDone).toHaveBeenCalledWith({ status: 'success', briefingId: 'b-42' });
    expect(es.closed).toBe(true);
  });

  it('shows a waiting message before any node event has arrived', async () => {
    render(<RunProgress runId="run-3" onDone={vi.fn()} />);
    expect(screen.getByText('Waiting for the run to start…')).toBeInTheDocument();
  });
});
