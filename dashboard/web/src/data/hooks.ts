// TanStack Query hooks over the DataSource interface. Components get a
// QueryView (idle / loading / error / empty / ready) and never see transports.
import { useQuery } from '@tanstack/react-query';
import type { Patient } from 'fhir/r4';
import { useDataSource } from './DataSourceContext';
import { toView, type QueryView } from './queryView';

export interface PatientSearchCriteria {
  name?: string;
  birthdate?: string;
  identifier?: string;
}

export interface PatientSearchResult {
  patients: Patient[];
  /** The server has more matches than this page (bundle has a `next` link). */
  truncated: boolean;
}

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

export function usePatientSearch(criteria: PatientSearchCriteria | undefined): ViewResult<PatientSearchResult> {
  const ds = useDataSource();
  const params = criteria ? criteriaParams(criteria) : {};
  const enabled = Object.keys(params).length > 0;
  const q = useQuery({
    queryKey: ['patient-search', params],
    enabled,
    queryFn: async ({ signal }) => {
      const bundle = await ds.search<Patient>('Patient', params, signal);
      const patients = (bundle.entry ?? []).map((e) => e.resource).filter((r): r is Patient => r?.resourceType === 'Patient');
      return { patients, truncated: (bundle.link ?? []).some((l) => l.relation === 'next') };
    },
  });
  return { view: toView(q, (d) => d.patients.length === 0), retry: () => void q.refetch() };
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
