import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { CLIENT_ID, CLIENT_SECRET } from './helpers/mockOpenEmr.js';
import { cookieHeader, login, parseSetCookies, REDIRECT_URI, startHarness, startLogin, type Harness } from './helpers/harness.js';

let h: Harness;
beforeEach(async () => {
  h = await startHarness();
});
afterEach(async () => {
  await h.close();
});

describe('GET /auth/login', () => {
  it('redirects to the OpenEMR authorize endpoint with code + PKCE S256 + state + nonce', async () => {
    const start = await startLogin(h);
    const p = start.authorizeUrl.searchParams;
    expect(`${start.authorizeUrl.origin}${start.authorizeUrl.pathname}`).toBe(`${h.mock.issuer}/authorize`);
    expect(p.get('response_type')).toBe('code');
    expect(p.get('client_id')).toBe(CLIENT_ID);
    expect(p.get('redirect_uri')).toBe(REDIRECT_URI);
    expect(p.get('code_challenge_method')).toBe('S256');
    expect(p.get('code_challenge')).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(p.get('state')).toMatch(/^[A-Za-z0-9_-]{43,}$/);
    expect(p.get('nonce')).toMatch(/^[A-Za-z0-9_-]{43,}$/);
    expect(p.get('aud')).toBe(`${h.mock.baseUrl}/apis/default/fhir`);
    // The secret and the verifier never go to the browser.
    expect(start.authorizeUrl.toString()).not.toContain(CLIENT_SECRET);
    expect(p.has('code_verifier')).toBe(false);
    expect(p.has('client_secret')).toBe(false);
  });

  it('requests openid fhirUser and read scopes, never offline_access', async () => {
    const scopes = ((await startLogin(h)).authorizeUrl.searchParams.get('scope') ?? '').split(' ');
    expect(scopes).toEqual(expect.arrayContaining(['openid', 'fhirUser', 'user/Patient.rs', 'user/Observation.rs']));
    expect(scopes).not.toContain('offline_access');
  });

  it('binds the login to the browser with a short-lived HttpOnly, Secure, SameSite=Lax cookie', async () => {
    const { loginCookie } = await startLogin(h);
    expect(loginCookie.attributes.httponly).toBe(true);
    expect(loginCookie.attributes.secure).toBe(true);
    expect(String(loginCookie.attributes.samesite).toLowerCase()).toBe('lax');
    expect(loginCookie.attributes.path).toBe('/');
    expect(Number(loginCookie.attributes['max-age'])).toBeLessThanOrEqual(600);
  });

  it('uses a fresh state, nonce and verifier per login', async () => {
    const a = await startLogin(h);
    const b = await startLogin(h);
    expect(a.state).not.toBe(b.state);
    expect(a.nonce).not.toBe(b.nonce);
    expect(a.challenge).not.toBe(b.challenge);
  });
});

