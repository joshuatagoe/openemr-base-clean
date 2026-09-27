import { http, HttpResponse } from 'msw';
import { afterEach, describe, expect, it } from 'vitest';
import {
  beginLaunch,
  completeLaunch,
  parseLaunchParams,
  PENDING_KEY,
  SMART_SCOPES,
  SmartLaunchError,
  validateIss,
} from '../src/smart/launch';
import { challengeS256, randomToken } from '../src/smart/pkce';
import { server } from './msw/server';

// jsdom's page origin is http://localhost:3000: the "same-origin OpenEMR" in these tests.
const ORIGIN = window.location.origin;
const ISS = `${ORIGIN}/apis/default/fhir`;
const REDIRECT = `${ORIGIN}/interface/modules/custom_modules/oe-module-copilot/public/dashboard/`;
const CLIENT = 'synthetic-client-id-0001';
const PATIENT = '9a000000-0000-4000-8000-00000000000a';

const smartConfig = (over: Record<string, unknown> = {}) =>
  http.get('*/apis/default/fhir/.well-known/smart-configuration', () =>
    HttpResponse.json({
      issuer: ISS,
      authorization_endpoint: `${ORIGIN}/oauth2/default/authorize`,
      token_endpoint: `${ORIGIN}/oauth2/default/token`,
      code_challenge_methods_supported: ['S256'],
      ...over,
    }),
  );

afterEach(() => {
  window.sessionStorage.clear();
  window.localStorage.clear();
});

describe('parseLaunchParams', () => {
  it('recognises an EHR launch, a callback, an OAuth error and a plain visit', () => {
    expect(parseLaunchParams(`?launch=abc&iss=${encodeURIComponent(ISS)}`)).toEqual({ kind: 'launch', launch: 'abc', iss: ISS });
    expect(parseLaunchParams('?code=c1&state=s1')).toEqual({ kind: 'callback', code: 'c1', state: 's1' });
    expect(parseLaunchParams('?error=access_denied&state=s1')).toEqual({ kind: 'error', error: 'access_denied' });
    expect(parseLaunchParams('')).toEqual({ kind: 'none' });
    // Half a launch is not a launch.
    expect(parseLaunchParams('?launch=abc')).toEqual({ kind: 'none' });
    expect(parseLaunchParams('?code=c1')).toEqual({ kind: 'none' });
  });

  it('keeps only known OAuth error codes (never echoes arbitrary text)', () => {
    expect(parseLaunchParams('?error=%3Cb%3Ex%3C%2Fb%3E')).toEqual({ kind: 'error', error: 'unknown' });
  });
});

describe('validateIss', () => {
  it('accepts the same-origin FHIR base (with or without a webroot) and drops a trailing slash', () => {
    expect(validateIss(ISS, ORIGIN)).toBe(ISS);
    expect(validateIss(`${ISS}/`, ORIGIN)).toBe(ISS);
    expect(validateIss(`${ORIGIN}/openemr/apis/default/fhir`, ORIGIN)).toBe(`${ORIGIN}/openemr/apis/default/fhir`);
  });

  it.each([
    ['another origin', 'https://evil.example/apis/default/fhir'],
    ['another port', 'http://localhost:3001/apis/default/fhir'],
    ['not a FHIR base', `${ORIGIN}/oauth2/default`],
    ['a query string', `${ISS}?x=1`],
    ['a fragment', `${ISS}#x`],
    ['not a URL', 'apis/default/fhir'],
    ['a javascript: URL', 'javascript:alert(1)'],
  ])('rejects %s', (_label, iss) => {
    expect(() => validateIss(iss, ORIGIN)).toThrow(SmartLaunchError);
  });
});

describe('PKCE', () => {
  it('computes the RFC 7636 S256 challenge', async () => {
    expect(await challengeS256('dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk')).toBe('E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM');
  });

  it('makes URL-safe random tokens that differ', () => {
    const a = randomToken();
    const b = randomToken();
    expect(a).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(a).not.toBe(b);
  });
});

describe('SMART_SCOPES', () => {
  it('asks for launch, identity and each read scope explicitly (no wildcard, no user/, no offline access)', () => {
    const scopes = SMART_SCOPES.split(' ');
    expect(scopes).toEqual(expect.arrayContaining(['launch', 'openid', 'fhirUser']));
    for (const r of ['Patient', 'AllergyIntolerance', 'Condition', 'MedicationRequest', 'CareTeam', 'Observation', 'Practitioner', 'Organization']) {
      expect(scopes).toContain(`patient/${r}.rs`);
    }
    expect(SMART_SCOPES).not.toMatch(/\*|user\/|system\/|offline_access|launch\/patient/);
  });
});

