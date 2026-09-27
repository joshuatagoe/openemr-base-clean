import { describe, expect, it, vi } from 'vitest';
import { allergyRows } from '../src/fhir/allergy';
import { careTeamView, pickCareTeam } from '../src/fhir/careTeam';
import { problemRows } from '../src/fhir/condition';
import { combinedRows, isActiveIssue, medicationListRows, prescriptionTable } from '../src/fhir/medication';
import { guardSubject, isForPatient } from '../src/fhir/subject';
import {
  allergy,
  careTeam,
  condition,
  facilityParticipant,
  medReq,
  ORG_1,
  PATIENT_A_ID,
  PATIENT_B_ID,
  PRACT_1,
  PRACT_2,
  practitionerParticipant,
  RELATED_1,
  stdMed,
} from './fixtures/clinical';

const NOW = new Date('2026-09-27T12:00:00');

describe('subject guard', () => {
  it('accepts relative and absolute references to the selected patient only', () => {
    expect(isForPatient(`Patient/${PATIENT_A_ID}`, PATIENT_A_ID)).toBe(true);
    expect(isForPatient(`https://openemr.invalid/apis/default/fhir/Patient/${PATIENT_A_ID}`, PATIENT_A_ID)).toBe(true);
    expect(isForPatient(`Patient/${PATIENT_B_ID}`, PATIENT_A_ID)).toBe(false);
    expect(isForPatient(`Patient/x${PATIENT_A_ID}`, PATIENT_A_ID)).toBe(false);
    expect(isForPatient(`Group/${PATIENT_A_ID}`, PATIENT_A_ID)).toBe(false);
    expect(isForPatient(undefined, PATIENT_A_ID)).toBe(false);
  });

  it('drops mismatches and logs only a count and the resource type', () => {
    const log = vi.fn();
    const items = [allergy('a1', 'Ours'), allergy('a2', 'Theirs', { patient: PATIENT_B_ID }), allergy('a3', 'Also theirs', { patient: PATIENT_B_ID })];
    const kept = guardSubject(items, PATIENT_A_ID, (a) => a.patient?.reference, 'AllergyIntolerance', log);
    expect(kept.map((a) => a.id)).toEqual(['a1']);
    expect(log).toHaveBeenCalledTimes(1);
    const msg = String(log.mock.calls[0]?.[0]);
    expect(msg).toContain('2');
    expect(msg).toContain('AllergyIntolerance');
    expect(msg).not.toContain(PATIENT_B_ID);
    expect(msg).not.toContain('Theirs');
  });

  it('does not log when nothing is dropped', () => {
    const log = vi.fn();
    guardSubject([allergy('a1', 'Ours')], PATIENT_A_ID, (a) => a.patient?.reference, 'AllergyIntolerance', log);
    expect(log).not.toHaveBeenCalled();
  });
});

describe('allergies (PHP allergies.html.twig parity)', () => {
  it('shows the narrative title and the criticality in brackets, highlighting high risk, keeping server order', () => {
    const rows = allergyRows([
      allergy('a1', 'Penicillin', { criticality: 'low', reaction: 'Rash' }),
      allergy('a2', 'Peanut', { criticality: 'high', reaction: 'Anaphylaxis' }),
      allergy('a3', 'Latex', { criticality: 'unable-to-assess' }),
      allergy('a4', 'Dust'),
    ]);
    expect(rows.map((r) => [r.name, r.severity, r.highlight])).toEqual([
      ['Penicillin', 'Low Risk', false],
      ['Peanut', 'High Risk', true],
      ['Latex', 'Unassigned', false],
      ['Dust', '', false],
    ]);
    expect(rows[0]?.tooltip).toBe('Penicillin Reaction: Rash - Low Risk');
    expect(rows[3]?.tooltip).toBe('Dust Reaction:  - ');
  });

  it('reads the unescaped narrative as text, never as markup', () => {
    const rows = allergyRows([allergy('a1', 'Eggs & <b>dairy</b>')]);
    expect(rows[0]?.name).toBe('Eggs & dairy');
  });

  it('falls back to code text / display when the narrative is missing', () => {
    const a = allergy('a1', 'x');
    delete a.text;
    a.code = { text: 'Shellfish' };
    expect(allergyRows([a])[0]?.name).toBe('Shellfish');
  });

  it('keeps active allergies only (PHP hides resolved and ended ones) and drops entered-in-error', () => {
    const rows = allergyRows([
      allergy('a1', 'Active one'),
      allergy('a2', 'Resolved one', { status: 'resolved' }),
      allergy('a3', 'Ended one', { status: 'inactive' }),
      allergy('a4', 'Mistake', { verification: 'entered-in-error' }),
    ]);
    expect(rows.map((r) => r.name)).toEqual(['Active one']);
  });
});

