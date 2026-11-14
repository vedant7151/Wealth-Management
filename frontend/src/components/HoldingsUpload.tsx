import { useEffect, useRef, useState } from 'react';
import { fetchHoldings, uploadHoldings } from '../lib/api';
import type { Holding } from '../types';

type GetToken = () => Promise<string | null>;

interface HoldingsUploadProps {
  clientId: string;
  getToken?: GetToken;
}

/** CSV holdings upload (replace-on-upload) + a table of the client's
 * current holdings. researcher_node uses these (when present) to build
 * ticker/sector-aware search queries instead of only the broader
 * portfolio themes. */
export function HoldingsUpload({ clientId, getToken }: HoldingsUploadProps) {
  const [holdings, setHoldings] = useState<Holding[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const reload = async () => {
    const rows = await fetchHoldings(clientId, getToken);
    setHoldings(rows);
  };

  useEffect(() => {
    let cancelled = false;
    fetchHoldings(clientId, getToken).then((rows) => {
      if (!cancelled) setHoldings(rows);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clientId]);

  const handleFile = async (file: File) => {
    setBusy(true);
    setError(null);
    try {
      await uploadHoldings(clientId, file, getToken);
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to upload holdings CSV.');
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = '';
    }
  };

  return (
    <section className="holdings-upload">
      <div className="holdings-upload-header">
        <h3>Holdings</h3>
        <label className="holdings-upload-button">
          {busy ? 'Uploading…' : 'Upload CSV'}
          <input
            ref={inputRef}
            type="file"
            accept=".csv,text/csv"
            disabled={busy}
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void handleFile(file);
            }}
            style={{ display: 'none' }}
          />
        </label>
      </div>

      {error && <p className="holdings-upload-error">{error}</p>}

      {holdings.length > 0 ? (
        <table className="holdings-table">
          <thead>
            <tr>
              <th>Ticker</th>
              <th>Sector</th>
              <th>Weight</th>
            </tr>
          </thead>
          <tbody>
            {holdings.map((h) => (
              <tr key={h.id}>
                <td>{h.ticker}</td>
                <td>{h.sector ?? '—'}</td>
                <td>{h.weight}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="holdings-upload-empty">
          No holdings uploaded yet — research queries use portfolio themes only.
        </p>
      )}
    </section>
  );
}
