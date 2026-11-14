import type { Briefing, Client, ClientBriefingsResponse, Holding, MyLimits, RunLimitInfo, RunLog, UsageSummary } from '../types';

// Production builds REQUIRE VITE_API_BASE_URL (vite.config.ts fails the build
// without it); the localhost fallback exists for `npm run dev` only, so a
// misconfigured deploy can never silently ship pointing at localhost.
const API_BASE =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? (import.meta.env.DEV ? 'http://localhost:8010' : '');

/** Thrown by runBriefing/createClient on a non-2xx response. `status` lets
 * callers distinguish 409 (already running) / 429 (rate limited) / other
 * from a generic failure and render a friendly message instead of the raw
 * error text. */
export class ApiError extends Error {
  status: number;
  retryAfter?: number;

  constructor(message: string, status: number, retryAfter?: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

function parseRunLimitHeaders(res: Response): RunLimitInfo | undefined {
  const remaining = res.headers.get('X-RateLimit-Remaining');
  const limit = res.headers.get('X-RateLimit-Limit');
  const reset = res.headers.get('X-RateLimit-Reset');
  if (remaining == null || limit == null || reset == null) return undefined;
  return { remaining: Number(remaining), limit: Number(limit), resetAt: Number(reset) };
}

async function friendlyErrorMessage(res: Response): Promise<string> {
  const detail = await res.json().catch(() => null);
  if (res.status === 409) {
    return detail?.detail ?? 'A briefing run is already in progress for this client.';
  }
  if (res.status === 429) {
    const retryAfter = Number(res.headers.get('Retry-After')) || undefined;
    if (detail?.error === 'daily AI capacity reached') {
      return 'Daily AI capacity reached for all clients — please try again tomorrow.';
    }
    if (detail?.error === 'run_limit_exceeded') {
      return retryAfter
        ? `You've reached the run limit for this hour. Try again in about ${Math.ceil(retryAfter / 60)} minute(s).`
        : "You've reached the run limit for this hour.";
    }
    return 'Too many requests — please slow down and try again shortly.';
  }
  return detail?.detail ?? detail?.error ?? `Request failed (${res.status})`;
}

async function authHeaders(getToken?: () => Promise<string | null>): Promise<HeadersInit> {
  if (!getToken) return {};
  try {
    const token = await getToken();
    return token ? { Authorization: `Bearer ${token}` } : {};
  } catch {
    return {};
  }
}

/** Loads the caller's clients. Throws ApiError on any failure (network
 * included) -- there is no bundled/mock fallback; the UI shows the error. */
export async function fetchClients(getToken?: () => Promise<string | null>): Promise<Client[]> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/clients`, { headers: await authHeaders(getToken) });
  } catch {
    throw new ApiError('Cannot reach the API server.', 0);
  }
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new ApiError(detail?.detail ?? `Could not load clients (${res.status})`, res.status);
  }
  return (await res.json()) as Client[];
}

export async function updateClient(
  clientId: string,
  input: { name: string; portfolio_themes: string[] },
  getToken?: () => Promise<string | null>
): Promise<Client> {
  const res = await fetch(`${API_BASE}/api/clients/${clientId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...(await authHeaders(getToken)) },
    body: JSON.stringify(input)
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `Failed to update client (${res.status})`);
  }
  return (await res.json()) as Client;
}

/** Permanently deletes the client and all its briefings/runs/holdings. */
export async function deleteClient(clientId: string, getToken?: () => Promise<string | null>): Promise<void> {
  const res = await fetch(`${API_BASE}/api/clients/${clientId}`, {
    method: 'DELETE',
    headers: await authHeaders(getToken)
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `Failed to delete client (${res.status})`);
  }
}

export async function createClient(
  input: { name: string; portfolio_themes: string[] },
  getToken?: () => Promise<string | null>
): Promise<Client> {
  const res = await fetch(`${API_BASE}/api/clients`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(await authHeaders(getToken)) },
    body: JSON.stringify(input)
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `Failed to create client (${res.status})`);
  }
  return (await res.json()) as Client;
}

