import { isAbsolute, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { DEFAULT_SCOPES, loadConfig } from '../src/config.js';

const base = {
  OPENEMR_BASE_URL: 'https://localhost:9300',
  OAUTH_ISSUER: 'https://localhost:9300/oauth2/default',
  CLIENT_ID: 'cid',
  CLIENT_SECRET: 'csecret',
  REDIRECT_URI: 'http://localhost:5173/auth/callback',
  SESSION_SECRET: 'x'.repeat(32),
};

describe('loadConfig', () => {
  it('derives the OpenEMR endpoints from the issuer and base url', () => {
    const c = loadConfig(base);
    expect(c.authorizeUrl).toBe('https://localhost:9300/oauth2/default/authorize');
    expect(c.tokenUrl).toBe('https://localhost:9300/oauth2/default/token');
    expect(c.fhirBaseUrl).toBe('https://localhost:9300/apis/default/fhir');
    expect(c.stdApiBaseUrl).toBe('https://localhost:9300/apis/default/api');
    expect(c.callbackPath).toBe('/auth/callback');
    expect(c.cookieSecure).toBe(true);
    expect(c.scopes).toBe(DEFAULT_SCOPES);
  });

  it.each(['OPENEMR_BASE_URL', 'OAUTH_ISSUER', 'CLIENT_ID', 'CLIENT_SECRET', 'REDIRECT_URI', 'SESSION_SECRET'])(
    'refuses to start without %s, naming the variable but never its value',
    (key) => {
      const env = Object.fromEntries(Object.entries(base).filter(([k]) => k !== key));
      expect(() => loadConfig(env)).toThrow(key);
    },
  );

  it('resolves a relative WEB_DIST_DIR to an absolute path (@fastify/static requires one)', () => {
    const c = loadConfig({ ...base, WEB_DIST_DIR: '../web/dist' });
    expect(isAbsolute(c.webDistDir ?? '')).toBe(true);
    expect(c.webDistDir).toBe(resolve('../web/dist'));
  });

  it('rejects a short session secret', () => {
    expect(() => loadConfig({ ...base, SESSION_SECRET: 'short' })).toThrow(/SESSION_SECRET/);
  });

  it('does not echo the client secret in errors', () => {
    try {
      loadConfig({ ...base, SESSION_SECRET: 'short' });
    } catch (e) {
      expect(String(e)).not.toContain('csecret');
    }
  });

  it('never allows offline_access (no refresh tokens)', () => {
    expect(DEFAULT_SCOPES.split(' ')).not.toContain('offline_access');
    expect(() => loadConfig({ ...base, OAUTH_SCOPES: 'openid offline_access user/Patient.rs' })).toThrow(/offline_access/);
  });

  it('requests only read scopes by default', () => {
    for (const s of DEFAULT_SCOPES.split(' ').filter((x) => x.includes('/'))) {
      expect(s).toMatch(/^user\/[A-Za-z_]+\.rs$/);
    }
  });

  it('allows plain http only for loopback hosts', () => {
    expect(() => loadConfig({ ...base, OPENEMR_BASE_URL: 'http://openemr.example.com' })).toThrow(/https/);
    expect(() => loadConfig({ ...base, OAUTH_ISSUER: 'http://openemr.example.com/oauth2/default' })).toThrow(/https/);
    expect(loadConfig({ ...base, OPENEMR_BASE_URL: 'http://127.0.0.1:9999' }).fhirBaseUrl).toBe('http://127.0.0.1:9999/apis/default/fhir');
  });

  it('parses COOKIE_SECURE=false for http dev', () => {
    expect(loadConfig({ ...base, COOKIE_SECURE: 'false' }).cookieSecure).toBe(false);
    expect(() => loadConfig({ ...base, COOKIE_SECURE: 'maybe' })).toThrow(/COOKIE_SECURE/);
  });
});
