import { useEffect, useState } from 'react';
import { fetchAdminUsage } from '../lib/api';
import type { UsageSummary } from '../types';

type GetToken = () => Promise<string | null>;

/** Admin-only usage panel over GET /api/admin/usage. Renders nothing at
 * all (not even an error) for a non-admin caller -- a 403/404/network
 * failure is indistinguishable from "this viewer isn't an admin", which
 * is the common case, not an error state worth surfacing. */
export function UsagePanel({ getToken }: { getToken?: GetToken }) {
  const [usage, setUsage] = useState<UsageSummary | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchAdminUsage(getToken).then((data) => {
      if (!cancelled) setUsage(data);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!usage || usage.providers.length === 0) return null;

  return (
    <section className="usage-panel">
      <h2 className="section-heading">Usage &amp; Cost (today)</h2>
      <table className="usage-table">
        <thead>
          <tr>
            <th>Provider</th>
            <th>Calls</th>
            <th>Failures</th>
            <th>Cache hit</th>
            <th>Avg latency</th>
            <th>Est. cost</th>
          </tr>
        </thead>
        <tbody>
          {usage.providers.map((p) => (
            <tr key={p.provider}>
              <td>{p.provider}</td>
              <td>{p.calls}</td>
              <td>{p.failures}</td>
              <td>{p.cache_hit_rate != null ? `${Math.round(p.cache_hit_rate * 100)}%` : '—'}</td>
              <td>{p.avg_latency_ms != null ? `${Math.round(p.avg_latency_ms)} ms` : '—'}</td>
              <td>{p.est_cost_usd != null ? `$${p.est_cost_usd.toFixed(4)}` : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="usage-panel-note">Costs are rough estimates, not billing data. Circuit breakers: {Object.keys(usage.circuit_breakers).length ? JSON.stringify(usage.circuit_breakers) : 'all closed'}.</p>
    </section>
  );
}
