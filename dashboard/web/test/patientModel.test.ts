import type { Patient } from 'fhir/r4';
import { describe, expect, it } from 'vitest';
import { deceasedDate, patientBarName, patientDobAgeLine, patientListName, patientMrn, patientSexLabel } from '../src/fhir/patient';
import { ageAtDeathDisplay, patientAgeDisplay } from '../src/format/age';
import { formatShortDate, parseDateDisplayFormat } from '../src/format/date';
import { mrn, patientA, patientB, patientDeceased, ssn, V2_0203 } from './fixtures/patients';

const TODAY = '2026-09-27';

describe('MRN (pubpid) selection', () => {
  it('picks the identifier whose type code is PT when the SSN comes first (OpenEMR order)', () => {
    expect(patientMrn(patientA)).toBe('SYN-1001');
  });

  it('picks PT regardless of order', () => {
    expect(patientMrn(patientB)).toBe('SYN-2002');
    const reversed: Patient = { ...patientA, identifier: [...(patientA.identifier ?? [])].reverse() };
    expect(patientMrn(reversed)).toBe('SYN-1001');
  });

  it('never falls back to the SSN or to the first identifier', () => {
    expect(patientMrn({ resourceType: 'Patient', identifier: [ssn('900-00-0000')] })).toBeUndefined();
    expect(patientMrn({ resourceType: 'Patient', identifier: [{ value: 'untyped-1' }] })).toBeUndefined();
    expect(patientMrn({ resourceType: 'Patient' })).toBeUndefined();
  });

  it('finds PT among several codings and ignores a PT code from another code system', () => {
    const multi: Patient = {
      resourceType: 'Patient',
      identifier: [
        { type: { coding: [{ system: 'urn:other', code: 'PT' }] }, value: 'wrong-system' },
        { type: { coding: [{ system: 'urn:x', code: 'X' }, { system: V2_0203, code: 'PT' }] }, value: 'right' },
      ],
    };
    expect(patientMrn(multi)).toBe('right');
  });

  it('skips a PT identifier with an empty value', () => {
    expect(patientMrn({ resourceType: 'Patient', identifier: [{ ...mrn(''), value: '' }, mrn('SYN-9')] })).toBe('SYN-9');
  });
});

describe('names', () => {
  it('patient bar: first + last of the official name only (no middle, prefix, suffix, old names)', () => {
    expect(patientBarName(patientA)).toBe('Ada Samplefamily');
  });

  it('falls back to the first name entry when none is official', () => {
    expect(patientBarName({ resourceType: 'Patient', name: [{ family: 'Solo', given: ['Uno'] }] })).toBe('Uno Solo');
    expect(patientBarName({ resourceType: 'Patient', name: [{ family: 'Onlyfamily' }] })).toBe('Onlyfamily');
    expect(patientBarName({ resourceType: 'Patient' })).toBe('');
  });

  it('picker list name: "Family, Given Middle"', () => {
    expect(patientListName(patientA)).toBe('Samplefamily, Ada Quinn');
    expect(patientListName(patientB)).toBe('Otherfamily, Bram');
  });
});

describe('date display (date_display_format global)', () => {
  it.each([
    [0, '1980-06-15'],
    [1, '06/15/1980'],
    [2, '15/06/1980'],
  ] as const)('format %i', (fmt, out) => {
    expect(formatShortDate('1980-06-15', fmt)).toBe(out);
  });

  it('returns short or missing input unchanged, like oeFormatShortDate', () => {
    expect(formatShortDate('1980', 1)).toBe('1980');
    expect(formatShortDate(undefined, 0)).toBe('');
  });

  it('parses the configured format and defaults to 0 (YYYY-MM-DD)', () => {
    expect(parseDateDisplayFormat('1')).toBe(1);
    expect(parseDateDisplayFormat('2')).toBe(2);
    expect(parseDateDisplayFormat(undefined)).toBe(0);
    expect(parseDateDisplayFormat('us')).toBe(0);
  });
});

describe('age (PatientService::getPatientAge)', () => {
  it.each([
    ['1980-06-15', '46'],
    ['1980-09-27', '46'], // birthday today
    ['1980-09-28', '45'], // birthday tomorrow
    ['1980-10-01', '45'],
    ['2024-08-27', '2'], // 25 months
    ['2024-09-27', '24 month'], // exactly 24 months: still months (> 24 is years)
    ['2025-01-10', '20 month'],
    ['2025-01-28', '19 month'], // day not reached yet this month
    ['2026-09-01', '0 month'],
  ])('born %s -> "%s"', (dob, age) => {
    expect(patientAgeDisplay(dob, TODAY)).toBe(age);
  });

  it('is empty without a birth date', () => {
    expect(patientAgeDisplay(undefined, TODAY)).toBe('');
  });
});

describe('age at death (oeFormatAge, format 0)', () => {
  it.each([
    ['1930-03-20', '2001-03-19', '70'],
    ['1930-03-20', '2001-03-20', '71'],
    ['1999-01-15', '2001-01-15', '2'], // exactly 24 months: years (>= 24)
    ['2000-01-15', '2001-01-14', '11 months'],
    ['2000-01-15', '2000-02-15', '1 month'],
  ])('born %s, died %s -> "%s"', (dob, death, age) => {
    expect(ageAtDeathDisplay(dob, death)).toBe(age);
  });
});

describe('deceased and the DOB / age line', () => {
  it('takes the date part of deceasedDateTime', () => {
    expect(deceasedDate(patientDeceased)).toBe('2001-03-19');
    expect(deceasedDate(patientA)).toBeUndefined();
  });

  it('reads "DOB: <date> Age: <age>" for a living patient', () => {
    expect(patientDobAgeLine(patientA, { today: TODAY, dateFormat: 0 })).toBe('DOB: 1980-06-15 Age: 46');
    expect(patientDobAgeLine(patientA, { today: TODAY, dateFormat: 1 })).toBe('DOB: 06/15/1980 Age: 46');
  });

  it('reads "DOB: <date> Age at death: <age>" for a deceased patient', () => {
    expect(patientDobAgeLine(patientDeceased, { today: TODAY, dateFormat: 0 })).toBe('DOB: 1930-03-20 Age at death: 70');
  });

  it('says the age at death is unknown when only deceasedBoolean is set', () => {
    const p: Patient = { resourceType: 'Patient', birthDate: '1930-03-20', deceasedBoolean: true };
    expect(patientDobAgeLine(p, { today: TODAY, dateFormat: 0 })).toBe('DOB: 1930-03-20 Age at death: unknown');
  });
});

describe('sex (FHIR Patient.gender, from OpenEMR patient_data.sex, labelled "Birth Sex" in Demographics)', () => {
  it.each([
    ['male', 'Male'],
    ['female', 'Female'],
    ['other', 'Other'],
    ['unknown', 'Unknown'],
  ] as const)('%s -> %s', (gender, label) => {
    expect(patientSexLabel({ resourceType: 'Patient', gender })).toBe(label);
  });

  it('is empty when gender is absent', () => {
    expect(patientSexLabel({ resourceType: 'Patient' })).toBe('');
  });
});