describe('problem list (both categories, de-duplicated)', () => {
  const icd = (code: string, display: string) => ({ system: 'http://hl7.org/fhir/sid/icd-10-cm', code, display });

  it('merges per-encounter copies of the same problem and keeps distinct problems', () => {
    const rows = problemRows([
      condition('p1', 'problem-list-item', { text: 'Asthma', onset: '2023-07-06T20:30:00+00:00' }),
      // One lists row linked to two encounters → two encounter-diagnosis Conditions.
      condition('e1', 'encounter-diagnosis', { coding: icd('E11.9', 'Type 2 diabetes'), onset: '2018-05-08T19:37:00+00:00' }),
      condition('e2', 'encounter-diagnosis', { coding: icd('E11.9', 'Type 2 diabetes'), onset: '2018-05-08T19:37:00+00:00' }),
      // Same code, different onset: a different lists row.
      condition('e3', 'encounter-diagnosis', { coding: icd('E11.9', 'Type 2 diabetes'), onset: '2024-01-01T00:00:00+00:00' }),
      // Same id twice (pre-2025-11-15 encounter-diagnosis ids are the lists uuid).
      condition('e4', 'encounter-diagnosis', { text: 'Gout', onset: '2020-02-02T00:00:00+00:00' }),
      condition('e4', 'encounter-diagnosis', { text: 'Gout', onset: '2020-02-02T00:00:00+00:00' }),
    ]);
    expect(rows.map((r) => r.title)).toEqual(['Type 2 diabetes', 'Gout', 'Asthma', 'Type 2 diabetes']);
  });

  it('orders by onset ascending with no onset first (MySQL NULLs first), stable otherwise', () => {
    const rows = problemRows([
      condition('p1', 'problem-list-item', { text: 'Later', onset: '2024-01-01T00:00:00+00:00' }),
      condition('p2', 'problem-list-item', { text: 'No onset A' }),
      condition('p3', 'problem-list-item', { text: 'Earlier', onset: '2001-01-01T00:00:00+00:00' }),
      condition('p4', 'problem-list-item', { text: 'No onset B' }),
    ]);
    expect(rows.map((r) => r.title)).toEqual(['No onset A', 'No onset B', 'Earlier', 'Later']);
  });

  it('hides inactive (ended) and entered-in-error problems; keeps resolved ones, marked', () => {
    const rows = problemRows([
      condition('p1', 'problem-list-item', { text: 'Ongoing', onset: '2020-01-01T00:00:00+00:00' }),
      condition('p2', 'problem-list-item', { text: 'Ended', status: 'inactive', onset: '2019-01-01T00:00:00+00:00' }),
      condition('p3', 'problem-list-item', { text: 'First episode', status: 'resolved', onset: '2021-01-01T00:00:00+00:00' }),
      condition('p4', 'problem-list-item', { text: 'Recurs', status: 'recurrence', onset: '2022-01-01T00:00:00+00:00' }),
      condition('p5', 'problem-list-item', { text: 'Wrong chart', verification: 'entered-in-error' }),
    ]);
    expect(rows.map((r) => [r.title, r.resolved])).toEqual([
      ['Ongoing', false],
      ['First episode', true],
      ['Recurs', false],
    ]);
  });

  it('a merged problem is shown if any copy is still open', () => {
    const rows = problemRows([
      condition('e1', 'encounter-diagnosis', { text: 'Back pain', onset: '2020-01-01T00:00:00+00:00', status: 'resolved' }),
      condition('e2', 'encounter-diagnosis', { text: 'Back pain', onset: '2020-01-01T00:00:00+00:00', status: 'active' }),
    ]);
    expect(rows).toEqual([expect.objectContaining({ title: 'Back pain', resolved: false })]);
  });
});

