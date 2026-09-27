import { describe, expect, it } from 'vitest';
import { matchFhirRequest, matchStdMedicationRequest } from '../src/allowlist.js';

const PID = 'a2c3ab57-cdd6-4aad-afc9-e19c171e7ed7';
const q = (s: string) => new URLSearchParams(s);

describe('FHIR allow-list', () => {
  it.each([
    [`Patient/${PID}`, ''],
    ['Patient', 'name=Demo'],
    ['Patient', 'birthdate=1970-01-01'],
    ['Patient', 'identifier=7'],
    ['Patient', 'name=Demo&birthdate=1970-01-01'],
    ['AllergyIntolerance', `patient=${PID}`],
    ['Condition', `patient=${PID}`],
    ['Condition', `patient=${PID}&category=problem-list-item`],
    ['Condition', `patient=${PID}&category=encounter-diagnosis`],
    ['MedicationRequest', `patient=${PID}`],
    ['CareTeam', `patient=${PID}`],
    ['Observation', `patient=${PID}&category=laboratory`],
    ['Practitioner/prac-1', ''],
    ['Organization/org-1', ''],
  ])('allows GET %s?%s', (path, query) => {
    const r = matchFhirRequest(path, q(query));
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.upstreamPath).toBe(path);
      expect(r.query.toString()).toBe(q(query).toString());
    }
  });

  it.each([
    'Encounter',
    'Immunization',
    'DocumentReference',
    'Patient/x/$everything',
    'Patient/$export',
    '$export',
    'metadata',
    'Practitioner',
    'Organization',
    'Condition/abc',
    'Patient/../Encounter',
    'Patient/a/b',
    '',
    'patient',
  ])('404s for a route that is not allow-listed: %s', (path) => {
    const r = matchFhirRequest(path, q(''));
    expect(r).toEqual({ ok: false, status: 404, error: 'not_found' });
  });

  it.each([
    ['Patient', ''],
    ['Patient', '_count=50'],
    ['Patient', 'name=Demo&_count=50'],
    ['Patient', 'family=Demo'],
    ['Patient', 'name=Demo&name=Other'],
    ['Patient', 'name='],
    ['Patient', 'name=%3Cscript%3E'],
    ['Patient', 'birthdate=yesterday'],
    [`Patient/${PID}`, '_format=xml'],
    ['AllergyIntolerance', ''],
    ['AllergyIntolerance', 'patient=bad/id'],
    ['AllergyIntolerance', `patient=${PID}&_include=*`],
    ['Observation', `patient=${PID}`],
    ['Observation', `patient=${PID}&category=vital-signs`],
    ['Observation', `patient=${PID}&category=laboratory,vital-signs`],
    ['Condition', `patient=${PID}&category=anything`],
    ['MedicationRequest', `patient=${PID}&_revinclude=Provenance:target`],
  ])('400s for a parameter that is not allow-listed: %s?%s', (path, query) => {
    const r = matchFhirRequest(path, q(query));
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.status).toBe(400);
  });
});

describe('standard API allow-list', () => {
  it('allows /patient/:pid/medication with a numeric pid and no params', () => {
    const r = matchStdMedicationRequest('7', q(''));
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.upstreamPath).toBe('patient/7/medication');
      expect(r.query.toString()).toBe('');
    }
  });

  it('rejects non-numeric pids and any query param', () => {
    expect(matchStdMedicationRequest('abc', q(''))).toMatchObject({ ok: false, status: 404 });
    expect(matchStdMedicationRequest('7', q('x=1'))).toMatchObject({ ok: false, status: 400 });
  });
});
