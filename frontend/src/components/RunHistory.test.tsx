import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { RunLog } from '../types';

vi.mock('../lib/api', () => ({ fetchRuns: vi.fn() }));

import { fetchRuns } from '../lib/api';
import { RunHistory } from './RunHistory';

function makeRun(overrides: Partial<RunLog> = {}): RunLog {
  return {
    id: 'r1',
    clientId: 'c1',
    trigger: 'manual',
    status: 'success',
    startedAt: new Date().toISOString(),
    finishedAt: null,
    durationMs: 1200,
    errorCode: null,
    briefingId: 'b1',
    ...overrides
  };
}

function failedRun(overrides: Partial<RunLog> = {}): RunLog {
  return makeRun({ status: 'failed', briefingId: null, errorCode: 'writer_failed', ...overrides });
}

describe('RunHistory dead-letter state', () => {
  beforeEach(() => {
    vi.mocked(fetchRuns).mockReset();
  });

  it('shows a visible failure explanation with the raw error code for a failed run', async () => {
    vi.mocked(fetchRuns).mockResolvedValue([failedRun({ id: 'r2', errorCode: 'timeout_recovered' })]);
    render(<RunHistory clientId="c1" />);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/interrupted/i);
    expect(alert).toHaveTextContent('timeout_recovered');
  });

  it('does not show a failure panel for successful runs', async () => {
    vi.mocked(fetchRuns).mockResolvedValue([makeRun()]);
    render(<RunHistory clientId="c1" />);
    await screen.findByText('Run history');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('falls back to a generic message for an unknown error code', async () => {
    vi.mocked(fetchRuns).mockResolvedValue([failedRun({ errorCode: 'WeirdError' })]);
    render(<RunHistory clientId="c1" />);
    expect(await screen.findByRole('alert')).toHaveTextContent(/failed before producing a briefing/i);
  });

  it('retry calls onRetry and surfaces a rejection (e.g. rate limited)', async () => {
    vi.mocked(fetchRuns).mockResolvedValue([failedRun()]);
    const onRetry = vi.fn().mockRejectedValue(new Error('Run limit reached'));
    render(<RunHistory clientId="c1" onRetry={onRetry} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Retry run' }));
    await waitFor(() => expect(onRetry).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('Run limit reached')).toBeInTheDocument();
  });

  it('offers retry only on the most recent run', async () => {
    vi.mocked(fetchRuns).mockResolvedValue([failedRun({ id: 'new' }), failedRun({ id: 'old' })]);
    render(<RunHistory clientId="c1" onRetry={vi.fn()} />);
    await screen.findAllByRole('alert');
    expect(screen.getAllByRole('button', { name: 'Retry run' })).toHaveLength(1);
  });
});