describe('GET /auth/callback', () => {
  it('exchanges the code with the client secret ALWAYS sent, plus the PKCE verifier', async () => {
    await login(h);
    const tokenCalls = h.mock.requests.filter((r) => r.path === '/oauth2/default/token');
    expect(tokenCalls).toHaveLength(1);
    const body = tokenCalls[0]?.body ?? {};
    expect(body.grant_type).toBe('authorization_code');
    expect(body.client_id).toBe(CLIENT_ID);
    expect(body.client_secret).toBe(CLIENT_SECRET);
    expect(body.redirect_uri).toBe(REDIRECT_URI);
    expect(body.code_verifier).toMatch(/^[A-Za-z0-9_-]{43,128}$/);
  });

  it('sets an HttpOnly, Secure, SameSite=Lax session cookie that expires with the token', async () => {
    const start = await startLogin(h);
    const code = h.mock.issueCode({ challenge: start.challenge, nonce: start.nonce, redirectUri: REDIRECT_URI });
    const res = await h.app.inject({
      url: `/auth/callback?code=${code}&state=${start.state}`,
      headers: { cookie: cookieHeader([start.loginCookie]) },
    });
    expect(res.statusCode).toBe(302);
    expect(res.headers.location).toBe('/');
    const cookies = parseSetCookies(res);
    const sid = cookies.find((c) => c.name === '__Host-dash_sid');
    expect(sid).toBeDefined();
    expect(sid?.attributes.httponly).toBe(true);
    expect(sid?.attributes.secure).toBe(true);
    expect(String(sid?.attributes.samesite).toLowerCase()).toBe('lax');
    expect(sid?.attributes.path).toBe('/');
    expect(sid?.attributes.domain).toBeUndefined();
    const maxAge = Number(sid?.attributes['max-age']);
    expect(maxAge).toBeGreaterThan(3000);
    expect(maxAge).toBeLessThanOrEqual(3600);
    // The token itself is never in a cookie.
    for (const c of cookies) {
      for (const t of h.mock.issuedAccessTokens) expect(decodeURIComponent(c.value)).not.toContain(t);
    }
    // The one-time login cookie is cleared.
    const cleared = cookies.find((c) => c.name === start.loginCookie.name);
    expect(cleared?.value).toBe('');
  });

  it('rejects a state mismatch without calling the token endpoint', async () => {
    const start = await startLogin(h);
    const code = h.mock.issueCode({ challenge: start.challenge, nonce: start.nonce, redirectUri: REDIRECT_URI });
    const res = await h.app.inject({
      url: `/auth/callback?code=${code}&state=forged-state-value`,
      headers: { cookie: cookieHeader([start.loginCookie]) },
    });
    expect(res.statusCode).toBe(302);
    expect(res.headers.location).toBe('/?auth_error=state_mismatch');
    expect(h.mock.requests.some((r) => r.path.endsWith('/token'))).toBe(false);
    expect(parseSetCookies(res).some((c) => c.name.endsWith('dash_sid') && c.value !== '')).toBe(false);
  });

  it('rejects a callback without the login cookie (login CSRF)', async () => {
    const start = await startLogin(h);
    const code = h.mock.issueCode({ challenge: start.challenge, nonce: start.nonce, redirectUri: REDIRECT_URI });
    const res = await h.app.inject({ url: `/auth/callback?code=${code}&state=${start.state}` });
    expect(res.headers.location).toBe('/?auth_error=login_expired');
    expect(h.mock.requests.some((r) => r.path.endsWith('/token'))).toBe(false);
  });

  it('rejects a tampered (unsigned) login cookie', async () => {
    const start = await startLogin(h);
    const code = h.mock.issueCode({ challenge: start.challenge, nonce: start.nonce, redirectUri: REDIRECT_URI });
    const res = await h.app.inject({
      url: `/auth/callback?code=${code}&state=${start.state}`,
      headers: { cookie: `${start.loginCookie.name}=${start.loginCookie.value.split('.')[0]}` },
    });
    expect(res.headers.location).toBe('/?auth_error=login_expired');
  });

  it('makes the pending login single-use (a replayed callback fails)', async () => {
    const start = await startLogin(h);
    const code = h.mock.issueCode({ challenge: start.challenge, nonce: start.nonce, redirectUri: REDIRECT_URI });
    const url = `/auth/callback?code=${code}&state=${start.state}`;
    const headers = { cookie: cookieHeader([start.loginCookie]) };
    expect((await h.app.inject({ url, headers })).headers.location).toBe('/');
    expect((await h.app.inject({ url, headers })).headers.location).toBe('/?auth_error=login_expired');
  });

  it('rejects an id_token whose nonce does not match', async () => {
    const start = await startLogin(h);
    const code = h.mock.issueCode({ challenge: start.challenge, nonce: start.nonce, redirectUri: REDIRECT_URI, claims: { nonce: 'other' } });
    const res = await h.app.inject({ url: `/auth/callback?code=${code}&state=${start.state}`, headers: { cookie: cookieHeader([start.loginCookie]) } });
    expect(res.headers.location).toBe('/?auth_error=invalid_id_token');
    expect(parseSetCookies(res).some((c) => c.name.endsWith('dash_sid') && c.value !== '')).toBe(false);
  });

  it.each([
    ['iss', { iss: 'https://evil.example/oauth2/default' }],
    ['aud', { aud: 'some-other-client' }],
    ['exp', { exp: 1000 }],
  ])('rejects an id_token with a wrong %s', async (_name, claims) => {
    const start = await startLogin(h);
    const code = h.mock.issueCode({ challenge: start.challenge, nonce: start.nonce, redirectUri: REDIRECT_URI, claims });
    const res = await h.app.inject({ url: `/auth/callback?code=${code}&state=${start.state}`, headers: { cookie: cookieHeader([start.loginCookie]) } });
    expect(res.headers.location).toBe('/?auth_error=invalid_id_token');
  });

  it('reports a failed token exchange without leaking details', async () => {
    const start = await startLogin(h);
    h.mock.nextTokenResponse = { status: 401, body: { error: 'invalid_client', error_description: 'Client authentication failed' } };
    const res = await h.app.inject({ url: `/auth/callback?code=x&state=${start.state}`, headers: { cookie: cookieHeader([start.loginCookie]) } });
    expect(res.headers.location).toBe('/?auth_error=token_exchange_failed');
  });

  it('passes an authorization error from OpenEMR through as a coarse code', async () => {
    const start = await startLogin(h);
    const res = await h.app.inject({ url: `/auth/callback?error=access_denied&state=${start.state}`, headers: { cookie: cookieHeader([start.loginCookie]) } });
    expect(res.headers.location).toBe('/?auth_error=access_denied');
  });
});

