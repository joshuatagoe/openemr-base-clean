// TanStack Query hooks over the DataSource interface. Components get a
// QueryView (idle / loading / error / empty / ready) and never see transports.
import { keepPreviousData, useQueries, useQuery } from '@tanstack/react-query';
import type { Bundle, Patient } from 'fhir/r4';
import { useDataSource } from './DataSourceContext';
import { DataSourceError } from './errors';
import { finderPage, mergeCandidates, planFinder, sortPatients, type FinderPage, type FinderState } from './finder';
import { toView, type QueryView } from './queryView';

export interface ViewResult<T> {
  view: QueryView<T>;
  retry: () => void;
}

/** FHIR id datatype; anything else in the URL is treated as "not found". */
export const FHIR_ID = /^[A-Za-z0-9.-]{1,64}$/;

type FinderData =
  | { kind: 'server'; patients: Patient[]; hasNext: boolean; offset: number }
  | { kind: 'client'; sorted: Patient[]; truncated: boolean };

function patientsOf(bundle: Bundle<Patient>): Patient[] {
  return (bundle.entry ?? []).map((e) => e.resource).filter((r): r is Patient => r?.resourceType === 'Patient');
}

/**
 * The Patient Finder list (see data/finder.ts for how a Finder state becomes
 * FHIR searches). Server mode asks for one row more than the page, because
 * OpenEMR's Bundle has only a `self` link and `total` = the entries returned:
 * the extra row is how the page knows a next page exists. The previous page
 * stays on screen while the next one loads (as the Finder's DataTable does).
 * Mode A only: the SMART transport has a patient-context token and no list.
 */
export interface FinderResult extends ViewResult<FinderPage> {
  /** The rows shown are the previous state's while the new ones load. */
  updating: boolean;
}

export function usePatientFinder(state: FinderState): FinderResult {
  const ds = useDataSource();
  const plan = planFinder(state);
  const { page, pageSize } = state;
  const q = useQuery({
    queryKey:
      plan.mode === 'server'
        ? ['patient-finder', 'server', plan.params]
        : ['patient-finder', 'client', plan.requests, state.filters, state.search.trim(), state.sort],
    enabled: ds.transport === 'bff' && Number.isInteger(page) && page >= 1,
    placeholderData: keepPreviousData,
    queryFn: async ({ signal }): Promise<FinderData> => {
      if (plan.mode === 'server') {
        const patients = patientsOf(await ds.search<Patient>('Patient', plan.params, signal));
        return { kind: 'server', patients: patients.slice(0, pageSize), hasNext: patients.length > pageSize, offset: (page - 1) * pageSize };
      }
      const results = await Promise.all(plan.requests.map(async (params) => patientsOf(await ds.search<Patient>('Patient', params, signal))));
      const merged = mergeCandidates(results);
      return { kind: 'client', sorted: sortPatients(merged.patients.filter(plan.keep), state.sort), truncated: merged.truncated };
    },
  });
  let data: FinderPage | undefined;
  if (q.data?.kind === 'server') {
    const d = q.data;
    data = { patients: d.patients, hasNext: d.hasNext, truncated: false, ...(d.hasNext || d.patients.length === 0 ? {} : { total: d.offset + d.patients.length }) };
  } else if (q.data?.kind === 'client') {
    data = finderPage(q.data.sorted, page, pageSize, q.data.truncated);
  }
  const view = toView({ ...q, data }, (d) => d.patients.length === 0);
  return { view, retry: () => void q.refetch(), updating: q.isPlaceholderData };
}

/** A patient read for a list row: `gone` = not found, not accessible or refused (dropped silently by callers). */
export type PatientRowRead = { id: string; status: 'loading' } | { id: string; status: 'ready'; patient: Patient } | { id: string; status: 'gone' | 'failed' };

const GONE_KINDS = new Set(['not_found', 'forbidden', 'not_accessible', 'bad_request']);

/** Reads several patients by id (shares the cache with usePatient). */
export function usePatientsById(ids: readonly string[]): PatientRowRead[] {
  const ds = useDataSource();
  const results = useQueries({
    queries: ids.map((id) => ({
      queryKey: ['patient', id],
      enabled: FHIR_ID.test(id),
      queryFn: ({ signal }: { signal: AbortSignal }) => ds.read<Patient>('Patient', id, signal),
    })),
  });
  return ids.map((id, i): PatientRowRead => {
    const r = results[i];
    if (!r || !FHIR_ID.test(id)) return { id, status: 'gone' };
    if (r.isError) return { id, status: r.error instanceof DataSourceError && GONE_KINDS.has(r.error.kind) ? 'gone' : 'failed' };
    if (r.isSuccess) return r.data.resourceType === 'Patient' && r.data.id === id ? { id, status: 'ready', patient: r.data } : { id, status: 'gone' };
    return { id, status: 'loading' };
  });
}

export function usePatient(id: string): ViewResult<Patient> {
  const ds = useDataSource();
  const q = useQuery({
    queryKey: ['patient', id],
    enabled: FHIR_ID.test(id),
    queryFn: ({ signal }) => ds.read<Patient>('Patient', id, signal),
  });
  return { view: toView(q), retry: () => void q.refetch() };
}

/** OpenEMR's numeric pid for a FHIR patient id (needed by the medication-list route, C3). */
export function usePatientPid(id: string | undefined): ViewResult<string> {
  const ds = useDataSource();
  const q = useQuery({
    queryKey: ['patient-pid', id],
    enabled: !!id && FHIR_ID.test(id),
    queryFn: ({ signal }) => ds.patientPid(id ?? '', signal),
    staleTime: Infinity,
  });
  return { view: toView(q), retry: () => void q.refetch() };
}
