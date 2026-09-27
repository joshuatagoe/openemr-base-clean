import { QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';
import type { DataSource } from '../src/data/DataSource';
import { DataSourceContext } from '../src/data/DataSourceContext';
import { DataSourceError } from '../src/data/errors';
import { usePatient, usePatientPid, usePatientSearch } from '../src/data/hooks';
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

  it('usePatientSearch: idle without criteria, trims and drops blank criteria, empty when no match', async () => {
    const ds = fakeSource({ search: vi.fn(() => Promise.resolve(searchset())) as DataSource['search'] });
    const idle = renderHook(() => usePatientSearch({ name: '  ' }), { wrapper: wrapper(ds) });
    expect(idle.result.current.view.status).toBe('idle');
    const { result } = renderHook(() => usePatientSearch({ name: ' Sample ', birthdate: '' }), { wrapper: wrapper(ds) });
    await waitFor(() => expect(result.current.view.status).toBe('empty'));
    expect(ds.search).toHaveBeenCalledTimes(1);
    expect(ds.search).toHaveBeenCalledWith('Patient', { name: 'Sample' }, expect.any(AbortSignal));
  });

  it('usePatientSearch: flags a truncated result (bundle has a next link)', async () => {
    const bundle = { ...searchset(patientA), link: [{ relation: 'next', url: 'https://openemr.invalid/next' }] };
    const ds = fakeSource({ search: vi.fn(() => Promise.resolve(bundle)) as DataSource['search'] });
    const { result } = renderHook(() => usePatientSearch({ name: 'Sample' }), { wrapper: wrapper(ds) });
    await waitFor(() => expect(result.current.view.status).toBe('ready'));
    if (result.current.view.status === 'ready') expect(result.current.view.data.truncated).toBe(true);
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
