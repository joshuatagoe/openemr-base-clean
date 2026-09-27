import { describe, expect, it } from 'vitest';
import { matchFhirRequest, matchStdMedicationRequest, matchStdPatientRequest } from '../src/allowlist.js';

const PID = 'a2c3ab57-cdd6-4aad-afc9-e19c171e7ed7';
const q = (s: string) => new URLSearchParams(s);

describe('FHIR allow-list', () => {
  it.each([
    [`Patient/${PID}`, ''],
    ['Patient', 'name=Demo'],
    ['Patient', 'birthdate=1970-01-01'],
    ['Patient', 'identifier=7'],
    ['Patient', 'name=Demo&birthdate=1970-01-01'],
    // The patient list (landing page): a bare search bounded by _count.
    ['Patient', '_count=21'],
    ['Patient', '_count=1'],
    ['Patient', '_count=50'],
    // Page size 100 (Finder's largest "Show" option) plus the one probe row.
    ['Patient', '_count=100'],
    ['Patient', '_count=101'],
    ['Patient', '_count=101&_sort=phone'],
    ['Patient', '_count=101&_sort=-phone'],
    ['Patient', '_count=101&_sort=identifier'],
    ['Patient', '_count=101&_sort=-identifier'],
    // Finder columns: Home Phone (phone), SSN / External ID (identifier).
    ['Patient', 'phone=555-0100'],
    ['Patient', 'phone=(555) 010-0100'],
    ['Patient', 'phone=+1 555.010.0100'],
    ['Patient', 'phone=5550100&_count=101&_offset=0&_sort=family,given'],
    ['Patient', 'identifier=900-11-2222&_count=101'],
    ['Patient', 'name=Demo&phone=555-0100&birthdate=1970-01-01&identifier=SYN-1'],
    // The Finder's "Search with exact method": a whole-name match.
    ['Patient', 'name:exact=Demo&_count=11&_offset=0&_sort=family,given'],
    ['Patient', 'name:exact=Demo'],
    ['Patient', '_count=21&_offset=0&_sort=family,given'],
    ['Patient', '_count=21&_offset=40&_sort=family'],
    ['Patient', '_count=21&_sort=-family,-given'],
    ['Patient', '_count=21&_sort=birthdate'],
    ['Patient', '_count=21&_sort=-birthdate'],
    ['Patient', 'name=Demo&_count=21&_offset=20&_sort=family,given'],
    ['Patient', 'name=Demo&_count=50'],
    ['Patient', 'identifier=7&_offset=999999'],
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
    // Paging alone does not bound the list: a bare search needs _count.
    ['Patient', '_offset=20'],
    ['Patient', '_sort=family'],
    ['Patient', '_offset=0&_sort=family'],
    ['Patient', '_count=0'],
    ['Patient', '_count=102'],
    ['Patient', '_count=1000'],
    ['Patient', '_count=500'],
    ['Patient', '_count=-1'],
    ['Patient', '_count=020'],
    ['Patient', '_count=2.5'],
    ['Patient', '_count=abc'],
    ['Patient', '_count=21&_count=21'],
    ['Patient', '_count=21&_offset=-20'],
    ['Patient', '_count=21&_offset=1000000'],
    ['Patient', '_count=21&_offset=01'],
    ['Patient', '_count=21&_offset=1e3'],
    ['Patient', '_count=21&_sort=family,phone'],
    ['Patient', '_count=21&_sort=_id'],
    ['Patient', '_count=21&_sort='],
    ['Patient', '_count=21&_maxresults=5'],
    ['Patient', '_count=21&_limit=5'],
    ['Patient', '_count=21&gender=male'],
    ['Patient', 'phone='],
    ['Patient', 'phone=555-0100&phone=555-0101'],
    ['Patient', 'phone=abc'],
    ['Patient', 'phone=555-0100x12'],
    ['Patient', 'phone=---'],
    ['Patient', 'phone=()'],
    ['Patient', 'phone=555|0100'],
    ['Patient', 'phone=555%2C0100'],
    ['Patient', 'phone=%3Cscript%3E'],
    ['Patient', `phone=${'5'.repeat(33)}`],
    ['Patient', 'phone:contains=555'],
    ['Patient', 'name:contains=Dem'],
    ['Patient', 'name:exact=<b>'],
    ['Patient', 'name:exact='],
    ['Patient', 'name=Demo&name:exact=Demo'],
    ['Patient', 'telecom=555-0100'],
    ['Patient', '_count=21&_sort=telecom'],
    ['Patient', '_count=21&_sort=gender'],
    ['Patient', '_count=21&_sort=phone,family'],
    ['Patient', '_count=21&_summary=count'],
    ['Patient', '_count=21&_elements=name'],
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

describe('standard API patient read (uuid -> pid)', () => {
  it('allows /patient/:puuid with a uuid and no params', () => {
    const r = matchStdPatientRequest(PID, q(''));
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.upstreamPath).toBe(`patient/${PID}`);
      expect(r.query.toString()).toBe('');
      expect(r.kind).toBe('read');
    }
  });

  it.each(['7', 'abc', 'prac-1', `${PID}x`, `${PID}/medication`, '..', ''])('404s a puuid that is not a uuid: %s', (id) => {
    expect(matchStdPatientRequest(id, q(''))).toEqual({ ok: false, status: 404, error: 'not_found' });
  });

  it.each(['x=1', '_format=xml', 'fields=ss'])('400s any query parameter: %s', (query) => {
    expect(matchStdPatientRequest(PID, q(query))).toMatchObject({ ok: false, status: 400 });
  });
});