describe('beginLaunch', () => {
  it('discovers the endpoints from iss and builds the authorize URL with launch, aud and a S256 challenge', async () => {
    server.use(smartConfig());
    const url = new URL(await beginLaunch({ iss: ISS, launch: 'opaque-launch', clientId: CLIENT, redirectUri: REDIRECT }));
    expect(`${url.origin}${url.pathname}`).toBe(`${ORIGIN}/oauth2/default/authorize`);
    const q = url.searchParams;
    expect(q.get('response_type')).toBe('code');
    expect(q.get('client_id')).toBe(CLIENT);
    expect(q.get('redirect_uri')).toBe(REDIRECT);
    expect(q.get('scope')).toBe(SMART_SCOPES);
    expect(q.get('aud')).toBe(ISS);
    expect(q.get('launch')).toBe('opaque-launch');
    expect(q.get('code_challenge_method')).toBe('S256');
    const state = q.get('state') ?? '';
    expect(state).toMatch(/^[A-Za-z0-9_-]{43}$/);

    // Only what must survive the redirect is kept, in sessionStorage.
    const pending = JSON.parse(window.sessionStorage.getItem(PENDING_KEY) ?? '{}') as Record<string, unknown>;
    expect(Object.keys(pending).sort()).toEqual(['createdAt', 'fhirBaseUrl', 'state', 'tokenEndpoint', 'verifier']);
    expect(pending.state).toBe(state);
    expect(pending.fhirBaseUrl).toBe(ISS);
    expect(q.get('code_challenge')).toBe(await challengeS256(String(pending.verifier)));
    expect(window.localStorage.length).toBe(0);
  });

  it('refuses an iss on another origin before any request', async () => {
    await expect(beginLaunch({ iss: 'https://evil.example/apis/default/fhir', launch: 'l', clientId: CLIENT, redirectUri: REDIRECT })).rejects.toMatchObject({
      code: 'bad_issuer',
    });
    expect(window.sessionStorage.getItem(PENDING_KEY)).toBeNull();
  });

  it('refuses endpoints on another origin', async () => {
    server.use(smartConfig({ token_endpoint: 'https://evil.example/token' }));
    await expect(beginLaunch({ iss: ISS, launch: 'l', clientId: CLIENT, redirectUri: REDIRECT })).rejects.toMatchObject({ code: 'bad_configuration' });
  });

  it('refuses a server that does not offer S256', async () => {
    server.use(smartConfig({ code_challenge_methods_supported: ['plain'] }));
    await expect(beginLaunch({ iss: ISS, launch: 'l', clientId: CLIENT, redirectUri: REDIRECT })).rejects.toMatchObject({ code: 'bad_configuration' });
  });

  it('reports an unreachable discovery document', async () => {
    server.use(http.get('*/apis/default/fhir/.well-known/smart-configuration', () => new HttpResponse(null, { status: 404 })));
    await expect(beginLaunch({ iss: ISS, launch: 'l', clientId: CLIENT, redirectUri: REDIRECT })).rejects.toMatchObject({ code: 'discovery_failed' });
  });
});

function seedPending(over: Record<string, unknown> = {}) {
  window.sessionStorage.setItem(
    PENDING_KEY,
    JSON.stringify({
      state: 'state-1',
      verifier: 'verifier-1',
      fhirBaseUrl: ISS,
      tokenEndpoint: `${ORIGIN}/oauth2/default/token`,
      createdAt: Date.now(),
      ...over,
    }),
  );
}

function idToken(claims: Record<string, unknown>): string {
  const b64 = (o: unknown) => btoa(JSON.stringify(o)).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_');
  return `${b64({ alg: 'RS256' })}.${b64(claims)}.sig`;
}

