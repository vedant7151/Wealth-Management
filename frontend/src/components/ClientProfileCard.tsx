import { motion } from 'motion/react';
import type { Client } from '../types';

function getInitials(name: string): string {
  return name
    .split(' ')
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join('');
}

interface ClientProfileCardProps {
  client: Client;
  briefingCount: number;
  lastUpdated: string | null;
}

export function ClientProfileCard({ client, briefingCount, lastUpdated }: ClientProfileCardProps) {
  return (
    <motion.section
      className="client-card"
      layout
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, ease: 'easeOut' }}
      aria-label="Client profile"
    >
      <div className="client-card-top">
        <motion.div
          className="client-avatar"
          initial={{ scale: 0.8, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ duration: 0.35, ease: 'easeOut', delay: 0.05 }}
          aria-hidden="true"
        >
          {getInitials(client.name)}
        </motion.div>
        <div>
          <p className="client-label">Client Profile</p>
          <h2 className="client-name">{client.name}</h2>
        </div>
      </div>

      <div className="client-stats">
        <div className="client-stat">
          <span className="client-stat-value">{briefingCount}</span>
          <span className="client-stat-label">Briefings on file</span>
        </div>
        <div className="client-stat">
          <span className="client-stat-value">{client.portfolio_themes.length}</span>
          <span className="client-stat-label">Portfolio themes</span>
        </div>
        <div className="client-stat">
          <span className="client-stat-value client-stat-mono">{lastUpdated ?? '—'}</span>
          <span className="client-stat-label">Last briefing</span>
        </div>
      </div>

      <div className="client-themes" aria-label="Portfolio themes">
        {client.portfolio_themes.map((theme, i) => (
          <motion.span
            key={theme}
            className="theme-pill"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.3, delay: 0.1 + i * 0.05, ease: 'easeOut' }}
          >
            {theme}
          </motion.span>
        ))}
      </div>
    </motion.section>
  );
}
