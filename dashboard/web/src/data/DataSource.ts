import type { Bundle, FhirResource } from 'fhir/r4';

/**
 * How the app reaches OpenEMR. One React app, two transports:
 * - 'bff'   (mode A, standalone): same-origin `/api/...` on the BFF, cookie
 *           session, tokens never in the browser.
 * - 'smart' (modes B/C, served same-origin by OpenEMR or SMART-launched):
 *           browser holds a patient-context token in memory (smart/SmartApp).
 */
export type Transport = 'bff' | 'smart';

/** Resources the dashboard reads by id. */
export type FhirReadType = 'Patient' | 'Practitioner' | 'Organization';

/** Resources the dashboard searches. The BFF allow-lists the parameters. */
export type FhirSearchType = 'Patient' | 'AllergyIntolerance' | 'Condition' | 'MedicationRequest' | 'CareTeam' | 'Observation';

export type SearchParams = Readonly<Record<string, string>>;

/** A row of OpenEMR's standard API `GET /api/patient/:pid/medication`. */
export interface StdMedicationRow {
  uuid?: string;
  title?: string;
  [key: string]: unknown;
}

export interface DataSource {
  readonly transport: Transport;
  read<T extends FhirResource = FhirResource>(type: FhirReadType, id: string, signal?: AbortSignal): Promise<T>;
  search<T extends FhirResource = FhirResource>(type: FhirSearchType, params: SearchParams, signal?: AbortSignal): Promise<Bundle<T>>;
  /** Medication-list rows (not prescriptions). OpenEMR answers 404 when there are none: returned as []. */
  patientMedicationList(pid: string, signal?: AbortSignal): Promise<StdMedicationRow[]>;
  /** OpenEMR's numeric pid (as a string) for a FHIR Patient id (uuid). */
  patientPid(patientId: string, signal?: AbortSignal): Promise<string>;
}
