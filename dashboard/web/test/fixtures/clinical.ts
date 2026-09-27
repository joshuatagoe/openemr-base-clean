// Synthetic FHIR R4 resources shaped like OpenEMR's FHIR services output
// (DASHBOARD_ANALYSIS_A2_FHIR §2.2–§2.5, DASHBOARD_PHASE_B_RESULTS T4/T7).
// All names, codes, ids and dates are invented.
import type {
  AllergyIntolerance,
  Bundle,
  CareTeam,
  CareTeamParticipant,
  Condition,
  FhirResource,
  MedicationRequest,
  Observation,
  Organization,
  Practitioner,
} from 'fhir/r4';
import type { StdMedicationRow } from '../../src/data/DataSource';
import { PATIENT_A_ID, PATIENT_B_ID } from './patients';

export const PID_A = '42';

export function bundle<T extends FhirResource>(...resources: T[]): Bundle<T> {
  return {
    resourceType: 'Bundle',
    type: 'collection',
    total: resources.length,
    link: [{ relation: 'self', url: 'https://openemr.invalid/apis/default/fhir/x' }],
    entry: resources.map((r) => ({ fullUrl: `https://openemr.invalid/apis/default/fhir/${r.resourceType}/${r.id ?? ''}`, resource: r })),
  };
}

// ---------------------------------------------------------------- Allergies

const ALLERGY_CLINICAL = 'http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical';

export function allergy(
  id: string,
  title: string,
  opts: { criticality?: AllergyIntolerance['criticality']; reaction?: string; status?: string; patient?: string; verification?: string } = {},
): AllergyIntolerance {
  const a: AllergyIntolerance = {
    resourceType: 'AllergyIntolerance',
    id,
    clinicalStatus: { coding: [{ system: ALLERGY_CLINICAL, code: opts.status ?? 'active', display: 'Active' }] },
    verificationStatus: {
      coding: [{ system: 'http://terminology.hl7.org/CodeSystem/allergyintolerance-verification', code: opts.verification ?? 'unconfirmed' }],
    },
    category: ['medication'],
    code: { coding: [{ system: 'http://terminology.hl7.org/CodeSystem/data-absent-reason', code: 'unknown', display: 'Unknown' }] },
    // OpenEMR puts lists.title, unescaped, in the narrative (UtilsService::createNarrative).
    text: { status: 'additional', div: `<div xmlns='http://www.w3.org/1999/xhtml'>${title}</div>` },
    patient: { reference: `Patient/${opts.patient ?? PATIENT_A_ID}` },
  };
  if (opts.criticality) a.criticality = opts.criticality;
  if (opts.reaction) a.reaction = [{ manifestation: [{ text: opts.reaction, coding: [{ system: 'http://snomed.info/sct', code: '000000', display: opts.reaction }] }] }];
  return a;
}

// ----------------------------------------------------------------- Problems

const CONDITION_CLINICAL = 'http://terminology.hl7.org/CodeSystem/condition-clinical';
const CONDITION_CATEGORY = 'http://terminology.hl7.org/CodeSystem/condition-category';

export function condition(
  id: string,
  category: 'problem-list-item' | 'encounter-diagnosis',
  opts: { text?: string; coding?: { system: string; code: string; display: string }; onset?: string; status?: string; patient?: string; verification?: string } = {},
): Condition {
  const c: Condition = {
    resourceType: 'Condition',
    id,
    clinicalStatus: { coding: [{ system: CONDITION_CLINICAL, code: opts.status ?? 'active', display: 'Active' }] },
    verificationStatus: {
      coding: [{ system: 'http://terminology.hl7.org/CodeSystem/condition-ver-status', code: opts.verification ?? 'unconfirmed' }],
    },
    category: [{ coding: [{ system: CONDITION_CATEGORY, code: category }] }],
    code: opts.coding ? { coding: [opts.coding] } : { text: opts.text ?? 'Problem' },
    subject: { reference: `Patient/${opts.patient ?? PATIENT_A_ID}` },
  };
  if (opts.onset) c.onsetDateTime = opts.onset;
  return c;
}

// -------------------------------------------------------------- Medications

export function medReq(
  id: string,
  drug: string,
  opts: {
    status?: MedicationRequest['status'];
    intent?: MedicationRequest['intent'];
    sig?: string;
    dose?: { value: number; unit: string };
    timing?: string;
    qty?: number;
    authoredOn?: string;
    lastUpdated?: string;
    rxnorm?: string;
    patient?: string;
  } = {},
): MedicationRequest {
  const m: MedicationRequest = {
    resourceType: 'MedicationRequest',
    id,
    meta: { versionId: '1', lastUpdated: opts.lastUpdated ?? '2026-01-15T09:00:00+00:00' },
    status: opts.status ?? 'active',
    intent: opts.intent ?? 'order',
    medicationCodeableConcept: opts.rxnorm
      ? { coding: [{ system: 'http://www.nlm.nih.gov/research/umls/rxnorm', code: opts.rxnorm, display: drug }] }
      : { text: drug },
    subject: { reference: `Patient/${opts.patient ?? PATIENT_A_ID}` },
    // OpenEMR always emits numberOfRepeatsAllowed = 0 (refills is not selected; A2 G8).
    dispenseRequest: { numberOfRepeatsAllowed: 0 },
  };
  if (opts.authoredOn) m.authoredOn = opts.authoredOn;
  if (opts.qty !== undefined && m.dispenseRequest) m.dispenseRequest.quantity = { value: opts.qty, unit: '', code: '' };
  if (opts.sig || opts.dose || opts.timing) {
    m.dosageInstruction = [
      {
        ...(opts.sig ? { text: opts.sig } : {}),
        ...(opts.timing ? { timing: { code: { text: opts.timing } } } : {}),
        ...(opts.dose ? { doseAndRate: [{ doseQuantity: { value: opts.dose.value, unit: opts.dose.unit } }] } : {}),
      },
    ];
  }
  return m;
}