/** Starts a background run and returns its run_id immediately (202) --
 * does NOT wait for the briefing itself. Callers watch progress via
 * `runEventsUrl(runId)` (SSE) or poll `fetchRun(runId)`, then reload the
 * client's briefings once the run's terminal event/status arrives. */
export async function startRun(
  clientId: string,
  getToken?: () => Promise<string | null>
): Promise<{ runId: string; runLimit?: RunLimitInfo }> {
  const res = await fetch(`${API_BASE}/api/clients/${clientId}/run`, {
    method: 'POST',
    headers: await authHeaders(getToken)
  });
  if (!res.ok) {
    const retryAfter = Number(res.headers.get('Retry-After')) || undefined;
    throw new ApiError(await friendlyErrorMessage(res), res.status, retryAfter);
  }
  const body = (await res.json()) as { run_id: string };
  return { runId: body.run_id, runLimit: parseRunLimitHeaders(res) };
}

/** Full URL for the SSE progress stream of one run -- EventSource doesn't
 * support custom headers, so auth has to travel as a query param here
 * rather than an Authorization header (see RunProgress.tsx). */
export function runEventsUrl(runId: string, token?: string | null): string {
  const url = new URL(`${API_BASE}/api/runs/${runId}/events`);
  if (token) url.searchParams.set('token', token);
  return url.toString();
}

export async function fetchRun(runId: string, getToken?: () => Promise<string | null>): Promise<RunLog | null> {
  try {
    const res = await fetch(`${API_BASE}/api/runs/${runId}`, { headers: await authHeaders(getToken) });
    if (!res.ok) return null;
    return (await res.json()) as RunLog;
  } catch {
    return null;
  }
}

export async function fetchRuns(
  clientId: string | undefined,
  getToken?: () => Promise<string | null>
): Promise<RunLog[]> {
  try {
    const qs = clientId ? `?client_id=${encodeURIComponent(clientId)}` : '';
    const res = await fetch(`${API_BASE}/api/runs${qs}`, { headers: await authHeaders(getToken) });
    if (!res.ok) return [];
    return (await res.json()) as RunLog[];
  } catch {
    return [];
  }
}

/** Dependency-free liveness probe (GET /healthz) -- used here only to
 * read the server's CONFIGURED demo-mode setting for a persistent banner
 * when it's forced on ("true"); per-briefing demo badges (Briefing.isDemo)
 * are what actually reflect whether a given briefing was demo-served,
 * including the "auto" quota-exhausted fallback case this can't predict. */
export async function fetchServerDemoMode(): Promise<'true' | 'false' | 'auto' | null> {
  try {
    const res = await fetch(`${API_BASE}/healthz`);
    if (!res.ok) return null;
    const data = await res.json();
    return data.demoMode ?? null;
  } catch {
    return null;
  }
}

export async function fetchMyLimits(getToken?: () => Promise<string | null>): Promise<MyLimits | null> {
  try {
    const res = await fetch(`${API_BASE}/api/me/limits`, { headers: await authHeaders(getToken) });
    if (!res.ok) return null;
    const data = await res.json();
    return {
      run: { limit: data.run.limit, remaining: data.run.remaining, resetAt: data.run.reset_at },
      dailyBudget: {
        limit: data.daily_llm_budget.limit,
        remaining: data.daily_llm_budget.remaining,
        resetAt: data.daily_llm_budget.reset_at
      }
    };
  } catch {
    return null;
  }
}

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new ApiError(detail?.detail ?? detail?.error ?? `Request failed (${res.status})`, res.status);
  }
  return (await res.json()) as T;
}

/** Owner-only approval workflow (Phase 7). `overrideReason` is required by
 * the backend only when the briefing still has unresolved violations --
 * omitting it on a clean briefing is fine. */