describe('medications vs prescriptions (uuid split)', () => {
  it('PHP issue filter: outcome is not Resolved and the end date is empty or in the future', () => {
    expect(isActiveIssue(stdMed('u', 't'), NOW)).toBe(true);
    expect(isActiveIssue(stdMed('u', 't', { outcome: '1' }), NOW)).toBe(false);
    expect(isActiveIssue(stdMed('u', 't', { enddate: '2020-01-01 00:00:00' }), NOW)).toBe(false);
    expect(isActiveIssue(stdMed('u', 't', { enddate: '2030-01-01 00:00:00' }), NOW)).toBe(true);
    expect(isActiveIssue(stdMed('u', 't', { enddate: '' }), NOW)).toBe(true);
  });

  it('Medications: list rows ordered by begdate ascending (no begdate first), dosage from the matching MedicationRequest', () => {
    const rows = medicationListRows(
      [
        stdMed('u-late', 'Atorvastatin 20 mg', { id: '11', begdate: '2025-03-01 00:00:00' }),
        stdMed('u-early', 'Lisinopril 20 mg', { id: '12', begdate: '2019-08-01 00:00:00' }),
        stdMed('u-none', 'Vitamin D', { id: '13', begdate: null }),
        stdMed('u-ended', 'Old drug', { id: '14', enddate: '2020-01-01 00:00:00' }),
        // Linked to a prescription: OpenEMR's FHIR leaves it out, the PHP card still lists it.
        stdMed('u-linked', 'Metformin 500 mg', { id: '15', begdate: '2024-01-01 00:00:00' }),
      ],
      [medReq('u-late', 'Atorvastatin 20 mg', { sig: '1 tab nightly' }), medReq('u-early', 'Lisinopril 20 mg', { sig: '1 tab daily' })],
      NOW,
    );
    expect(rows.map((r) => [r.title, r.dosage])).toEqual([
      ['Vitamin D', ''],
      ['Lisinopril 20 mg', '1 tab daily'],
      ['Metformin 500 mg', ''],
      ['Atorvastatin 20 mg', '1 tab nightly'],
    ]);
  });

  it('Prescriptions: every MedicationRequest not on the list; active and completed only; newest modified first', () => {
    const table = prescriptionTable(
      [
        medReq('u-list', 'List med'),
        medReq('rx-old', 'Metformin HCl 500 mg', { sig: '1 tab BID', qty: 60, authoredOn: '2026-01-15T09:00:00-05:00', lastUpdated: '2026-01-15T14:00:00+00:00' }),
        medReq('rx-new', 'Amoxicillin 500 mg', {
          dose: { value: 500, unit: 'mg' },
          sig: '1 tab',
          timing: 'b.i.d.',
          qty: 30,
          authoredOn: '2026-05-01T09:00:00+00:00',
          lastUpdated: '2026-05-02T09:00:00+00:00',
        }),
        medReq('rx-done', 'Prednisone 10 mg', { status: 'completed', authoredOn: '2026-02-01T09:00:00+00:00', lastUpdated: '2026-02-01T09:00:00+00:00' }),
        medReq('rx-stopped', 'Stopped drug', { status: 'stopped' }),
      ],
      new Set(['u-list']),
    );
    expect(table.total).toBe(4);
    expect(table.rows.map((r) => [r.drug, r.details, r.qty, r.filled])).toEqual([
      ['Amoxicillin 500 mg', '500mg 1 tab b.i.d.', '30', '2026-05-01 09:00:00'],
      ['Prednisone 10 mg', '', '', '2026-02-01 09:00:00'],
      ['Metformin HCl 500 mg', '1 tab BID', '60', '2026-01-15 09:00:00'],
    ]);
  });

  it('Prescriptions: total 0 when there are none at all (PHP prints "None"); total > 0 with no rows when all are inactive', () => {
    expect(prescriptionTable([], new Set()).total).toBe(0);
    const t = prescriptionTable([medReq('rx', 'Stopped', { status: 'stopped' })], new Set());
    expect(t.total).toBe(1);
    expect(t.rows).toEqual([]);
  });

  it('Filled: the server-local date_added as PHP prints it; a date-only value stays as is', () => {
    const t = prescriptionTable([medReq('a', 'A', { authoredOn: '2026-01-15' })], new Set());
    expect(t.rows[0]?.filled).toBe('2026-01-15');
  });

  it('uses the RxNorm display when the drug is coded', () => {
    const t = prescriptionTable([medReq('rx', 'Metformin 500 MG Oral Tablet', { rxnorm: '860975' })], new Set());
    expect(t.rows[0]?.drug).toBe('Metformin 500 MG Oral Tablet');
  });

  it('combined fallback lists every active/completed MedicationRequest without guessing which is which', () => {
    const rows = combinedRows([medReq('a', 'Drug A', { sig: '1 daily' }), medReq('b', 'Drug B', { status: 'stopped' }), medReq('c', 'Drug C', { status: 'completed' })]);
    expect(rows.map((r) => [r.name, r.dosage])).toEqual([
      ['Drug A', '1 daily'],
      ['Drug C', ''],
    ]);
  });
});