/** A row of the standard API `GET /api/patient/:pid/medication` (`SELECT * FROM lists`). */
export function stdMed(uuid: string, title: string, opts: { id?: string; begdate?: string | null; enddate?: string | null; outcome?: string; pid?: string } = {}): StdMedicationRow {
  return {
    id: opts.id ?? '1',
    uuid,
    pid: opts.pid ?? PID_A,
    type: 'medication',
    title,
    begdate: opts.begdate === undefined ? '2025-01-01 00:00:00' : opts.begdate,
    enddate: opts.enddate ?? null,
    outcome: opts.outcome ?? '0',
    activity: '1',
    date: '2025-01-01 10:00:00',
  };
}

// ---------------------------------------------------------------- Care team

export const PRACT_1 = '71000000-0000-4000-8000-000000000001';
export const PRACT_2 = '72000000-0000-4000-8000-000000000002';
export const ORG_1 = '81000000-0000-4000-8000-000000000001';
export const RELATED_1 = '91000000-0000-4000-8000-000000000001';

export function practitionerParticipant(id: string, opts: { role?: string; org?: string; since?: string } = {}): CareTeamParticipant {
  return {
    role: [{ coding: [{ system: 'http://snomed.info/sct', code: '309343006', display: opts.role ?? 'Physician' }] }],
    member: { reference: `Practitioner/${id}` },
    ...(opts.org ? { onBehalfOf: { reference: `Organization/${opts.org}` } } : {}),
    ...(opts.since ? { period: { start: opts.since } } : {}),
  };
}

/** OpenEMR also adds every member facility as its own Organization participant (A2 §2.5). */
export function facilityParticipant(orgId: string): CareTeamParticipant {
  return {
    role: [{ coding: [{ system: 'http://snomed.info/sct', code: '43741000', display: 'Site of care' }] }],
    member: { reference: `Organization/${orgId}` },
  };
}

export function careTeam(id: string, opts: { name?: string; status?: CareTeam['status']; participants?: CareTeamParticipant[]; patient?: string } = {}): CareTeam {
  const t: CareTeam = {
    resourceType: 'CareTeam',
    id,
    status: opts.status ?? 'active',
    subject: { reference: `Patient/${opts.patient ?? PATIENT_A_ID}` },
    participant: opts.participants ?? [],
  };
  if (opts.name) t.name = opts.name;
  return t;
}

export function practitioner(id: string, given: string, family: string): Practitioner {
  return { resourceType: 'Practitioner', id, name: [{ use: 'official', given: [given], family }] };
}

export function organization(id: string, name: string): Organization {
  return { resourceType: 'Organization', id, name };
}

// --------------------------------------------------------------------- Labs

export const ENC_1 = 'e1000000-0000-4000-8000-000000000001';
export const ENC_2 = 'e2000000-0000-4000-8000-000000000002';

/**
 * One lab Observation, shaped like FhirObservationLaboratoryService output: one
 * per procedure_result row, effectiveDateTime = procedure_report.date_report as
 * local wall-clock time plus offset, encounter = the order's encounter, no
 * report id and no procedure name (A2 §2.6).
 */
export function labObs(
  id: string,
  opts: {
    loinc?: string;
    name?: string;
    /** false: result_code or result_text empty, so OpenEMR sends nullFlavor UNK. */
    coded?: boolean;
    status?: Observation['status'];
    effective?: string | null;
    encounter?: string | null;
    patient?: string;
    value?: { value: number; unit: string };
  } = {},
): Observation {
  const o: Observation = {
    resourceType: 'Observation',
    id,
    meta: { versionId: '1', lastUpdated: opts.effective ?? '2026-09-12T09:15:00+00:00' },
    status: opts.status ?? 'final',
    category: [{ coding: [{ system: 'http://terminology.hl7.org/CodeSystem/observation-category', code: 'laboratory', display: 'Laboratory' }] }],
    code:
      opts.coded === false
        ? { coding: [{ system: 'http://terminology.hl7.org/CodeSystem/v3-NullFlavor', code: 'UNK', display: 'unknown' }] }
        : { coding: [{ system: 'http://loinc.org', code: opts.loinc ?? '4548-4', display: opts.name ?? 'Hemoglobin A1c' }] },
    subject: { reference: `Patient/${opts.patient ?? PATIENT_A_ID}` },
  };
  if (opts.effective !== null) o.effectiveDateTime = opts.effective ?? '2026-09-12T09:15:00+00:00';
  if (opts.encounter !== null) o.encounter = { reference: `Encounter/${opts.encounter ?? ENC_1}` };
  if (opts.value) o.valueQuantity = { value: opts.value.value, unit: opts.value.unit };
  return o;
}

export { PATIENT_A_ID, PATIENT_B_ID };
