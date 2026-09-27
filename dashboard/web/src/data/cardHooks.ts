// Queries behind the clinical cards. Every result passes the subject guard
// before any model code sees it.
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { AllergyIntolerance, Bundle, CareTeam, Condition, FhirResource, MedicationRequest, Observation, Organization, Practitioner } from 'fhir/r4';
import { allergyRows, type AllergyRow } from '../fhir/allergy';
import { careTeamView, pickCareTeam, type CareTeamViewModel } from '../fhir/careTeam';
import { problemRows, type ProblemRow } from '../fhir/condition';
import { latestLabReport, type LabReportSummary } from '../fhir/lab';
import { combinedRows, medicationListRows, prescriptionTable, type CombinedRow, type MedListRow, type RxTable } from '../fhir/medication';
import { guardPid, guardSubject } from '../fhir/subject';
import { useDataSource } from './DataSourceContext';
import { DataSourceError } from './errors';
import { FHIR_ID, type ViewResult } from './hooks';
import { toView } from './queryView';

function resourcesOf<T extends FhirResource>(bundle: Bundle<T>, type: T['resourceType']): T[] {
  return (bundle.entry ?? []).map((e) => e.resource).filter((r): r is T => r?.resourceType === type);
}

export function useAllergies(patientId: string): ViewResult<AllergyRow[]> {
  const ds = useDataSource();
  const q = useQuery({
    queryKey: ['allergies', patientId],
    enabled: FHIR_ID.test(patientId),
    queryFn: async ({ signal }) => {
      const b = await ds.search<AllergyIntolerance>('AllergyIntolerance', { patient: patientId }, signal);
      return allergyRows(guardSubject(resourcesOf(b, 'AllergyIntolerance'), patientId, (a) => a.patient?.reference, 'AllergyIntolerance'));
    },
  });
  return { view: toView(q, (rows) => rows.length === 0), retry: () => void q.refetch() };
}

/** Both categories: a problem linked to an encounter only appears as `encounter-diagnosis` (A2 §2.2). */
export const PROBLEM_CATEGORIES = ['problem-list-item', 'encounter-diagnosis'] as const;

export function useProblems(patientId: string): ViewResult<ProblemRow[]> {
  const ds = useDataSource();
  const q = useQuery({
    queryKey: ['problems', patientId],
    enabled: FHIR_ID.test(patientId),
    queryFn: async ({ signal }) => {
      const bundles = await Promise.all(PROBLEM_CATEGORIES.map((category) => ds.search<Condition>('Condition', { patient: patientId, category }, signal)));
      const all = bundles.flatMap((b) => resourcesOf(b, 'Condition'));
      return problemRows(guardSubject(all, patientId, (c) => c.subject?.reference, 'Condition'));
    },
  });
  return { view: toView(q, (rows) => rows.length === 0), retry: () => void q.refetch() };
}

export type MedicationCards =
  | { kind: 'split'; medications: MedListRow[]; prescriptions: RxTable }
  /** The medication list could not be read, so the two cards cannot be separated. */
  | { kind: 'combined'; rows: CombinedRow[] };

/** Answers that mean "this transport or user cannot read the list", as opposed to a failure worth retrying. */
function listUnavailable(e: unknown): boolean {
  return e instanceof DataSourceError && (e.kind === 'forbidden' || e.kind === 'not_accessible' || e.kind === 'not_implemented');
}

const NO_LIST = Symbol('no-list');

export function useMedicationCards(patientId: string): ViewResult<MedicationCards> {
  const ds = useDataSource();
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ['medications', patientId],
    enabled: FHIR_ID.test(patientId),
    queryFn: async ({ signal }): Promise<MedicationCards> => {
      const listRows = (async () => {
        try {
          const pid = await qc.ensureQueryData({ queryKey: ['patient-pid', patientId], queryFn: ({ signal: s }) => ds.patientPid(patientId, s), staleTime: Infinity });
          // OpenEMR answers 404 when the patient has no list medications; the data source returns [].
          return guardPid(await ds.patientMedicationList(pid, signal), pid, 'medication list');
        } catch (e) {
          if (listUnavailable(e)) return NO_LIST;
          throw e;
        }
      })();
      const [bundle, rows] = await Promise.all([ds.search<MedicationRequest>('MedicationRequest', { patient: patientId }, signal), listRows]);
      const medReqs = guardSubject(resourcesOf(bundle, 'MedicationRequest'), patientId, (m) => m.subject?.reference, 'MedicationRequest');
      if (rows === NO_LIST) return { kind: 'combined', rows: combinedRows(medReqs) };
      const uuids = new Set(rows.map((r) => (typeof r.uuid === 'string' ? r.uuid : '')).filter(Boolean));
      return { kind: 'split', medications: medicationListRows(rows, medReqs, new Date()), prescriptions: prescriptionTable(medReqs, uuids) };
    },
  });
  return { view: toView(q), retry: () => void q.refetch() };
}

/** `null` when the patient has no usable care team. */
export function useCareTeam(patientId: string): ViewResult<CareTeamViewModel | null> {
  const ds = useDataSource();
  const q = useQuery({
    queryKey: ['care-team', patientId],
    enabled: FHIR_ID.test(patientId),
    queryFn: async ({ signal }) => {
      const b = await ds.search<CareTeam>('CareTeam', { patient: patientId }, signal);
      const team = pickCareTeam(guardSubject(resourcesOf(b, 'CareTeam'), patientId, (t) => t.subject?.reference, 'CareTeam'));
      return team ? careTeamView(team) : null;
    },
  });
  return { view: toView(q, (v) => v === null), retry: () => void q.refetch() };
}

/** The PHP Labs card's single most recent report; `null` when there is none. */
export function useLatestLabReport(patientId: string): ViewResult<LabReportSummary | null> {
  const ds = useDataSource();
  const q = useQuery({
    queryKey: ['labs', patientId],
    enabled: FHIR_ID.test(patientId),
    queryFn: async ({ signal }) => {
      const b = await ds.search<Observation>('Observation', { patient: patientId, category: 'laboratory' }, signal);
      return latestLabReport(guardSubject(resourcesOf(b, 'Observation'), patientId, (o) => o.subject?.reference, 'Observation'));
    },
  });
  return { view: toView(q, (r) => r === null), retry: () => void q.refetch() };
}

/** "Last, First", as the PHP Care Team card lists users. */
function practitionerName(p: Practitioner): string {
  const names = p.name ?? [];
  const n = names.find((x) => x.use === 'official') ?? names[0];
  const text = [n?.family, n?.given?.[0]].filter((s): s is string => !!s && s.trim() !== '').join(', ');
  return text || n?.text || '';
}

/**
 * Display name of a care-team member or facility. For the Physicians group
 * OpenEMR answers 403 here (ACL admin/users); the card then says so, and the
 * dashboard never works around it.
 */
export function useResourceName(type: 'Practitioner' | 'Organization', id: string | undefined): ViewResult<string> {
  const ds = useDataSource();
  const q = useQuery({
    queryKey: ['name', type, id],
    enabled: !!id && FHIR_ID.test(id),
    staleTime: 5 * 60_000,
    queryFn: async ({ signal }) => {
      if (type === 'Practitioner') return practitionerName(await ds.read<Practitioner>('Practitioner', id ?? '', signal));
      return ((await ds.read<Organization>('Organization', id ?? '', signal)).name ?? '').trim();
    },
  });
  return { view: toView(q, (name) => name === ''), retry: () => void q.refetch() };
}