describe('completeLaunch', () => {
  it('exchanges the code (form POST with the verifier, no secret) and keeps the token only in the returned object', async () => {
    let body: URLSearchParams | undefined;
    let contentType = '';
    let credentials = '';
    server.use(
      http.post('*/oauth2/default/token', async ({ request }) => {
        contentType = request.headers.get('content-type') ?? '';
        credentials = request.credentials;
        body = new URLSearchParams(await request.text());
        return HttpResponse.json({
          access_token: 'synthetic-access-token',
          token_type: 'Bearer',
          expires_in: 3600,
          scope: `${SMART_SCOPES} nonce`,
          patient: PATIENT,
          id_token: idToken({ fhirUser: `${ISS}/Practitioner/71000000-0000-4000-8000-000000000001` }),
        });
      }),
    );
    seedPending();
    const session = await completeLaunch({ code: 'code-1', state: 'state-1', clientId: CLIENT, redirectUri: REDIRECT });

    expect(contentType).toContain('application/x-www-form-urlencoded');
    expect(credentials).toBe('omit');
    expect(Object.fromEntries(body ?? [])).toEqual({
      grant_type: 'authorization_code',
      code: 'code-1',
      redirect_uri: REDIRECT,
      client_id: CLIENT,
      code_verifier: 'verifier-1',
    });
    expect(session.accessToken).toBe('synthetic-access-token');
    expect(session.patientId).toBe(PATIENT);
    expect(session.fhirBaseUrl).toBe(ISS);
    expect(session.fhirUser).toEqual({ type: 'Practitioner', id: '71000000-0000-4000-8000-000000000001' });
    expect(session.expiresAt).toBeGreaterThan(Date.now());
    // The pending entry is single use and no storage holds the token.
    expect(window.sessionStorage.getItem(PENDING_KEY)).toBeNull();
    expect(JSON.stringify({ ...window.sessionStorage })).not.toContain('synthetic-access-token');
    expect(window.localStorage.length).toBe(0);
  });

  it('rejects a state that does not match, without calling the token endpoint, and clears the pending entry', async () => {
    let called = false;
    server.use(
      http.post('*/oauth2/default/token', () => {
        called = true;
        return HttpResponse.json({});
      }),
    );
    seedPending();
    await expect(completeLaunch({ code: 'c', state: 'other', clientId: CLIENT, redirectUri: REDIRECT })).rejects.toMatchObject({ code: 'state_mismatch' });
    expect(called).toBe(false);
    expect(window.sessionStorage.getItem(PENDING_KEY)).toBeNull();
  });

  it('rejects a callback with no pending launch (reload, or a replayed code)', async () => {
    await expect(completeLaunch({ code: 'c', state: 'state-1', clientId: CLIENT, redirectUri: REDIRECT })).rejects.toMatchObject({ code: 'state_mismatch' });
  });

  it('rejects a pending launch older than ten minutes', async () => {
    seedPending({ createdAt: Date.now() - 11 * 60_000 });
    await expect(completeLaunch({ code: 'c', state: 'state-1', clientId: CLIENT, redirectUri: REDIRECT })).rejects.toMatchObject({ code: 'state_mismatch' });
  });

  it('rejects a stored token endpoint on another origin', async () => {
    seedPending({ tokenEndpoint: 'https://evil.example/token' });
    await expect(completeLaunch({ code: 'c', state: 'state-1', clientId: CLIENT, redirectUri: REDIRECT })).rejects.toMatchObject({ code: 'state_mismatch' });
  });

  it('fails when the token endpoint refuses the code', async () => {
    server.use(http.post('*/oauth2/default/token', () => HttpResponse.json({ error: 'invalid_client' }, { status: 401 })));
    seedPending();
    await expect(completeLaunch({ code: 'c', state: 'state-1', clientId: CLIENT, redirectUri: REDIRECT })).rejects.toMatchObject({ code: 'token_failed' });
  });

  it('fails when the token response has no patient context', async () => {
    server.use(http.post('*/oauth2/default/token', () => HttpResponse.json({ access_token: 't', token_type: 'Bearer', expires_in: 3600 })));
    seedPending();
    await expect(completeLaunch({ code: 'c', state: 'state-1', clientId: CLIENT, redirectUri: REDIRECT })).rejects.toMatchObject({ code: 'no_patient' });
  });

  it('fails on a non-Bearer token', async () => {
    server.use(http.post('*/oauth2/default/token', () => HttpResponse.json({ access_token: 't', token_type: 'MAC', patient: PATIENT })));
    seedPending();
    await expect(completeLaunch({ code: 'c', state: 'state-1', clientId: CLIENT, redirectUri: REDIRECT })).rejects.toMatchObject({ code: 'token_failed' });
  });

  it('treats a non-Practitioner or missing fhirUser as no user resource', async () => {
    server.use(
      http.post('*/oauth2/default/token', () =>
        HttpResponse.json({ access_token: 't', token_type: 'bearer', expires_in: 3600, patient: PATIENT, id_token: idToken({ fhirUser: `${ISS}/Person/p1` }) }),
      ),
    );
    seedPending();
    const s = await completeLaunch({ code: 'c', state: 'state-1', clientId: CLIENT, redirectUri: REDIRECT });
    expect(s.fhirUser).toBeUndefined();
  });
});
