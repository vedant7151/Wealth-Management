import { useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import type { Briefing, BriefingStatus, ComplianceViolation } from '../types';

const STATUS_LABEL: Record<BriefingStatus, string> = {
  draft: 'Draft',
  compliant: 'Compliant',
  needs_human_review: 'Needs human review',
  failed: 'Generation failed',
  approved: 'Approved',
  rejected: 'Rejected'
};

const STATUS_CLASS: Record<BriefingStatus, string> = {
  draft: 'compliance-badge-neutral',
  compliant: 'compliance-badge-good',
  needs_human_review: 'compliance-badge-warn',
  failed: 'compliance-badge-bad',
  approved: 'compliance-badge-good',
  rejected: 'compliance-badge-bad'
};

function severityClass(severity?: string): string {
  if (severity === 'high') return 'violation-severity-high';
  if (severity === 'medium') return 'violation-severity-medium';
  return 'violation-severity-low';
}

function ViolationItem({ violation }: { violation: ComplianceViolation }) {
  return (
    <li className="violation-item">
      <div className="violation-item-header">
        <span className={`violation-severity ${severityClass(violation.severity)}`}>
          {violation.severity ?? 'medium'}
        </span>
        <span className="violation-rule">{violation.rule}</span>
      </div>
      {violation.quote && <p className="violation-quote">&ldquo;{violation.quote}&rdquo;</p>}
      <p className="violation-why">{violation.why}</p>
      {violation.fix && <p className="violation-fix">Fix: {violation.fix}</p>}
    </li>
  );
}

interface ComplianceBadgeProps {
  briefing: Briefing;
}

export function ComplianceBadge({ briefing }: ComplianceBadgeProps) {
  const [open, setOpen] = useState(false);
  const hasViolations = briefing.complianceViolations.length > 0;
  const hasRevisions = briefing.revisions.length > 0;
  const expandable = hasViolations || hasRevisions;

  return (
    <div className="compliance-section">
      <button
        type="button"
        className={`compliance-badge ${STATUS_CLASS[briefing.status]}`}
        onClick={() => expandable && setOpen((o) => !o)}
        aria-expanded={expandable ? open : undefined}
        disabled={!expandable}
      >
        {STATUS_LABEL[briefing.status]}
        {briefing.revisionCount > 0 && (
          <span className="compliance-badge-count"> · {briefing.revisionCount} revision{briefing.revisionCount === 1 ? '' : 's'}</span>
        )}
        {expandable && (
          <motion.svg
            width="12"
            height="12"
            viewBox="0 0 24 24"
            fill="none"
            aria-hidden="true"
            animate={{ rotate: open ? 90 : 0 }}
            transition={{ duration: 0.2 }}
          >
            <path d="M9 6l6 6-6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
          </motion.svg>
        )}
      </button>

      <AnimatePresence initial={false}>
        {open && expandable && (
          <motion.div
            className="compliance-details"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.25, ease: 'easeOut' }}
          >
            {hasViolations && (
              <div className="compliance-violations">
                <h4>Unresolved compliance issues</h4>
                <ul>
                  {briefing.complianceViolations.map((v, i) => (
                    <ViolationItem key={`${v.rule}-${i}`} violation={v} />
                  ))}
                </ul>
              </div>
            )}

            {hasRevisions && (
              <div className="compliance-revisions">
                <h4>Revision history</h4>
                <ul className="revision-history-list">
                  {briefing.revisions.map((rev) => (
                    <li key={rev.revisionIndex} className="revision-history-item">
                      <div className="revision-history-header">
                        <span>Revision {rev.revisionIndex}</span>
                        <span className={rev.isCompliant ? 'compliance-badge-good' : 'compliance-badge-warn'}>
                          {rev.isCompliant ? 'Compliant' : `${rev.violations.length} issue${rev.violations.length === 1 ? '' : 's'}`}
                        </span>
                        {rev.criticModel && <span className="revision-critic-model">{rev.criticModel}</span>}
                      </div>
                      {rev.violations.length > 0 && (
                        <ul>
                          {rev.violations.map((v, i) => (
                            <ViolationItem key={`${rev.revisionIndex}-${v.rule}-${i}`} violation={v} />
                          ))}
                        </ul>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
