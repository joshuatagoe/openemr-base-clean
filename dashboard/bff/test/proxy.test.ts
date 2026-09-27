import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { OTHER_PATIENT_ID, STD_PATIENT_FORBIDDEN_UUID, STD_PATIENT_NO_PID_UUID, STD_PATIENT_UUID } from './helpers/mockOpenEmr.js';
import { login, startHarness, type Harness } from './helpers/harness.js';

const PID = 'a2c3ab57-cdd6-4aad-afc9-e19c171e7ed7';

let h: Harness;
let cookie: string;
beforeEach(async () => {
  h = await startHarness();
  cookie = await login(h);
});
afterEach(async () => {
  await h.close();
});

const fhirCalls = () => h.mock.requests.filter((r) => r.path.startsWith('/apis/'));

describe('allow-listed proxy', () => {
  it('requires a session', async () => {
    const res = await h.app.inject({ url: '/api/fhir/Patient?name=Demo' });
    expect(res.statusCode).toBe(401);
    expect(res.json()).toEqual({ error: 'unauthenticated' });
    expect(fhirCalls()).toHaveLength(0);
  });

  it('proxies a Patient name search with the bearer token and nothing from the browser', async () => {
    const res = await h.app.inject({
      url: '/api/fhir/Patient?name=Demo',
      headers: { cookie: `${cookie}; other=1`, authorization: 'Bearer browser-supplied', 'x-forwarded-for': '1.2.3.4' },
    });
    expect(res.statusCode).toBe(200);
    expect(res.json()).toMatchObject({ resourceType: 'Bundle' });
    expect(res.headers['content-type']).toContain('json');
    const call = fhirCalls()[0];
    expect(call?.path).toBe('/apis/default/fhir/Patient');
    expect(call?.query).toBe('name=Demo');
    expect(call?.headers.authorization).toBe(`Bearer ${h.mock.issuedAccessTokens[0]}`);
    expect(call?.headers.cookie).toBeUndefined();
    expect(call?.headers['x-forwarded-for']).toBeUndefined();
  });

  it('proxies the bounded patient list (no criteria) with its paging parameters unchanged', async () => {
    const res = await h.app.inject({ url: '/api/fhir/Patient?_count=21&_offset=20&_sort=family,given', headers: { cookie } });
    expect(res.statusCode).toBe(200);
    const call = fhirCalls()[0];
    expect(call?.path).toBe('/apis/default/fhir/Patient');
    expect(new URLSearchParams(call?.query)).toEqual(new URLSearchParams('_count=21&_offset=20&_sort=family,given'));
  });

  it('400s an unbounded patient list and never calls OpenEMR', async () => {
    for (const url of ['/api/fhir/Patient', '/api/fhir/Patient?_offset=0&_sort=family', '/api/fhir/Patient?_count=102']) {
      const res = await h.app.inject({ url, headers: { cookie } });
      expect(res.statusCode).toBe(400);
    }
    expect(fhirCalls()).toHaveLength(0);
  });

  it('marks API responses no-store', async () => {
    const res = await h.app.inject({ url: `/api/fhir/Patient/${PID}`, headers: { cookie } });
    expect(res.statusCode).toBe(200);
    expect(res.headers['cache-control']).toContain('no-store');
  });

  it.each([
    `/api/fhir/AllergyIntolerance?patient=${PID}`,
    `/api/fhir/Condition?patient=${PID}&category=problem-list-item`,
    `/api/fhir/MedicationRequest?patient=${PID}`,
    `/api/fhir/CareTeam?patient=${PID}`,
    `/api/fhir/Observation?patient=${PID}&category=laboratory`,
  ])('proxies %s', async (url) => {
    const res = await h.app.inject({ url, headers: { cookie } });
    expect(res.statusCode).toBe(200);
  });

  it('proxies the standard API medication list and passes its 404-as-empty through', async () => {
    const ok = await h.app.inject({ url: '/api/patient/10/medication', headers: { cookie } });
    expect(ok.statusCode).toBe(200);
    expect(fhirCalls().at(-1)?.path).toBe('/apis/default/api/patient/10/medication');
    const none = await h.app.inject({ url: '/api/patient/7/medication', headers: { cookie } });
    expect(none.statusCode).toBe(404);
  });

  describe('standard API patient read (uuid -> pid)', () => {
    it('proxies /api/patient/:puuid and answers only { pid, uuid } (never the rest of patient_data)', async () => {
      const res = await h.app.inject({ url: `/api/patient/${STD_PATIENT_UUID}`, headers: { cookie } });
      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ pid: '7', uuid: STD_PATIENT_UUID });
      expect(res.body).not.toContain('999-00-1234');
      expect(res.body).not.toContain('Synthetica');
      expect(res.headers['cache-control']).toContain('no-store');
      const call = fhirCalls().at(-1);
      expect(call?.path).toBe(`/apis/default/api/patient/${STD_PATIENT_UUID}`);
      expect(call?.query).toBe('');
      expect(call?.headers.authorization).toBe(`Bearer ${h.mock.issuedAccessTokens[0]}`);
    });

    it('requires a session', async () => {
      const res = await h.app.inject({ url: `/api/patient/${STD_PATIENT_UUID}` });
      expect(res.statusCode).toBe(401);
      expect(fhirCalls()).toHaveLength(0);
    });

    it('400s any query parameter without calling OpenEMR', async () => {
      const res = await h.app.inject({ url: `/api/patient/${STD_PATIENT_UUID}?fields=ss`, headers: { cookie } });
      expect(res.statusCode).toBe(400);
      expect(fhirCalls()).toHaveLength(0);
    });

    it('passes 404 and 403 through with normalised bodies', async () => {
      const missing = await h.app.inject({ url: '/api/patient/00000000-0000-4000-8000-00000000abcd', headers: { cookie } });
      expect(missing.statusCode).toBe(404);
      expect(missing.json()).toEqual({ error: 'not_found' });
      const forbidden = await h.app.inject({ url: `/api/patient/${STD_PATIENT_FORBIDDEN_UUID}`, headers: { cookie } });
      expect(forbidden.statusCode).toBe(403);
      expect(forbidden.json()).toEqual({ error: 'forbidden' });
    });

    it('answers 502 when OpenEMR returns no pid', async () => {
      const res = await h.app.inject({ url: `/api/patient/${STD_PATIENT_NO_PID_UUID}`, headers: { cookie } });
      expect(res.statusCode).toBe(502);
      expect(res.json()).toEqual({ error: 'upstream_invalid_response' });
    });
  });

  it.each([
    '/api/fhir/Encounter?patient=x',
    '/api/fhir/Patient/x/$everything',
    '/api/fhir/metadata',
    '/api/fhir/Practitioner',
    '/api/patient/7',
    '/api/patient/7/allergy',
    '/api/patient',
    '/api/facility',
    '/api/anything/else',
    '/apis/default/fhir/Patient',
  ])('404s a route that is not allow-listed: %s', async (url) => {
    const res = await h.app.inject({ url, headers: { cookie } });
    expect(res.statusCode).toBe(404);
    expect(fhirCalls()).toHaveLength(0);
  });

  it.each(['POST', 'PUT', 'PATCH', 'DELETE'] as const)('404s %s on an allow-listed path', async (method) => {
    const res = await h.app.inject({ method, url: '/api/fhir/Patient?name=Demo', headers: { cookie }, payload: {} });
    expect(res.statusCode).toBe(404);
    expect(fhirCalls()).toHaveLength(0);
  });

  it('400s a parameter that is not allow-listed and never calls OpenEMR', async () => {
    const res = await h.app.inject({ url: '/api/fhir/Patient?name=Demo&_count=500', headers: { cookie } });
    expect(res.statusCode).toBe(400);
    expect(res.json()).toMatchObject({ error: 'bad_request' });
    expect(fhirCalls()).toHaveLength(0);
  });

  it("maps OpenEMR's 500 for another patient to a 403-style error", async () => {
    const res = await h.app.inject({ url: `/api/fhir/Patient/${OTHER_PATIENT_ID}`, headers: { cookie } });
    expect(res.statusCode).toBe(403);
    expect(res.json()).toEqual({ error: 'not_accessible' });
  });

  it('passes a 403 through with a normalised body', async () => {
    const res = await h.app.inject({ url: '/api/fhir/Practitioner/prac-1', headers: { cookie } });
    expect(res.statusCode).toBe(403);
    expect(res.json()).toEqual({ error: 'forbidden' });
  });

  it('turns other upstream 5xx into 502 without leaking the upstream body', async () => {
    const res = await h.app.inject({ url: '/api/fhir/Patient/boom', headers: { cookie } });
    expect(res.statusCode).toBe(502);
    expect(res.body).not.toContain('internal detail');
  });

  it('times out a slow upstream with 504', async () => {
    const res = await h.app.inject({ url: '/api/fhir/Patient/slow', headers: { cookie } });
    expect(res.statusCode).toBe(504);
    expect(res.json()).toEqual({ error: 'upstream_timeout' });
  });

  it('ends the session when OpenEMR rejects the token (401)', async () => {
    const res = await h.app.inject({ url: '/api/fhir/Patient/revoked', headers: { cookie } });
    expect(res.statusCode).toBe(401);
    expect(res.json()).toEqual({ error: 'session_expired' });
    expect((await h.app.inject({ url: '/auth/me', headers: { cookie } })).statusCode).toBe(401);
  });
});