describe('care team', () => {
  it('picks the first active team, else the first that is not entered-in-error', () => {
    const proposed = careTeam('t1', { status: 'proposed', name: 'Proposed' });
    const active = careTeam('t2', { status: 'active', name: 'Active' });
    const error = careTeam('t0', { status: 'entered-in-error', name: 'Error' });
    expect(pickCareTeam([error, proposed, active])?.id).toBe('t2');
    expect(pickCareTeam([error, proposed])?.id).toBe('t1');
    expect(pickCareTeam([error])).toBeUndefined();
    expect(pickCareTeam([])).toBeUndefined();
  });

  it('lists people only: the duplicated facility participants are skipped; role, facility and since come from FHIR', () => {
    const view = careTeamView(
      careTeam('t1', {
        name: 'Primary team',
        participants: [
          practitionerParticipant(PRACT_1, { role: 'Family medicine', org: ORG_1, since: '2024-03-01' }),
          facilityParticipant(ORG_1),
          practitionerParticipant(PRACT_2, { role: 'Cardiology' }),
          { member: { reference: `RelatedPerson/${RELATED_1}` }, role: [{ text: 'Mother' }] },
          facilityParticipant(ORG_1),
        ],
      }),
    );
    expect(view.name).toBe('Primary team');
    expect(view.statusLabel).toBe('Active');
    expect(view.badgeClass).toBe('badge-success');
    expect(view.members).toEqual([
      expect.objectContaining({ type: 'provider', memberId: PRACT_1, role: 'Family medicine', facilityId: ORG_1, since: '2024-03-01' }),
      expect.objectContaining({ type: 'provider', memberId: PRACT_2, role: 'Cardiology', facilityId: undefined, since: '' }),
      expect.objectContaining({ type: 'related', memberId: RELATED_1, role: 'Mother' }),
    ]);
  });

  it('maps team status to the PHP badge classes', () => {
    expect(careTeamView(careTeam('t', { status: 'inactive', name: 'n' })).badgeClass).toBe('badge-warning');
    expect(careTeamView(careTeam('t', { status: 'proposed', name: 'n' })).badgeClass).toBe('badge-info');
    expect(careTeamView(careTeam('t', { status: 'suspended', name: 'n' })).badgeClass).toBe('badge-secondary');
  });
});
