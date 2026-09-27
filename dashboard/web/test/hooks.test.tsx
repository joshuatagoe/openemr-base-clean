import { QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';
import type { DataSource } from '../src/data/DataSource';
import { DataSourceContext } from '../src/data/DataSourceContext';
import { DataSourceError } from '../src/data/errors';
import { usePatient, usePatientList, usePatientPid } from '../src/data/hooks';
import { createQueryClient } from '../src/data/queryClient';
import { patientA, PATIENT_A_ID, searchset } from './fixtures/patients';

function fakeSource(overrides: Partial<DataSource> = {}): DataSource {
  return {
    transport: 'bff',
    read: vi.fn(() => Promise.resolve(patientA)) as DataSource['read'],
    search: vi.fn(() => Promise.resolve(searchset(patientA))) as DataSource['search'],
    patientMedicationList: vi.fn(() => Promise.resolve([])),
    patientPid: vi.fn(() => Promise.resolve('7')),
    ...overrides,
  };
}

function wrapper(ds: DataSource) {
  const qc = createQueryClient({ retryDelay: 0 });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>
      <DataSourceContext.Provider value={ds}>{children}</DataSourceContext.Provider>
    </QueryClientProvider>
  );
}

describe('data hooks over the DataSource interface', () => {
  it('usePatient: loading -> ready', async () => {
    const ds = fakeSource();
    const { result } = renderHook(() => usePatient(PATIENT_A_ID), { wrapper: wrapper(ds) });
    expect(result.current.view.status).toBe('loading');
    await waitFor(() => expect(result.current.view).toEqual({ status: 'ready', data: patientA }));
    expect(ds.read).toHaveBeenCalledWith('Patient', PATIENT_A_ID, expect.any(AbortSignal));
  });

  it('usePatient: does not call the source for an id that is not a FHIR id', () => {
    const ds = fakeSource();
    const { result } = renderHook(() => usePatient('a/b'), { wrapper: wrapper(ds) });
    expect(result.current.view.status).toBe('idle');
    expect(ds.read).not.toHaveBeenCalled();
  });

  it('usePatientList: one page of the list, sorted by family then given name, asking for one extra row to know if there is a next page', async () => {
    const page = Array.from({ length: 3 }, (_, i) => ({ ...patientA, id: `p${i}` }));
    const ds = fakeSource({ search: vi.fn(() => Promise.resolve(searchset(...page))) as DataSource['search'] });
    const { result } = renderHook(() => usePatientList({ criteria: { name: ' Sample ', birthdate: '' }, page: 3, pageSize: 2 }), {
      wrapper: wrapper(ds),
    });
    await waitFor(() => expect(result.current.view.status).toBe('ready'));
    expect(ds.search).toHaveBeenCalledWith('Patient', { name: 'Sample', _count: '3', _offset: '4', _sort: 'family,given' }, expect.any(AbortSignal));
    if (result.current.view.status === 'ready') {
      expect(result.current.view.data.patients.map((p) => p.id)).toEqual(['p0', 'p1']);
      expect(result.current.view.data.hasNext).toBe(true);
    }
  });

  it('usePatientList: no criteria is the plain list; last page has no next; empty page is "empty"', async () => {
    const ds = fakeSource({ search: vi.fn(() => Promise.resolve(searchset(patientA))) as DataSource['search'] });
    const { result } = renderHook(() => usePatientList({ criteria: {}, page: 1, pageSize: 20 }), { wrapper: wrapper(ds) });
    await waitFor(() => expect(result.current.view.status).toBe('ready'));
    expect(ds.search).toHaveBeenCalledWith('Patient', { _count: '21', _offset: '0', _sort: 'family,given' }, expect.any(AbortSignal));
    if (result.current.view.status === 'ready') expect(result.current.view.data.hasNext).toBe(false);

    const none = fakeSource({ search: vi.fn(() => Promise.resolve(searchset())) as DataSource['search'] });
    const empty = renderHook(() => usePatientList({ criteria: {}, page: 2, pageSize: 20 }), { wrapper: wrapper(none) });
    await waitFor(() => expect(empty.result.current.view.status).toBe('empty'));
  });

  it('usePatientList: disabled for the SMART transport (patient-context token, no list)', () => {
    const ds = fakeSource({ transport: 'smart' });
    const { result } = renderHook(() => usePatientList({ criteria: {}, page: 1, pageSize: 20 }), { wrapper: wrapper(ds) });
    expect(result.current.view.status).toBe('idle');
    expect(ds.search).not.toHaveBeenCalled();
  });

  it('usePatientPid: resolves the numeric pid; errors surface as data', async () => {
    const ok = renderHook(() => usePatientPid(PATIENT_A_ID), { wrapper: wrapper(fakeSource()) });
    await waitFor(() => expect(ok.result.current.view).toEqual({ status: 'ready', data: '7' }));

    const failing = fakeSource({ patientPid: vi.fn(() => Promise.reject(new DataSourceError('forbidden', 'x', 403))) });
    const bad = renderHook(() => usePatientPid(PATIENT_A_ID), { wrapper: wrapper(failing) });
    await waitFor(() => expect(bad.result.current.view.status).toBe('error'));
    expect(failing.patientPid).toHaveBeenCalledTimes(1);
  });
});
