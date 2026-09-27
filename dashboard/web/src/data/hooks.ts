// TanStack Query hooks over the DataSource interface. Components get a
// QueryView (idle / loading / error / empty / ready) and never see transports.
import { useQueries, useQuery } from '@tanstack/react-query';
import type { Patient } from 'fhir/r4';
import { useDataSource } from './DataSourceContext';
import { DataSourceError } from './errors';
import { toView, type QueryView } from './queryView';

export interface PatientSearchCriteria {
  name?: string;
  birthdate?: string;
  identifier?: string;
}

/** One page of the patient list (or of search results: the same list, narrowed). */
export interface PatientListPage {
  patients: Patient[];
  /** Another page follows (the server returned more than a page). */
  hasNext: boolean;
}

export interface PatientListQuery {
  criteria: PatientSearchCriteria;
  /** 1-based. */
  page: number;
  pageSize?: number;
}

/** Rows per page, as OpenEMR's Patient Finder. */
export const PATIENT_PAGE_SIZE = 20;
/** Last name, then first and middle name (OpenEMR maps family -> lname, given -> fname, mname). */
export const PATIENT_LIST_SORT = 'family,given';

export interface ViewResult<T> {
  view: QueryView<T>;
  retry: () => void;
}

/** FHIR id datatype; anything else in the URL is treated as "not found". */
export const FHIR_ID = /^[A-Za-z0-9.-]{1,64}$/;

function criteriaParams(c: PatientSearchCriteria): Record<string, string> {
  const out: Record<string, string> = {};
  for (const key of ['name', 'birthdate', 'identifier'] as const) {
    const v = c[key]?.trim();
    if (v) out[key] = v;
  }
  return out;
}

/**
 * The patient list, like OpenEMR's Patient Finder: sorted by last name and
 * paged, with or without search criteria. OpenEMR's FHIR Patient search honours
 * `_count`, `_offset` and `_sort` (verified live), but its Bundle has only a
 * `self` link and `total` = the entries returned, so the page asks for one row
 * more than it shows to know whether a next page exists.
 * Mode A only: the SMART transport has a patient-context token and no list.
 */
export function usePatientList({ criteria, page, pageSize = PATIENT_PAGE_SIZE }: PatientListQuery): ViewResult<PatientListPage> {
  const ds = useDataSource();
  const params = {
    ...criteriaParams(criteria),
    _count: String(pageSize + 1),
    _offset: String((page - 1) * pageSize),
    _sort: PATIENT_LIST_SORT,
  };
  const q = useQuery({
    queryKey: ['patient-list', params],
    enabled: ds.transport === 'bff' && Number.isInteger(page) && page >= 1,
    queryFn: async ({ signal }) => {
      const bundle = await ds.search<Patient>('Patient', params, signal);
      const patients = (bundle.entry ?? []).map((e) => e.resource).filter((r): r is Patient => r?.resourceType === 'Patient');
      return { patients: patients.slice(0, pageSize), hasNext: patients.length > pageSize };
    },
  });
  return { view: toView(q, (d) => d.patients.length === 0), retry: () => void q.refetch() };
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
