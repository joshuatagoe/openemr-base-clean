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
});
