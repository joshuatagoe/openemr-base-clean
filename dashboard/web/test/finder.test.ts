// The Patient Finder's request planning and client-side steps (data/finder.ts):
// which FHIR searches a Finder state becomes, and how rows are refined, merged,
// sorted and paged when the server can't do it.
import type { Patient } from 'fhir/r4';
import { describe, expect, it } from 'vitest';
import {
  finderPage,
  globalSearchKeys,
  homePhone,
  mergeCandidates,
  planFinder,
  sortPatients,
  ssnOf,
  type FinderState,
} from '../src/data/finder';
import { mrn, ssn } from './fixtures/patients';

const base: FinderState = { filters: {}, search: '', sort: { key: 'name', dir: 'asc' }, page: 1, pageSize: 10 };

function pt(id: string, over: Partial<Patient> & { family?: string; given?: string[]; ss?: string; ext?: string; home?: string; work?: string } = {}): Patient {
  const { family = 'Fam', given = ['Giv'], ss, ext, home, work, ...rest } = over;
  const telecom = [
    ...(home ? [{ system: 'phone' as const, value: home, use: 'home' as const }] : []),
    ...(work ? [{ system: 'phone' as const, value: work, use: 'work' as const }] : []),
  ];
  return {
    resourceType: 'Patient',
    id,
    name: [{ use: 'official', family, given }],
    identifier: [...(ss ? [ssn(ss)] : []), ...(ext ? [mrn(ext)] : [])],
    ...(telecom.length ? { telecom } : {}),
    ...rest,
  };
}

describe('planFinder', () => {
  it('no search, no refined filter: one server-paged request, one probe row', () => {
    expect(planFinder(base)).toEqual({ mode: 'server', params: { _count: '11', _offset: '0', _sort: 'family,given' } });
    expect(planFinder({ ...base, page: 3, pageSize: 100, sort: { key: 'dob', dir: 'desc' } })).toEqual({
      mode: 'server',
      params: { _count: '101', _offset: '200', _sort: '-birthdate' },
    });
  });

  it('maps each sortable column to the _sort OpenEMR honours', () => {
    const sortOf = (key: 'name' | 'phone' | 'dob' | 'externalId', dir: 'asc' | 'desc') => {
      const plan = planFinder({ ...base, sort: { key, dir } });
      return plan.mode === 'server' ? plan.params._sort : '';
    };
    expect(sortOf('name', 'asc')).toBe('family,given');
    expect(sortOf('name', 'desc')).toBe('-family,-given');
    expect(sortOf('phone', 'asc')).toBe('phone');
    expect(sortOf('phone', 'desc')).toBe('-phone');
    expect(sortOf('dob', 'asc')).toBe('birthdate');
    expect(sortOf('externalId', 'desc')).toBe('-identifier');
  });

  it('name and date of birth filters stay server-paged', () => {
    expect(planFinder({ ...base, filters: { name: 'Sample', birthdate: '1980-06-15' } })).toEqual({
      mode: 'server',
      params: { name: 'Sample', birthdate: '1980-06-15', _count: '11', _offset: '0', _sort: 'family,given' },
    });
  });

  it('SSN, External ID and Home Phone filters fetch up to 101 candidates and refine them in the browser', () => {
    const plan = planFinder({ ...base, filters: { ssn: '900-11-2222', name: 'Ada' }, page: 2 });
    expect(plan).toMatchObject({ mode: 'client', requests: [{ name: 'Ada', identifier: '900-11-2222', _count: '101', _sort: 'family,given' }] });
    // SSN and External ID both use `identifier`: one request, both checked in the browser.
    const both = planFinder({ ...base, filters: { ssn: '900-11-2222', externalId: 'SYN-1001', phone: '555-0100' } });
    expect(both).toMatchObject({ mode: 'client', requests: [{ identifier: '900-11-2222', phone: '555-0100', _count: '101' }] });
  });

  it('the global search runs one request per field the text could be, never more than three', () => {
    const keys = (s: string) => globalSearchKeys(s);
    expect(keys('Smith')).toEqual(['name', 'identifier']);
    expect(keys("O'Brien")).toEqual(['name']);
    expect(keys('555-0100')).toEqual(['identifier', 'phone']);
    expect(keys('(555) 010-0100')).toEqual(['phone']);
    expect(keys('1980')).toEqual(['identifier', 'phone', 'birthdate']);
    expect(keys('1980-06-15')).toEqual(['identifier', 'phone', 'birthdate']);
    expect(keys('SYN-1001')).toEqual(['identifier']);
    expect(keys('<b>')).toEqual([]);
    expect(keys('a|b')).toEqual([]);

    const plan = planFinder({ ...base, search: 'Smith', filters: { birthdate: '1980-06-15' } });
    expect(plan).toMatchObject({
      mode: 'client',
      requests: [
        { name: 'Smith', birthdate: '1980-06-15', _count: '101', _sort: 'family,given' },
        { identifier: 'Smith', birthdate: '1980-06-15', _count: '101', _sort: 'family,given' },
      ],
    });
  });

  it('a global branch that collides with a column filter sends the search text and checks the filter in the browser', () => {
    const plan = planFinder({ ...base, search: 'Smith', filters: { name: 'Ann' } });
    if (plan.mode !== 'client') throw new Error('expected client mode');
    expect(plan.requests[0]).toMatchObject({ name: 'Smith' });
    expect(plan.requests[1]).toMatchObject({ identifier: 'Smith', name: 'Ann' });
    const annSmith = pt('1', { family: 'Smith', given: ['Ann'] });
    const bobSmith = pt('2', { family: 'Smith', given: ['Bob'] });
    expect([annSmith, bobSmith].filter(plan.keep).map((p) => p.id)).toEqual(['1']);
  });
});

