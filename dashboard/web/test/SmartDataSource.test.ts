import { http, HttpResponse } from 'msw';
import { describe, expect, it, vi } from 'vitest';
import { DataSourceError } from '../src/data/errors';
import { SmartDataSource } from '../src/data/SmartDataSource';
import { server } from './msw/server';

const ORIGIN = window.location.origin;
const FHIR = `${ORIGIN}/apis/default/fhir`;
const PID = '9a000000-0000-4000-8000-00000000000a';

function ds(opts: { token?: string | undefined; onSessionExpired?: () => void } = {}) {
  const token = 'token' in opts ? opts.token : 'synthetic-access-token';
  return new SmartDataSource({ fhirBaseUrl: FHIR, getAccessToken: () => token, ...(opts.onSessionExpired ? { onSessionExpired: opts.onSessionExpired } : {}) });
}

describe('SmartDataSource (modes B/C)', () => {
  it('reads from the same-origin FHIR base with the in-memory bearer token and no cookies', async () => {
    let seen: Request | undefined;
    server.use(
      http.get('*/apis/default/fhir/Patient/:id', ({ request, params }) => {
        seen = request;
        return HttpResponse.json({ resourceType: 'Patient', id: params.id });
      }),
    );
    const source = ds();
    expect(source.transport).toBe('smart');
    expect(await source.read('Patient', PID)).toEqual({ resourceType: 'Patient', id: PID });
    const url = new URL(seen?.url ?? '');
    expect(url.origin).toBe(ORIGIN);
    expect(url.pathname).toBe(`/apis/default/fhir/Patient/${PID}`);
    expect(seen?.headers.get('authorization')).toBe('Bearer synthetic-access-token');
    expect(seen?.headers.get('accept')).toContain('application/fhir+json');
    expect(seen?.credentials).toBe('omit');
  });

  it('searches with encoded parameters', async () => {
    let url: URL | undefined;
    server.use(
      http.get('*/apis/default/fhir/Observation', ({ request }) => {
        url = new URL(request.url);
        return HttpResponse.json({ resourceType: 'Bundle', type: 'searchset', entry: [] });
      }),
    );
    await ds().search('Observation', { patient: PID, category: 'laboratory' });
    expect(url?.searchParams.get('patient')).toBe(PID);
    expect(url?.searchParams.get('category')).toBe('laboratory');
  });

  it('encodes ids so they cannot change the path', async () => {
    let path = '';
    server.use(
      http.get('*/apis/default/fhir/*', ({ request }) => {
        path = new URL(request.url).pathname;
        return HttpResponse.json({}, { status: 404 });
      }),
    );
    await expect(ds().read('Patient', '../Encounter')).rejects.toBeInstanceOf(DataSourceError);
    expect(path).toBe('/apis/default/fhir/Patient/..%2FEncounter');
  });

  it('ends the session on 401 (token expired or revoked)', async () => {
    const onSessionExpired = vi.fn();
    server.use(http.get('*/apis/default/fhir/Patient/:id', () => HttpResponse.json({ message: 'Unauthorized' }, { status: 401 })));
    const err = await ds({ onSessionExpired }).read('Patient', PID).catch((e: unknown) => e);
    expect((err as DataSourceError).kind).toBe('session_expired');
    expect(onSessionExpired).toHaveBeenCalledTimes(1);
  });

  it('does not call OpenEMR without a token', async () => {
    const onSessionExpired = vi.fn();
    // Any request would fail the test (onUnhandledRequest: 'error').
    const err = await ds({ token: undefined, onSessionExpired }).read('Patient', PID).catch((e: unknown) => e);
    expect((err as DataSourceError).kind).toBe('session_expired');
    expect(onSessionExpired).toHaveBeenCalledTimes(1);
  });

  it.each([
    [403, 'forbidden'],
    [404, 'not_found'],
    [400, 'bad_request'],
    // OpenEMR answers 500 "patient id invalid" for a patient outside the token's context.
    [500, 'upstream'],
    [502, 'upstream'],
  ] as const)('maps HTTP %i to kind %s', async (status, kind) => {
    server.use(http.get('*/apis/default/fhir/Patient/:id', () => HttpResponse.json({}, { status })));
    const err = await ds().read('Patient', PID).catch((e: unknown) => e);
    expect((err as DataSourceError).kind).toBe(kind);
    expect((err as DataSourceError).status).toBe(status);
  });

  it('maps a network failure to kind network', async () => {
    server.use(http.get('*/apis/default/fhir/Patient/:id', () => HttpResponse.error()));
    const err = await ds().read('Patient', PID).catch((e: unknown) => e);
    expect((err as DataSourceError).kind).toBe('network');
  });

  it('has no standard API: the medication list and the pid are forbidden to patient tokens, without a request', async () => {
    const source = ds();
    await expect(source.patientMedicationList('7')).rejects.toMatchObject({ kind: 'forbidden' });
    await expect(source.patientPid(PID)).rejects.toMatchObject({ kind: 'forbidden' });
  });

  it('refuses a FHIR base on another origin', () => {
    expect(() => new SmartDataSource({ fhirBaseUrl: 'https://evil.example/apis/default/fhir', getAccessToken: () => 't' })).toThrow(/same origin/);
  });
});