describe('session', () => {
  it('GET /auth/me reports the signed-in user (display name only, no token)', async () => {
    const cookie = await login(h);
    const res = await h.app.inject({ url: '/auth/me', headers: { cookie } });
    expect(res.statusCode).toBe(200);
    const body = res.json();
    expect(body.authenticated).toBe(true);
    expect(body.user.displayName).toBe('Dana Testdoctor');
    expect(res.body).not.toContain(h.mock.issuedAccessTokens[0]);
    expect(res.body).not.toContain('id_token');
    expect(res.headers['cache-control']).toContain('no-store');
  });

  it('resolves the name from the fhirUser Practitioner when the id_token has none (OpenEMR sends no name claim)', async () => {
    const cookie = await login(h, { name: undefined, fhirUser: `${h.mock.baseUrl}/apis/default/fhir/Practitioner/prac-named` });
    const me = (await h.app.inject({ url: '/auth/me', headers: { cookie } })).json();
    expect(me.user.displayName).toBe('Dana Named');
    const call = h.mock.requests.find((r) => r.path.endsWith('/Practitioner/prac-named'));
    expect(call?.headers.authorization).toBe(`Bearer ${h.mock.issuedAccessTokens[0]}`);
  });

  it('falls back to a generic label when the Practitioner read is refused (Physicians get 403)', async () => {
    const cookie = await login(h, { name: undefined, fhirUser: `${h.mock.baseUrl}/apis/default/fhir/Practitioner/prac-1` });
    const me = (await h.app.inject({ url: '/auth/me', headers: { cookie } })).json();
    expect(me.user.displayName).toBe('OpenEMR user');
  });

  it('never follows a fhirUser that points outside the configured FHIR base', async () => {
    const cookie = await login(h, { name: undefined, fhirUser: 'https://evil.example/fhir/Practitioner/x' });
    const me = (await h.app.inject({ url: '/auth/me', headers: { cookie } })).json();
    expect(me.user.displayName).toBe('OpenEMR user');
    expect(h.mock.requests.some((r) => r.path.includes('Practitioner'))).toBe(false);
  });

  it('GET /auth/me without a cookie is 401 unauthenticated', async () => {
    const res = await h.app.inject({ url: '/auth/me' });
    expect(res.statusCode).toBe(401);
    expect(res.json()).toEqual({ error: 'unauthenticated' });
  });

  it('expires the session with the token (1 h) -> 401 session_expired', async () => {
    const cookie = await login(h);
    h.clock.now += 3599 * 1000;
    const res = await h.app.inject({ url: '/auth/me', headers: { cookie } });
    expect(res.statusCode).toBe(401);
    expect(res.json()).toEqual({ error: 'session_expired' });
    const api = await h.app.inject({ url: '/api/fhir/Patient?name=Demo', headers: { cookie } });
    expect(api.statusCode).toBe(401);
    expect(api.json()).toEqual({ error: 'session_expired' });
  });

  it('keeps the session valid before the token expires', async () => {
    const cookie = await login(h);
    h.clock.now += 30 * 60 * 1000;
    expect((await h.app.inject({ url: '/auth/me', headers: { cookie } })).statusCode).toBe(200);
  });

  it('follows a shorter token lifetime from OpenEMR', async () => {
    h.mock.tokenExpiresIn = 120;
    const cookie = await login(h);
    h.clock.now += 120 * 1000;
    expect((await h.app.inject({ url: '/auth/me', headers: { cookie } })).statusCode).toBe(401);
  });

  it('POST /auth/logout clears the session and the cookie', async () => {
    const cookie = await login(h);
    const out = await h.app.inject({ method: 'POST', url: '/auth/logout', headers: { cookie } });
    expect(out.statusCode).toBe(204);
    const sid = parseSetCookies(out).find((c) => c.name === '__Host-dash_sid');
    expect(sid?.value).toBe('');
    const me = await h.app.inject({ url: '/auth/me', headers: { cookie } });
    expect(me.statusCode).toBe(401);
    const api = await h.app.inject({ url: '/api/fhir/Patient?name=Demo', headers: { cookie } });
    expect(api.statusCode).toBe(401);
  });

  it('GET /auth/logout is not a logout (no state change on GET)', async () => {
    const cookie = await login(h);
    const res = await h.app.inject({ method: 'GET', url: '/auth/logout', headers: { cookie } });
    expect(res.statusCode).toBe(404);
    expect((await h.app.inject({ url: '/auth/me', headers: { cookie } })).statusCode).toBe(200);
  });

  it('rejects a forged session cookie', async () => {
    await login(h);
    const res = await h.app.inject({ url: '/auth/me', headers: { cookie: '__Host-dash_sid=forged' } });
    expect(res.statusCode).toBe(401);
  });
});

describe('COOKIE_SECURE=false (plain-http local dev)', () => {
  it('drops the Secure flag and the __Host- prefix but keeps HttpOnly and SameSite=Lax', async () => {
    const dev = await startHarness({ COOKIE_SECURE: 'false' });
    try {
      const cookie = await login(dev);
      expect(cookie.startsWith('dash_sid=')).toBe(true);
      const { loginCookie } = await startLogin(dev);
      expect(loginCookie.attributes.secure).toBeUndefined();
      expect(loginCookie.attributes.httponly).toBe(true);
      expect(String(loginCookie.attributes.samesite).toLowerCase()).toBe('lax');
    } finally {
      await dev.close();
    }
  });
});