describe('security headers and health', () => {
  it('sets CSP with frame-ancestors none and related headers on every response', async () => {
    for (const url of ['/healthz', '/auth/me', '/api/fhir/Patient?name=Demo']) {
      const res = await h.app.inject({ url, headers: { cookie } });
      const csp = String(res.headers['content-security-policy']);
      expect(csp).toContain("default-src 'self'");
      expect(csp).toContain("frame-ancestors 'none'");
      expect(csp).toContain("object-src 'none'");
      expect(res.headers['x-content-type-options']).toBe('nosniff');
      expect(res.headers['referrer-policy']).toBe('no-referrer');
      expect(res.headers['x-frame-options']).toBe('DENY');
    }
  });

  it('sends HSTS only when cookies are Secure (https deployments)', async () => {
    const res = await h.app.inject({ url: '/healthz' });
    expect(res.headers['strict-transport-security']).toContain('max-age=');
  });

  it('GET /healthz is 200 without a session and does not call OpenEMR', async () => {
    const before = h.mock.requests.length;
    const res = await h.app.inject({ url: '/healthz' });
    expect(res.statusCode).toBe(200);
    expect(res.json()).toEqual({ status: 'ok' });
    expect(h.mock.requests.length).toBe(before);
  });

  it('returns a request id header generated server-side', async () => {
    const res = await h.app.inject({ url: '/healthz', headers: { 'x-request-id': 'client-chosen' } });
    expect(res.headers['x-request-id']).toMatch(/^[0-9a-f-]{36}$/);
  });
});
