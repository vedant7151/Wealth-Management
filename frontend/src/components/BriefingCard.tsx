import { safeHttpUrl } from '../lib/safeUrl';
import { useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import type { Briefing } from '../types';
import { renderBriefingMarkdown } from '../lib/markdown';
import { ComplianceBadge } from './ComplianceBadge';
import { ApprovalActions } from './ApprovalActions';

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      dateStyle: 'medium',
      timeStyle: 'short'
    });
  } catch {
    return iso;
  }
}

function getDomain(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return url;
  }
}

// Fallback display name when a citation has no article title (e.g. older
// rows saved before Citation.title existed): derive something readable
// from the domain, e.g. "reuters.com" -> "Reuters".
function prettifySourceName(domain: string): string {
  const parts = domain.split('.');
  const core = parts.length > 2 ? parts[parts.length - 2] : parts[0];
  return core
    .split('-')
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

type GetToken = () => Promise<string | null>;

interface BriefingCardProps {
  briefing: Briefing;
  index: number;
  getToken?: GetToken;
  onBriefingUpdated?: (updated: Briefing) => void;
}

export function BriefingCard({ briefing, index, getToken, onBriefingUpdated }: BriefingCardProps) {
  const [sourcesOpen, setSourcesOpen] = useState(false);

  return (
    <motion.article
      id={`briefing-${briefing.id}`}
      className="briefing-card"
      initial={{ opacity: 0, y: 18 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, ease: 'easeOut', delay: Math.min(index * 0.06, 0.3) }}
    >
      <header className="briefing-card-header">
        <span className="badge badge-accent">Morning Briefing</span>
        {briefing.isDemo && (
          <span className="badge badge-demo" title="Served from a pre-recorded real run, not a live API call">
            Demo data
          </span>
        )}
        <time className="briefing-timestamp" dateTime={briefing.createdAt}>
          {formatDate(briefing.createdAt)}
        </time>
      </header>

      <ComplianceBadge briefing={briefing} />

      {onBriefingUpdated && (
        <ApprovalActions briefing={briefing} getToken={getToken} onUpdated={onBriefingUpdated} />
      )}

      {renderBriefingMarkdown(briefing.content)}

      <div className="briefing-sources">
        <button
          type="button"
          className="briefing-sources-toggle"
          onClick={() => setSourcesOpen((open) => !open)}
          aria-expanded={sourcesOpen}
        >
          <motion.svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            aria-hidden="true"
            animate={{ rotate: sourcesOpen ? 90 : 0 }}
            transition={{ duration: 0.2 }}
          >
            <path
              d="M9 6l6 6-6 6"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </motion.svg>
          {briefing.citations.length} source{briefing.citations.length === 1 ? '' : 's'} cited
        </button>

        <AnimatePresence initial={false}>
          {sourcesOpen && (
            <motion.ul
              className="briefing-sources-list"
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.25, ease: 'easeOut' }}
            >
              {briefing.citations.map((citation) => {
                const domain = getDomain(citation.url);
                const sourceName = citation.title?.trim() || prettifySourceName(domain);
                return (
                  <li key={citation.url}>
                    {safeHttpUrl(citation.url) ? (
                      <a href={safeHttpUrl(citation.url)!} target="_blank" rel="noopener noreferrer">
                        {sourceName}
                      </a>
                    ) : (
                      <span>{sourceName}</span>
                    )}
                    <span className="citation-domain"> — {domain}</span>
                    {citation.publishedDate && <span className="citation-date"> · {citation.publishedDate}</span>}
                  </li>
                );
              })}
            </motion.ul>
          )}
        </AnimatePresence>
      </div>
    </motion.article>
  );
}
