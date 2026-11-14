import { useState } from 'react';
import type { Briefing } from '../types';
import { approveBriefing, downloadBriefingPdf, editBriefing, rejectBriefing } from '../lib/api';

type GetToken = () => Promise<string | null>;

interface ApprovalActionsProps {
  briefing: Briefing;
  getToken?: GetToken;
  onUpdated: (updated: Briefing) => void;
}

/** Owner-only approve/reject + "edit then re-check" actions for one
 * briefing, and a PDF download button available regardless of status.
 * Approve/reject/edit are only meaningful while the briefing isn't
 * already finalized (approved/rejected) -- the backend also enforces
 * this, this is just UI affordance. */
export function ApprovalActions({ briefing, getToken, onUpdated }: ApprovalActionsProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(briefing.content);
  const [overrideReason, setOverrideReason] = useState('');
  const [pdfBusy, setPdfBusy] = useState(false);

  const finalized = briefing.status === 'approved' || briefing.status === 'rejected';
  const hasViolations = briefing.complianceViolations.length > 0;

  const run = async (action: () => Promise<Briefing>) => {
    setBusy(true);
    setError(null);
    try {
      const updated = await action();
      onUpdated(updated);
      setEditing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Action failed.');
    } finally {
      setBusy(false);
    }
  };

  const handleApprove = () => {
    if (hasViolations && !overrideReason.trim()) {
      setError('This briefing has unresolved violations — provide an override reason to approve anyway.');
      return;
    }
    void run(() => approveBriefing(briefing.id, overrideReason.trim() || undefined, getToken));
  };

  const handleReject = () => void run(() => rejectBriefing(briefing.id, undefined, getToken));

  const handleRecheck = () => void run(() => editBriefing(briefing.id, draft, getToken));

  const handlePdf = async () => {
    setPdfBusy(true);
    setError(null);
    try {
      await downloadBriefingPdf(briefing.id, getToken);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to download PDF.');
    } finally {
      setPdfBusy(false);
    }
  };

  return (
    <div className="approval-actions">
      <div className="approval-actions-row">
        {!finalized && (
          <>
            <button type="button" className="approval-btn approval-btn-approve" onClick={handleApprove} disabled={busy}>
              Approve
            </button>
            <button type="button" className="approval-btn approval-btn-reject" onClick={handleReject} disabled={busy}>
              Reject
            </button>
            <button
              type="button"
              className="approval-btn approval-btn-edit"
              onClick={() => setEditing((v) => !v)}
              disabled={busy}
            >
              {editing ? 'Cancel edit' : 'Edit & re-check'}
            </button>
          </>
        )}
        <button type="button" className="approval-btn approval-btn-pdf" onClick={handlePdf} disabled={pdfBusy}>
          {pdfBusy ? 'Preparing PDF…' : 'Download PDF'}
        </button>
      </div>

      {!finalized && hasViolations && (
        <input
          type="text"
          className="approval-override-input"
          placeholder="Override reason (required to approve with open violations)"
          value={overrideReason}
          onChange={(e) => setOverrideReason(e.target.value)}
        />
      )}

      {editing && (
        <div className="approval-edit-panel">
          <textarea
            className="approval-edit-textarea"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={8}
          />
          <button type="button" className="approval-btn approval-btn-approve" onClick={handleRecheck} disabled={busy}>
            {busy ? 'Re-checking…' : 'Save & re-check compliance'}
          </button>
        </div>
      )}

      {briefing.reviewedAt && (
        <p className="approval-review-meta">
          {briefing.status === 'approved' ? 'Approved' : 'Reviewed'} {new Date(briefing.reviewedAt).toLocaleString()}
          {briefing.overrideReason && ` — override: "${briefing.overrideReason}"`}
        </p>
      )}

      {error && <p className="approval-error">{error}</p>}
    </div>
  );
}