export async function approveBriefing(
  briefingId: string,
  overrideReason: string | undefined,
  getToken?: () => Promise<string | null>
): Promise<Briefing> {
  const res = await fetch(`${API_BASE}/api/briefings/${briefingId}/approve`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...(await authHeaders(getToken)) },
    body: JSON.stringify({ override_reason: overrideReason })
  });
  return jsonOrThrow<Briefing>(res);
}

export async function rejectBriefing(
  briefingId: string,
  reason: string | undefined,
  getToken?: () => Promise<string | null>
): Promise<Briefing> {
  const res = await fetch(`${API_BASE}/api/briefings/${briefingId}/reject`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...(await authHeaders(getToken)) },
    body: JSON.stringify({ reason })
  });
  return jsonOrThrow<Briefing>(res);
}

/** The review queue's "edit then re-check" action: re-runs the FULL
 * compliance engine on the edited text server-side. */
export async function editBriefing(
  briefingId: string,
  content: string,
  getToken?: () => Promise<string | null>
): Promise<Briefing> {
  const res = await fetch(`${API_BASE}/api/briefings/${briefingId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...(await authHeaders(getToken)) },
    body: JSON.stringify({ content })
  });
  return jsonOrThrow<Briefing>(res);
}

export async function fetchReviewQueue(getToken?: () => Promise<string | null>): Promise<Briefing[]> {
  try {
    const res = await fetch(`${API_BASE}/api/review-queue`, { headers: await authHeaders(getToken) });
    if (!res.ok) return [];
    return (await res.json()) as Briefing[];
  } catch {
    return [];
  }
}

/** Downloads one briefing's PDF and triggers a browser save -- fetched as
 * a blob (not a plain <a href> link) so the Authorization header travels
 * with the request. */
export async function downloadBriefingPdf(
  briefingId: string,
  getToken?: () => Promise<string | null>
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/briefings/${briefingId}/pdf`, { headers: await authHeaders(getToken) });
  if (!res.ok) throw new ApiError(`Failed to generate PDF (${res.status})`, res.status);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `briefing-${briefingId}.pdf`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export async function fetchHoldings(clientId: string, getToken?: () => Promise<string | null>): Promise<Holding[]> {
  try {
    const res = await fetch(`${API_BASE}/api/clients/${clientId}/holdings`, { headers: await authHeaders(getToken) });
    if (!res.ok) return [];
    return (await res.json()) as Holding[];
  } catch {
    return [];
  }
}

export async function uploadHoldings(
  clientId: string,
  file: File,
  getToken?: () => Promise<string | null>
): Promise<{ count: number }> {
  const formData = new FormData();
  formData.append('file', file);
  const res = await fetch(`${API_BASE}/api/clients/${clientId}/holdings`, {
    method: 'POST',
    headers: await authHeaders(getToken),
    body: formData
  });
  return jsonOrThrow<{ count: number }>(res);
}

/** Admin-only (ADMIN_USER_IDS); callers should treat a non-200 response
 * (403 for a non-admin caller, in particular) as "hide the usage panel",
 * not as an error to surface. */
export async function fetchAdminUsage(getToken?: () => Promise<string | null>): Promise<UsageSummary | null> {
  try {
    const res = await fetch(`${API_BASE}/api/admin/usage`, { headers: await authHeaders(getToken) });
    if (!res.ok) return null;
    return (await res.json()) as UsageSummary;
  } catch {
    return null;
  }
}

/** Loads one client's briefings (auth token required). Throws ApiError on any
 * failure; there is no mock-data fallback. */
export async function fetchClientBriefings(
  clientId: string,
  getToken?: () => Promise<string | null>
): Promise<ClientBriefingsResponse> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/clients/${clientId}/briefings`, { headers: await authHeaders(getToken) });
  } catch {
    throw new ApiError('Cannot reach the API server.', 0);
  }
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new ApiError(detail?.detail ?? `Could not load briefings (${res.status})`, res.status);
  }
  return (await res.json()) as ClientBriefingsResponse;
}
