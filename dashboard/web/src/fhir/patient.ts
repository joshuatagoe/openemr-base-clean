// Reading OpenEMR's FHIR Patient for the header and the picker.
// Mapping (DASHBOARD_ANALYSIS_A2_FHIR §1, §2.1): official name = fname/lname,
// MRN (pubpid) = identifier with type code PT, deceased = deceasedDateTime.
import type { HumanName, Identifier, Patient } from 'fhir/r4';
import { ageAtDeathDisplay, patientAgeDisplay } from '../format/age';
import { formatShortDate, type DateDisplayFormat } from '../format/date';

const V2_0203 = 'http://terminology.hl7.org/CodeSystem/v2-0203';

function isMrn(identifier: Identifier): boolean {
  return (identifier.type?.coding ?? []).some((c) => c.code === 'PT' && (c.system === undefined || c.system === V2_0203));
}

/**
 * The MRN (OpenEMR `pubpid`): the identifier whose type code is `PT`, chosen by
 * code, never by position. OpenEMR emits the SSN (type `SS`) first; it is
 * never returned here, and there is no fallback to another identifier.
 */
export function patientMrn(patient: Patient): string | undefined {
  return patient.identifier?.find((i) => isMrn(i) && typeof i.value === 'string' && i.value.trim() !== '')?.value;
}

function currentName(patient: Patient): HumanName | undefined {
  const names = patient.name ?? [];
  return names.find((n) => n.use === 'official') ?? names[0];
}

/** Patient bar name: `fname + " " + lname` (no middle name, prefix or suffix). */
export function patientBarName(patient: Patient): string {
  const n = currentName(patient);
  return [n?.given?.[0], n?.family].filter((part): part is string => !!part && part.trim() !== '').join(' ');
}

/** Picker name: "Family, Given Middle". */
export function patientListName(patient: Patient): string {
  const n = currentName(patient);
  const given = (n?.given ?? []).filter((g) => g.trim() !== '').join(' ');
  if (n?.family && given) return `${n.family}, ${given}`;
  return n?.family || given || n?.text || '(no name)';
}

/** Date of death as YYYY-MM-DD, from deceasedDateTime. */
export function deceasedDate(patient: Patient): string | undefined {
  return patient.deceasedDateTime && patient.deceasedDateTime.length >= 10 ? patient.deceasedDateTime.slice(0, 10) : undefined;
}

export function isDeceased(patient: Patient): boolean {
  return deceasedDate(patient) !== undefined || patient.deceasedBoolean === true;
}

export interface DisplayOptions {
  today: string;
  dateFormat: DateDisplayFormat;
}

/** "DOB: <date> Age: <age>", or "DOB: <date> Age at death: <age>" (demographics.php setMyPatient). */
export function patientDobAgeLine(patient: Patient, { today, dateFormat }: DisplayOptions): string {
  const dob = patient.birthDate;
  const dobText = `DOB: ${formatShortDate(dob, dateFormat)}`;
  if (!isDeceased(patient)) return `${dobText} Age: ${patientAgeDisplay(dob, today)}`;
  const death = deceasedDate(patient);
  const age = death && dob ? ageAtDeathDisplay(dob, death) : 'unknown';
  return `${dobText} Age at death: ${age}`;
}

const SEX_LABELS: Readonly<Record<string, string>> = { male: 'Male', female: 'Female', other: 'Other', unknown: 'Unknown' };

/** Picker only (the header does not show sex, for PHP parity). */
export function patientSexLabel(patient: Patient): string {
  return patient.gender ? (SEX_LABELS[patient.gender] ?? patient.gender) : '';
}
