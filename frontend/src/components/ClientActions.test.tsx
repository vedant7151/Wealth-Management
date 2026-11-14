import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../lib/api', () => ({ updateClient: vi.fn(), deleteClient: vi.fn() }));

import { deleteClient, updateClient } from '../lib/api';
import { ClientActions } from './ClientActions';

const client = { id: 'c1', name: 'Old Name', portfolio_themes: ['AI', 'Energy'] };

describe('ClientActions', () => {
  beforeEach(() => {
    vi.mocked(updateClient).mockReset();
    vi.mocked(deleteClient).mockReset();
  });

  it('edits name and themes and reports the updated client', async () => {
    const updated = { id: 'c1', name: 'New Name', portfolio_themes: ['Rates'] };
    vi.mocked(updateClient).mockResolvedValue(updated);
    const onUpdated = vi.fn();
    render(<ClientActions client={client} onUpdated={onUpdated} onDeleted={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: 'Edit client' }));
    fireEvent.change(screen.getByLabelText('Client name'), { target: { value: 'New Name' } });
    fireEvent.change(screen.getByLabelText('Portfolio themes, comma separated'), { target: { value: 'Rates' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(onUpdated).toHaveBeenCalledWith(updated));
    expect(updateClient).toHaveBeenCalledWith('c1', { name: 'New Name', portfolio_themes: ['Rates'] }, undefined);
  });

  it('rejects an empty name or no themes without calling the API', async () => {
    render(<ClientActions client={client} onUpdated={vi.fn()} onDeleted={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit client' }));
    fireEvent.change(screen.getByLabelText('Client name'), { target: { value: '  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/at least one portfolio theme/i);
    expect(updateClient).not.toHaveBeenCalled();
  });

  it('delete needs a confirmation step and cancel does not delete', async () => {
    render(<ClientActions client={client} onUpdated={vi.fn()} onDeleted={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Delete client' }));
    expect(screen.getByRole('alertdialog')).toHaveTextContent('Old Name');
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(deleteClient).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Delete client' })).toBeInTheDocument();
  });

  it('confirmed delete calls the API then onDeleted; a failure shows the message', async () => {
    vi.mocked(deleteClient).mockResolvedValueOnce(undefined);
    const onDeleted = vi.fn();
    const { unmount } = render(<ClientActions client={client} onUpdated={vi.fn()} onDeleted={onDeleted} />);
    fireEvent.click(screen.getByRole('button', { name: 'Delete client' }));
    fireEvent.click(screen.getByRole('button', { name: 'Yes, delete' }));
    await waitFor(() => expect(onDeleted).toHaveBeenCalledTimes(1));
    expect(deleteClient).toHaveBeenCalledWith('c1', undefined);
    unmount();

    vi.mocked(deleteClient).mockRejectedValueOnce(new Error('A briefing run is in progress for this client'));
    const onDeleted2 = vi.fn();
    render(<ClientActions client={client} onUpdated={vi.fn()} onDeleted={onDeleted2} />);
    fireEvent.click(screen.getByRole('button', { name: 'Delete client' }));
    fireEvent.click(screen.getByRole('button', { name: 'Yes, delete' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('run is in progress');
    expect(onDeleted2).not.toHaveBeenCalled();
  });
});
