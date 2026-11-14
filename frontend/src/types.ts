export interface Citation {
  url: string;
  title: string | null;
  publishedDate: string | null;
}

export type BriefingStatus = 'draft' | 'compliant' | 'needs_human_review' | 'failed' | 'approved' | 'rejected';

export interface ComplianceViolation {
  rule: string;
  severity?: 'high' | 'medium' | 'low';
  quote: string;
  why: string;
  fix: string;
  span?: [number, number] | null;
}

export interface BriefingRevision {
  revisionIndex: number;
  content: string;
  violations: ComplianceViolation[];
  criticModel: string | null;
  isCompliant: boolean;
  createdAt: string;
}

export interface Briefing {
  id: string;
  content: string;
  createdAt: string;
  status: BriefingStatus;
  complianceViolations: ComplianceViolation[];
  revisionCount: number;
  citations: Citation[];
  revisions: BriefingRevision[];
  reviewedAt: string | null;
  reviewedBy: string | null;
  overrideReason: string | null;
  clientProfileId: string;
  /** Phase 8: true when this briefing was served from a pre-recorded
   * real-run fixture (demo mode, or the quota-exhausted fallback) instead
   * of a live graph run -- always render a visible badge when true. */
  isDemo: boolean;
}

export interface Holding {
  id: string;
  ticker: string;
  sector: string | null;
  weight: number;
}

export interface UsageProviderStats {
  provider: string;
  calls: number;
  failures: number;
  cache_hit_rate: number | null;
  avg_latency_ms: number | null;
  est_cost_usd: number | null;
}

export interface UsageSummary {
  window: string;
  providers: UsageProviderStats[];
  circuit_breakers: Record<string, unknown>;
}

export interface Client {
  id: string;
  name: string;
  portfolio_themes: string[];
}

export interface ClientBriefingsResponse {
  client: Client;
  briefings: Briefing[];
}

export interface RunLimitInfo {
  limit: number;
  remaining: number;
  resetAt: number; // epoch seconds
}

export interface MyLimits {
  run: RunLimitInfo;
  dailyBudget: RunLimitInfo;
}

export type RunTrigger = 'manual' | 'cron' | 'demo';
export type RunLogStatus = 'running' | 'success' | 'failed';

export interface RunLog {
  id: string;
  clientId: string;
  trigger: RunTrigger;
  status: RunLogStatus;
  startedAt: string;
  finishedAt: string | null;
  durationMs: number | null;
  errorCode: string | null;
  briefingId: string | null;
}

/** One live SSE event from GET /api/runs/{id}/events -- ids/counts/node
 * names/reasons/status only, never briefing text or prompts (see
 * backend/runs.py's module docstring). */
export interface RunEvent {
  seq: number;
  ts: string;
  type: 'node' | 'done';
  node?: string;
  reason?: string | null;
  stats?: Record<string, unknown>;
  status?: RunLogStatus;
  duration_ms?: number;
  briefing_id?: string | null;
  error_code?: string | null;
}
