import { useState, type FormEvent } from 'react';
import { deleteClient, updateClient } from '../lib/api';
import type { Client } from '../types';

interface ClientActionsProps {
  client: Client;
  getToken?: () => Promise<string | null>;
  onUpdated: (client: Client) => void | Promise<void>;
  onDeleted: () => void | Promise<void>;
}

/** Edit (name + themes) and delete for the selected client. Delete is a
 * two-step confirm because it permanently removes the client's briefings,
 * run history and holdings. */
export function ClientActions({ client, getToken, onUpdated, onDeleted }: ClientActionsProps) {
  const [mode, setMode] = useState<'idle' | 'edit' | 'confirm-delete'>('idle');
  const [name, setName] = useState(client.name);
  const [themes, setThemes] = useState(client.portfolio_themes.join(', '));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const startEdit = () => {
    setName(client.name);
    setThemes(client.portfolio_themes.join(', '));
    setError(null);
    setMode('edit');
  };

  const handleSave = async (event: FormEvent) => {
    event.preventDefault();
    const parsedThemes = themes
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean);
    if (!name.trim() || parsedThemes.length === 0) {
      setError('Enter a client name and at least one portfolio theme.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const updated = await updateClient(client.id, { name: name.trim(), portfolio_themes: parsedThemes }, getToken);
      await onUpdated(updated);
      setMode('idle');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update client.');
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async () => {
    setBusy(true);
    setError(null);
    try {
      await deleteClient(client.id, getToken);
      await onDeleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete client.');
      setMode('idle');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="client-actions">
      {mode === 'idle' && (
        <div className="client-actions-row">
          <button type="button" className="client-actions-button" onClick={startEdit}>
            Edit client
          </button>
          <button
            type="button"
            className="client-actions-button client-actions-danger"
            onClick={() => {
              setError(null);
              setMode('confirm-delete');
            }}
          >
            Delete client
          </button>
        </div>
      )}

      {mode === 'edit' && (
        <form className="add-client-form" onSubmit={handleSave}>
          <div className="add-client-fields">
            <input
              type="text"
              aria-label="Client name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="add-client-input"
            />
            <input
              type="text"
              aria-label="Portfolio themes, comma separated"
              value={themes}
              onChange={(e) => setThemes(e.target.value)}
              className="add-client-input"
            />
            <button type="submit" className="add-client-submit" disabled={busy}>
              {busy ? 'Saving…' : 'Save'}
            </button>
            <button type="button" className="client-actions-button" onClick={() => setMode('idle')} disabled={busy}>
              Cancel
            </button>
          </div>
        </form>
      )}

      {mode === 'confirm-delete' && (
        <div className="client-actions-confirm" role="alertdialog" aria-label="Confirm delete">
          <span>
            Delete <strong>{client.name}</strong> and all of its briefings, run history and holdings? This cannot be
            undone.
          </span>
          <div className="client-actions-row">
            <button
              type="button"
              className="client-actions-button client-actions-danger"
              onClick={handleDelete}
              disabled={busy}
            >
              {busy ? 'Deleting…' : 'Yes, delete'}
            </button>
            <button type="button" className="client-actions-button" onClick={() => setMode('idle')} disabled={busy}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {error && (
        <p className="add-client-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
