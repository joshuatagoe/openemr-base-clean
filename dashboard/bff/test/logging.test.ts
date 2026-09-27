import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { CLIENT_SECRET } from './helpers/mockOpenEmr.js';
import { login, SESSION_SECRET, startHarness, type Harness } from './helpers/harness.js';

let h: Harness;
beforeEach(async () => {
  h = await startHarness({ LOG_LEVEL: 'debug' });
});
afterEach(async () => {
  await h.close();
});

describe('request logging', () => {
  it('logs request id, route and status, but never tokens, secrets, cookies, codes or PHI in query strings', async () => {
    const cookie = await login(h);
    const sid = cookie.split('=').slice(1).join('=');
    await h.app.inject({ url: '/api/fhir/Patient?name=Zebediah&birthdate=1931-02-03', headers: { cookie } });
    await h.app.inject({ url: '/api/fhir/Patient/revoked', headers: { cookie } });
    await h.app.inject({ url: '/api/fhir/Encounter?patient=secret-patient-uuid', headers: { cookie } });
    await h.app.inject({ method: 'POST', url: '/auth/logout', headers: { cookie } });

    const text = h.logs.join('\n');
    expect(text.length).toBeGreaterThan(0);
    const forbidden = [
      ...h.mock.issuedAccessTokens,
      ...h.mock.issuedIdTokens,
      CLIENT_SECRET,
      SESSION_SECRET,
      sid,
      decodeURIComponent(sid),
      'Zebediah',
      '1931-02-03',
      'secret-patient-uuid',
      'code-',
      'Bearer',
    ];
    for (const f of forbidden) expect(text).not.toContain(f);

    const lines = h.logs.map((l) => JSON.parse(l) as Record<string, unknown>);
    const done = lines.filter((l) => l.msg === 'request completed');
    expect(done.length).toBeGreaterThanOrEqual(5);
    for (const l of done) {
      expect(typeof l.reqId).toBe('string');
      expect(typeof l.statusCode).toBe('number');
      expect(typeof l.route).toBe('string');
      expect(String(l.route)).not.toContain('?');
    }
    const search = done.find((l) => l.resource === 'Patient' && l.statusCode === 200);
    expect(search?.route).toBe('/api/fhir/*');
  });

  it('never logs the Finder search values: SSN, phone number, External ID, or a refused value', async () => {
    const cookie = await login(h);
    const urls = [
      '/api/fhir/Patient?identifier=900-11-2222&_count=101&_sort=identifier',
      '/api/fhir/Patient?phone=(555)%20010-0199&_count=101&_sort=-phone',
      '/api/fhir/Patient?identifier=EXT-77123&_count=11',
      // Refused (400): a malformed phone, and an unknown parameter with a value.
      '/api/fhir/Patient?phone=555-0199x44&_count=11',
      '/api/fhir/Patient?ssn=900-99-8888',
    ];
    const statuses: number[] = [];
    for (const url of urls) statuses.push((await h.app.inject({ url, headers: { cookie } })).statusCode);
    expect(statuses).toEqual([200, 200, 200, 400, 400]);

    const text = h.logs.join('\n');
    for (const value of ['900-11-2222', '010-0199', '0199', '(555)', 'EXT-77123', '77123', '0199x44', '900-99-8888', 'identifier=', 'phone=', 'ssn']) {
      expect(text).not.toContain(value);
    }
    const done = h.logs.map((l) => JSON.parse(l) as Record<string, unknown>).filter((l) => l.msg === 'request completed');
    expect(done.filter((l) => l.resource === 'Patient')).toHaveLength(3);
  });
});
