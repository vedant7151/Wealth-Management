import { describe, expect, it } from 'vitest';
import { safeHttpUrl } from './safeUrl';

describe('safeHttpUrl', () => {
  it('allows http and https', () => {
    expect(safeHttpUrl('https://example.com/a?b=1')).toBe('https://example.com/a?b=1');
    expect(safeHttpUrl('http://example.com/')).toBe('http://example.com/');
  });
  it('rejects javascript:, data:, vbscript:, file: and garbage', () => {
    for (const bad of ['javascript:alert(1)', ' JaVaScRiPt:alert(1)', 'data:text/html,<script>1</script>', 'vbscript:x', 'file:///etc/passwd', 'not a url', '', null, undefined]) {
      expect(safeHttpUrl(bad as string | null | undefined)).toBeNull();
    }
  });
});
