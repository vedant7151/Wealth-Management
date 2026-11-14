/** Returns the URL only if it is plain http(s); otherwise null. Source URLs
 * come from web search results (untrusted): rendering one as an <a href>
 * without this would allow `javascript:`/`data:` URLs. */
export function safeHttpUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const u = new URL(value);
    return u.protocol === 'http:' || u.protocol === 'https:' ? u.toString() : null;
  } catch {
    return null;
  }
}
