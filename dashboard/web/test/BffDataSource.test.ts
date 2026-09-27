import { http, HttpResponse } from 'msw';
import { describe, expect, it, vi } from 'vitest';
import { BffDataSource } from '../src/data/BffDataSource';
import { DataSourceError } from '../src/data/errors';
import { SmartDataSource } from '../src/data/SmartDataSource';
import { server } from './msw/server';

const PID = 'a2c3ab57-cdd6-4aad-afc9-e19c171e7ed7';

describe('BffDataSource', () => {
  it('reads a resource from the same-origin /api/fhir proxy with the cookie session', async () => {
    let seen: Request | undefined;
    server.use(
      http.get('*/api/fhir/Patient/:id', ({ request, params }) => {
        seen = request;
        return HttpResponse.json({ resourceType: 'Patient', id: params.id });
      }),
    );
    const ds = new BffDataSource();
    const p = await ds.read('Patient', PID);
    expect(p).toEqual({ resourceType: 'Patient', id: PID });
    expect(ds.transport).toBe('bff');
    expect(new URL(seen?.url ?? '').pathname).toBe(`/api/fhir/Patient/${PID}`);
    expect(new URL(seen?.url ?? '').origin).toBe(window.location.origin);
    expect(seen?.credentials).toBe('same-origin');
    // No token handling in the browser in mode A.
    expect(seen?.headers.get('authorization')).toBeNull();
  });

  it('searches with encoded parameters', async () => {
    let url: URL | undefined;
    server.use(
      http.get('*/api/fhir/Observation', ({ request }) => {
        url = new URL(request.url);
        return HttpResponse.json({ resourceType: 'Bundle', type: 'searchset', entry: [] });
      }),
    );
    const b = await new BffDataSource().search('Observation', { patient: PID, category: 'laboratory' });
    expect(b.resourceType).toBe('Bundle');
    expect(url?.searchParams.get('patient')).toBe(PID);
    expect(url?.searchParams.get('category')).toBe('laboratory');
  });

  it('encodes ids so they cannot change the path', async () => {
    let path = '';
    server.use(
      http.get('*/api/fhir/*', ({ request }) => {
        path = new URL(request.url).pathname;
        return HttpResponse.json({ error: 'not_found' }, { status: 404 });
      }),
    );
    await expect(new BffDataSource().read('Patient', '../Encounter')).rejects.toBeInstanceOf(DataSourceError);
    expect(path).toBe('/api/fhir/Patient/..%2FEncounter');
  });

  it('returns the standard-API medication rows and treats 404 as an empty list', async () => {
    server.use(
      // OpenEMR's ListRestController answers a bare array.
      http.get('*/api/patient/10/medication', () => HttpResponse.json([{ uuid: 'u1', title: 'Synthetic' }])),
      http.get('*/api/patient/11/medication', () => HttpResponse.json({ data: [{ uuid: 'u2' }] })),
      http.get('*/api/patient/7/medication', () => new HttpResponse(null, { status: 404 })),
    );
    const ds = new BffDataSource();
    expect(await ds.patientMedicationList('10')).toEqual([{ uuid: 'u1', title: 'Synthetic' }]);
    expect(await ds.patientMedicationList('11')).toEqual([{ uuid: 'u2' }]);
    expect(await ds.patientMedicationList('7')).toEqual([]);
  });

  it('reports a 401 as session expired and notifies the auth layer', async () => {
    server.use(http.get('*/api/fhir/Patient', () => HttpResponse.json({ error: 'session_expired' }, { status: 401 })));
    const onSessionExpired = vi.fn();
    const err = await new BffDataSource({ onSessionExpired }).search('Patient', { name: 'x' }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(DataSourceError);
    expect((err as DataSourceError).kind).toBe('session_expired');
    expect(onSessionExpired).toHaveBeenCalledOnce();
  });

  it.each([
    [403, { error: 'not_accessible' }, 'not_accessible'],
    [403, { error: 'forbidden' }, 'forbidden'],
    [404, { error: 'not_found' }, 'not_found'],
    [400, { error: 'bad_request' }, 'bad_request'],
    [504, { error: 'upstream_timeout' }, 'timeout'],
    [502, { error: 'upstream_error' }, 'upstream'],
    [500, { error: 'internal_error' }, 'upstream'],
  ])('maps HTTP %i to kind %s', async (status, body, kind) => {
    server.use(http.get('*/api/fhir/Patient/:id', () => HttpResponse.json(body, { status })));
    const err = await new BffDataSource().read('Patient', PID).catch((e: unknown) => e);
    expect((err as DataSourceError).kind).toBe(kind);
    expect((err as DataSourceError).status).toBe(status);
  });

  it('maps a network failure to kind network', async () => {
    server.use(http.get('*/api/fhir/Patient/:id', () => HttpResponse.error()));
    const err = await new BffDataSource().read('Patient', PID).catch((e: unknown) => e);
    expect((err as DataSourceError).kind).toBe('network');
  });
});

describe('SmartDataSource (planned for C5)', () => {
  it('is a stub that says it is not implemented yet', async () => {
    const ds = new SmartDataSource({ fhirBaseUrl: 'https://openemr.example/apis/default/fhir', getAccessToken: () => 'x' });
    expect(ds.transport).toBe('smart');
    const err = await ds.read('Patient', PID).catch((e: unknown) => e);
    expect((err as DataSourceError).kind).toBe('not_implemented');
    await expect(ds.search('Patient', { name: 'x' })).rejects.toMatchObject({ kind: 'not_implemented' });
    await expect(ds.patientMedicationList('7')).rejects.toMatchObject({ kind: 'not_implemented' });
  });
});
