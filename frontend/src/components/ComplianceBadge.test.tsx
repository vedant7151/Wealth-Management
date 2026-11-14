import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { Briefing } from '../types';
import { ComplianceBadge } from './ComplianceBadge';

function makeBriefing(overrides: Partial<Briefing> = {}): Briefing {
  return {
    id: 'b1',
    content: 'draft content',
    createdAt: new Date().toISOString(),
    status: 'compliant',
    complianceViolations: [],
    revisionCount: 0,
    citations: [],
    revisions: [],
    reviewedAt: null,
    reviewedBy: null,
    overrideReason: null,
    clientProfileId: 'c1',
    isDemo: false,
    ...overrides,
  };
}

describe('ComplianceBadge', () => {
  it('renders the status label for a compliant briefing', () => {
    render(<ComplianceBadge briefing={makeBriefing({ status: 'compliant' })} />);
    expect(screen.getByRole('button', { name: /Compliant/ })).toBeInTheDocument();
  });

  it('renders the needs_human_review label', () => {
    render(<ComplianceBadge briefing={makeBriefing({ status: 'needs_human_review' })} />);
    expect(screen.getByRole('button', { name: /Needs human review/ })).toBeInTheDocument();
  });

  it('is not expandable when there are no violations or revisions', () => {
    render(<ComplianceBadge briefing={makeBriefing({ status: 'compliant' })} />);
    const button = screen.getByRole('button');
    expect(button).toBeDisabled();
  });

  it('expands to show violations when the badge is clicked', () => {
    const briefing = makeBriefing({
      status: 'needs_human_review',
      complianceViolations: [
        { rule: 'DIRECTIVE_ADVICE', severity: 'high', quote: 'buy now', why: 'directive advice', fix: 'rephrase' },
      ],
    });
    render(<ComplianceBadge briefing={briefing} />);

    const button = screen.getByRole('button');
    expect(button).not.toBeDisabled();
    expect(screen.queryByText(/Unresolved compliance issues/)).not.toBeInTheDocument();

    fireEvent.click(button);

    expect(screen.getByText(/Unresolved compliance issues/)).toBeInTheDocument();
    expect(screen.getByText('DIRECTIVE_ADVICE')).toBeInTheDocument();
    expect(screen.getByText(/buy now/)).toBeInTheDocument();
  });

  it('shows the revision count next to the badge label', () => {
    render(<ComplianceBadge briefing={makeBriefing({ status: 'compliant', revisionCount: 2 })} />);
    expect(screen.getByText(/2 revisions/)).toBeInTheDocument();
  });

  it('expands to show revision history', () => {
    const briefing = makeBriefing({
      status: 'compliant',
      revisionCount: 1,
      revisions: [
        {
          revisionIndex: 0,
          content: 'first draft',
          violations: [],
          criticModel: 'rules+gemini-3.6-flash',
          isCompliant: true,
          createdAt: new Date().toISOString(),
        },
      ],
    });
    render(<ComplianceBadge briefing={briefing} />);

    fireEvent.click(screen.getByRole('button'));

    expect(screen.getByText(/Revision history/)).toBeInTheDocument();
    expect(screen.getByText('Revision 0')).toBeInTheDocument();
    expect(screen.getByText('rules+gemini-3.6-flash')).toBeInTheDocument();
  });
});
