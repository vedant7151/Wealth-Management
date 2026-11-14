import { useState, type FormEvent } from 'react';
import { AnimatePresence, motion } from 'motion/react';

interface AddClientFormProps {
  onCreate: (input: { name: string; portfolio_themes: string[] }) => Promise<void>;
}

export function AddClientForm({ onCreate }: AddClientFormProps) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [themes, setThemes] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    const parsedThemes = themes
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean);

    if (!name.trim() || parsedThemes.length === 0) {
      setError('Enter a client name and at least one portfolio theme.');
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      await onCreate({ name: name.trim(), portfolio_themes: parsedThemes });
      setName('');
      setThemes('');
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create client.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="add-client">
      <button type="button" className="add-client-toggle" onClick={() => setOpen((v) => !v)}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" />
        </svg>
        Add client
      </button>

      <AnimatePresence initial={false}>
        {open && (
          <motion.form
            className="add-client-form"
            onSubmit={handleSubmit}
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.25, ease: 'easeOut' }}
          >
            <div className="add-client-fields">
              <input
                type="text"
                placeholder="Client name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="add-client-input"
              />
              <input
                type="text"
                placeholder="Portfolio themes, comma separated"
                value={themes}
                onChange={(e) => setThemes(e.target.value)}
                className="add-client-input"
              />
              <button type="submit" className="add-client-submit" disabled={submitting}>
                {submitting ? 'Creating…' : 'Create'}
              </button>
            </div>
            {error && <p className="add-client-error">{error}</p>}
          </motion.form>
        )}
      </AnimatePresence>
    </div>
  );
}
