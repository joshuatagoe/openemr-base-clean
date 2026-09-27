// Synthetic FHIR R4 Patients shaped like OpenEMR's FhirPatientService output:
// the SSN identifier (type SS) is emitted BEFORE the MRN (type PT), the MRN's
// `system` is the v2-0203 code system itself, and `active` is always true.
// All names, numbers and dates are invented.
import type { Bundle, Identifier, Patient } from 'fhir/r4';

export const V2_0203 = 'http://terminology.hl7.org/CodeSystem/v2-0203';

export const ssn = (value: string): Identifier => ({
  use: 'official',
  type: { coding: [{ system: V2_0203, code: 'SS' }] },
  system: 'http://hl7.org/fhir/sid/us-ssn',
  value,
});

export const mrn = (value: string): Identifier => ({
  use: 'official',
  type: { coding: [{ system: V2_0203, code: 'PT' }] },
  system: V2_0203,
  value,
});

export const PATIENT_A_ID = '9a000000-0000-4000-8000-00000000000a';
export const PATIENT_B_ID = '9b000000-0000-4000-8000-00000000000b';
export const PATIENT_DECEASED_ID = '9d000000-0000-4000-8000-00000000000d';

export const patientA: Patient = {
  resourceType: 'Patient',
  id: PATIENT_A_ID,
  active: true,
  identifier: [ssn('900-11-2222'), mrn('SYN-1001')],
  name: [
    { use: 'old', family: 'Formername', given: ['Oldgiven'] },
    { use: 'official', family: 'Samplefamily', given: ['Ada', 'Quinn'], prefix: ['Ms.'], suffix: ['Jr.'] },
  ],
  gender: 'female',
  birthDate: '1980-06-15',
  deceasedBoolean: false,
};

export const patientB: Patient = {
  resourceType: 'Patient',
  id: PATIENT_B_ID,
  active: true,
  identifier: [mrn('SYN-2002'), ssn('900-33-4444')],
  name: [{ use: 'official', family: 'Otherfamily', given: ['Bram'] }],
  gender: 'male',
  birthDate: '1955-01-02',
  deceasedBoolean: false,
};

export const patientDeceased: Patient = {
  resourceType: 'Patient',
  id: PATIENT_DECEASED_ID,
  active: true,
  identifier: [ssn('900-55-6666'), mrn('SYN-3003')],
  name: [{ use: 'official', family: 'Pastfamily', given: ['Cleo'] }],
  gender: 'female',
  birthDate: '1930-03-20',
  deceasedDateTime: '2001-03-19T08:30:00+00:00',
};

export function searchset(...patients: Patient[]): Bundle<Patient> {
  return {
    resourceType: 'Bundle',
    type: 'searchset',
    total: patients.length,
    entry: patients.map((p) => ({ fullUrl: `https://openemr.invalid/apis/default/fhir/Patient/${p.id ?? ''}`, resource: p, search: { mode: 'match' } })),
  };
}
