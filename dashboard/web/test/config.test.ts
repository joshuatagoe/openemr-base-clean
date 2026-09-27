import { describe, expect, it } from 'vitest';
import { parseTransport, smartConfigUrl } from '../src/config';

describe('build configuration', () => {
  it('selects the transport: smart only when asked for, the BFF otherwise', () => {
    expect(parseTransport('smart')).toBe('smart');
    expect(parseTransport(' SMART ')).toBe('smart');
    expect(parseTransport('bff')).toBe('bff');
    expect(parseTransport(undefined)).toBe('bff');
    expect(parseTransport('anything')).toBe('bff');
  });

  it('puts the SMART client config next to the app folder (outside the build output)', () => {
    expect(smartConfigUrl('/interface/modules/custom_modules/oe-module-copilot/public/dashboard/', 'https://emr.example')).toBe(
      'https://emr.example/interface/modules/custom_modules/oe-module-copilot/public/dashboard.config.json',
    );
    expect(smartConfigUrl('/', 'http://localhost:3000')).toBe('http://localhost:3000/dashboard.config.json');
  });
});
