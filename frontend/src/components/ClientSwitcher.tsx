import { motion } from 'motion/react';
import type { Client } from '../types';

interface ClientSwitcherProps {
  clients: Client[];
  selectedId: string;
  onSelect: (id: string) => void;
}

export function ClientSwitcher({ clients, selectedId, onSelect }: ClientSwitcherProps) {
  return (
    <div className="client-switcher" role="tablist" aria-label="Select client">
      {clients.map((client) => {
        const isActive = client.id === selectedId;
        return (
          <button
            key={client.id}
            type="button"
            role="tab"
            aria-selected={isActive}
            className={`client-switcher-item${isActive ? ' active' : ''}`}
            onClick={() => onSelect(client.id)}
          >
            {isActive && (
              <motion.span
                layoutId="client-switcher-highlight"
                className="client-switcher-highlight"
                transition={{ type: 'spring', stiffness: 400, damping: 32 }}
              />
            )}
            <span className="client-switcher-label">{client.name}</span>
          </button>
        );
      })}
    </div>
  );
}
