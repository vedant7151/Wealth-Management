import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, fetchClientBriefings, fetchClients } from './api';

describe('API fetch layer (no mock-data fallback)', () => {
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it('fetchClients throws a clear ApiError when the API is unreachable', async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));
    await expect(fetchClients()).rejects.toMatchObject({ message: 'Cannot reach the API server.' });
  });

  it('fetchClients throws with the server message on a non-2xx status', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({ detail: 'Invalid or expired token' })
    });
    const err = await fetchClients().catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.message).toBe('Invalid or expired token');
  });

  it('fetchClients returns an empty list as-is (no invented client)', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => [] });
    expect(await fetchClients()).toEqual([]);
  });

  it('fetchClients returns the real clients and sends the bearer token', async () => {
    const clients = [{ id: 'c1', name: 'Real Name', portfolio_themes: ['AI'] }];
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => clients });
    globalThis.fetch = fetchMock;
    expect(await fetchClients(async () => 'tok')).toEqual(clients);
    expect(fetchMock.mock.calls[0][1].headers).toEqual({ Authorization: 'Bearer tok' });
  });

  it('fetchClientBriefings sends the token and returns the real payload', async () => {
    const payload = { client: { id: 'c1', name: 'Real Name', portfolio_themes: [] }, briefings: [] };
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => payload });
    globalThis.fetch = fetchMock;
    expect(await fetchClientBriefings('c1', async () => 'tok')).toEqual(payload);
    expect(fetchMock.mock.calls[0][1].headers).toEqual({ Authorization: 'Bearer tok' });
  });

  it('fetchClientBriefings throws on HTTP errors and on network failure', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({ detail: 'Client not found' }) });
    await expect(fetchClientBriefings('x')).rejects.toMatchObject({ message: 'Client not found', status: 404 });
    globalThis.fetch = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));
    await expect(fetchClientBriefings('x')).rejects.toMatchObject({ message: 'Cannot reach the API server.' });
  });
});