describe('Search with exact method', () => {
  it('sends name:exact for the name filter and the global name branch, and checks whole names in the browser', () => {
    expect(planFinder({ ...base, exact: true, filters: { name: 'Ann' } })).toMatchObject({ mode: 'server', params: { 'name:exact': 'Ann' } });
    const plan = planFinder({ ...base, exact: true, search: 'Smith', filters: { name: 'Ann' } });
    if (plan.mode !== 'client') throw new Error('expected client mode');
    expect(plan.requests[0]).toEqual({ 'name:exact': 'Smith', _count: '101', _sort: 'family,given' });
    expect(plan.requests[1]).toMatchObject({ identifier: 'Smith', 'name:exact': 'Ann' });
    const ann = pt('1', { family: 'Smith', given: ['Ann'] });
    const anna = pt('2', { family: 'Smith', given: ['Anna'] });
    expect([ann, anna].filter(plan.keep).map((p) => p.id)).toEqual(['1']);
  });
});

describe('refinement (client mode keep)', () => {
  it('SSN keeps only the SS identifier, External ID only the PT identifier', () => {
    const a = pt('a', { ss: '900-11-2222', ext: 'SYN-1001' });
    const b = pt('b', { ss: '111-11-1111', ext: '900-11-2222' }); // the External ID equals the searched SSN
    const plan = planFinder({ ...base, filters: { ssn: '900-11-2222' } });
    if (plan.mode !== 'client') throw new Error('expected client mode');
    expect([a, b].filter(plan.keep).map((p) => p.id)).toEqual(['a']);

    const ext = planFinder({ ...base, filters: { externalId: '900-11-2222' } });
    if (ext.mode !== 'client') throw new Error('expected client mode');
    expect([a, b].filter(ext.keep).map((p) => p.id)).toEqual(['b']);
    // Case-insensitive, like MySQL's comparison.
    const lower = planFinder({ ...base, filters: { externalId: 'syn-1001' } });
    if (lower.mode !== 'client') throw new Error('expected client mode');
    expect([a, b].filter(lower.keep).map((p) => p.id)).toEqual(['a']);
  });

  it('Home Phone keeps the home number only (OpenEMR phone also matches work and mobile)', () => {
    const home = pt('h', { home: '555-0100' });
    const work = pt('w', { home: '555-0199', work: '555-0100' });
    const plan = planFinder({ ...base, filters: { phone: '555-0100' } });
    if (plan.mode !== 'client') throw new Error('expected client mode');
    expect([home, work].filter(plan.keep).map((p) => p.id)).toEqual(['h']);
  });
});

describe('merge, sort, page', () => {
  it('merges the branches, de-duplicates by id, and keeps at most 100', () => {
    const a = pt('a');
    const b = pt('b');
    const merged = mergeCandidates([[a, b], [b, pt('c')]]);
    expect(merged.patients.map((p) => p.id)).toEqual(['a', 'b', 'c']);
    expect(merged.truncated).toBe(false);

    const many = Array.from({ length: 101 }, (_, i) => pt(`p${i}`));
    expect(mergeCandidates([many]).truncated).toBe(true);
    const twoHalves = mergeCandidates([many.slice(0, 60), many.slice(50, 100)]);
    expect(twoHalves.patients).toHaveLength(100);
    expect(twoHalves.truncated).toBe(false);
    // Two branches of 60 and 50 distinct rows: all read, but only 100 are shown.
    const over = mergeCandidates([many.slice(0, 60), many.slice(60, 101)]);
    expect(over.truncated).toBe(false);
    expect(finderPage(over.patients, 10, 10, over.truncated)).toMatchObject({ truncated: true, hasNext: false, total: 100 });
  });

  it('sorts like OpenEMR: last, first, middle name; phone; DOB; External ID; blanks first ascending', () => {
    const rows = [
      pt('1', { family: 'beta', given: ['Zed'], birthDate: '1990-01-01', home: '555-0300', ext: 'B2' }),
      pt('2', { family: 'Alpha', given: ['Yan', 'Q'], birthDate: '1970-01-01', ext: 'a1' }),
      pt('3', { family: 'Alpha', given: ['Yan', 'P'], home: '555-0200', ext: 'C3' }),
    ];
    const ids = (key: 'name' | 'phone' | 'dob' | 'externalId', dir: 'asc' | 'desc') => sortPatients(rows, { key, dir }).map((p) => p.id);
    expect(ids('name', 'asc')).toEqual(['3', '2', '1']);
    expect(ids('name', 'desc')).toEqual(['1', '2', '3']);
    expect(ids('phone', 'asc')).toEqual(['2', '3', '1']);
    expect(ids('dob', 'asc')).toEqual(['3', '2', '1']);
    expect(ids('dob', 'desc')).toEqual(['1', '2', '3']);
    expect(ids('externalId', 'asc')).toEqual(['2', '1', '3']);
  });

  it('pages a client-side list and knows its total', () => {
    const list = Array.from({ length: 23 }, (_, i) => pt(`p${i}`));
    expect(finderPage(list, 1, 10, false)).toMatchObject({ hasNext: true, total: 23 });
    expect(finderPage(list, 3, 10, false).patients).toHaveLength(3);
    expect(finderPage(list, 3, 10, false)).toMatchObject({ hasNext: false, truncated: false });
  });

  it('reads the home phone and the SSN by their codes', () => {
    const p = pt('x', { home: '555-0100', work: '555-0999', ss: '900-11-2222', ext: 'E1' });
    expect(homePhone(p)).toBe('555-0100');
    expect(ssnOf(p)).toBe('900-11-2222');
    expect(homePhone(pt('y', { work: '555-0999' }))).toBeUndefined();
  });
});
