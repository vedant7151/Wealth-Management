import { useEffect, useState } from 'react';
import { motion } from 'motion/react';
import type { RunLimitInfo } from '../types';

interface RunBriefingButtonProps {
  onRun: () => Promise<void>;
  runLimit?: RunLimitInfo | null;
}

function formatCountdown(resetAt: number): string {
  const remainingMs = resetAt * 1000 - Date.now();
  if (remainingMs <= 0) return 'now';
  const totalSeconds = Math.ceil(remainingMs / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
}

export function RunBriefingButton({ onRun, runLimit }: RunBriefingButtonProps) {
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [, setTick] = useState(0);

  const exhausted = runLimit != null && runLimit.remaining <= 0;

  useEffect(() => {
    if (!exhausted) return;
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, [exhausted]);

  const handleClick = async () => {
    setRunning(true);
    setError(null);
    try {
      await onRun();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to generate briefing.');
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="run-briefing">
      <button
        type="button"
        className="run-briefing-button"
        onClick={handleClick}
        disabled={running || exhausted}
      >
        {running ? (
          <motion.svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            aria-hidden="true"
            animate={{ rotate: 360 }}
            transition={{ duration: 0.8, repeat: Infinity, ease: 'linear' }}
          >
            <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2.5" strokeOpacity="0.25" />
            <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" />
          </motion.svg>
        ) : (
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path
              d="M5 3v4M3 5h4M6 17v4M4 19h4M13 3l2.5 6.5L22 12l-6.5 2.5L13 21l-2.5-6.5L4 12l6.5-2.5L13 3Z"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinejoin="round"
            />
          </svg>
        )}
        {running ? 'Generating briefing…' : exhausted ? 'Run limit reached' : 'Run morning briefing'}
      </button>
      {running && <p className="run-briefing-hint">This calls live research + LLM APIs, usually 20-90s.</p>}
      {!running && runLimit && !exhausted && (
        <p className="run-briefing-hint">
          {runLimit.remaining} of {runLimit.limit} runs left this hour
        </p>
      )}
      {!running && exhausted && runLimit && (
        <p className="run-briefing-hint">Run limit exhausted — resets in {formatCountdown(runLimit.resetAt)}.</p>
      )}
      {error && <p className="add-client-error">{error}</p>}
    </div>
  );
}
