import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { loadRecent, RECENT_MAX, recentStorageKey, saveRecent, withRecent } from '../src/recent/recentPatients';
import { PATIENT_A_ID, PATIENT_B_ID } from './fixtures/patients';

const USER = 'https://openemr.invalid/apis/default/fhir/Practitioner/9f000000-0000-4000-8000-0000000000aa';
const OTHER_USER = 'https://openemr.invalid/apis/default/fhir/Practitioner/9f000000-0000-4000-8000-0000000000bb';
const id = (n: number) => `9c000000-0000-4000-8000-${String(n).padStart(12, '0')}`;

beforeEach(() => window.localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe('recent patients: storage key', () => {
  it('is namespaced by a SHA-256 hash of the user id, never the id itself', async () => {
    const key = await recentStorageKey(USER);
    expect(key).toMatch(/^dash\.recentPatients\.v1\.[0-9a-f]{64}$/);
    expect(key).not.toContain('Practitioner');
    expect(key).not.toContain('9f000000');
    expect(await recentStorageKey(USER)).toBe(key);
    expect(await recentStorageKey(OTHER_USER)).not.toBe(key);
  });

  it('returns null when there is no user id or no WebCrypto (the list then lives in memory only)', async () => {
    expect(await recentStorageKey('')).toBeNull();
    vi.spyOn(crypto.subtle, 'digest').mockRejectedValue(new Error('no subtle'));
    expect(await recentStorageKey(USER)).toBeNull();
  });
});

describe('recent patients: list rules', () => {
  it('puts the opened patient first, without duplicates, and keeps at most 10', () => {
    expect(withRecent([], PATIENT_A_ID)).toEqual([PATIENT_A_ID]);
    expect(withRecent([PATIENT_A_ID], PATIENT_B_ID)).toEqual([PATIENT_B_ID, PATIENT_A_ID]);
    expect(withRecent([PATIENT_B_ID, PATIENT_A_ID], PATIENT_A_ID)).toEqual([PATIENT_A_ID, PATIENT_B_ID]);
    const full = Array.from({ length: RECENT_MAX }, (_, i) => id(i));
    const next = withRecent(full, id(99));
    expect(RECENT_MAX).toBe(10);
    expect(next).toHaveLength(10);
    expect(next[0]).toBe(id(99));
    expect(next).not.toContain(id(9));
  });

  it('ignores anything that is not a FHIR id', () => {
    expect(withRecent([PATIENT_A_ID], 'a/b')).toEqual([PATIENT_A_ID]);
    expect(withRecent([PATIENT_A_ID], '')).toEqual([PATIENT_A_ID]);
  });
});

describe('recent patients: storage', () => {
  it('stores FHIR patient ids only, as a JSON array under the hashed key', async () => {
    const key = (await recentStorageKey(USER)) ?? '';
    saveRecent(key, [PATIENT_B_ID, PATIENT_A_ID]);
    expect(window.localStorage.length).toBe(1);
    expect(JSON.parse(window.localStorage.getItem(key) ?? 'null')).toEqual([PATIENT_B_ID, PATIENT_A_ID]);
    expect(loadRecent(key)).toEqual([PATIENT_B_ID, PATIENT_A_ID]);
  });

  it('removes the entry when the list is empty', async () => {
    const key = (await recentStorageKey(USER)) ?? '';
    saveRecent(key, [PATIENT_A_ID]);
    saveRecent(key, []);
    expect(window.localStorage.getItem(key)).toBeNull();
  });

  it('keeps users apart', async () => {
    const mine = (await recentStorageKey(USER)) ?? '';
    const theirs = (await recentStorageKey(OTHER_USER)) ?? '';
    saveRecent(mine, [PATIENT_A_ID]);
    expect(loadRecent(theirs)).toEqual([]);
  });

  it.each([
    ['not JSON', '{oops'],
    ['not an array', '{"ids":["x"]}'],
    ['a string', '"abc"'],
  ])('reads a corrupt entry (%s) as empty', async (_label, raw) => {
    const key = (await recentStorageKey(USER)) ?? '';
    window.localStorage.setItem(key, raw);
    expect(loadRecent(key)).toEqual([]);
  });

  it('drops values that are not FHIR ids (e.g. a name written by something else), duplicates, and anything past 10', async () => {
    const key = (await recentStorageKey(USER)) ?? '';
    const many = Array.from({ length: 12 }, (_, i) => id(i));
    window.localStorage.setItem(key, JSON.stringify([PATIENT_A_ID, 'Ada Samplefamily', 42, PATIENT_A_ID, { id: 'x' }, ...many]));
    const got = loadRecent(key);
    expect(got[0]).toBe(PATIENT_A_ID);
    expect(got).not.toContain('Ada Samplefamily');
    expect(got).toHaveLength(10);
    expect(new Set(got).size).toBe(10);
  });

  it('never throws when storage is unavailable', async () => {
    const key = (await recentStorageKey(USER)) ?? '';
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('full', 'QuotaExceededError');
    });
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError');
    });
    expect(loadRecent(key)).toEqual([]);
    expect(() => saveRecent(key, [PATIENT_A_ID])).not.toThrow();
    expect(() => saveRecent(key, [])).not.toThrow();
  });

  it('never throws when the localStorage accessor itself throws', async () => {
    const key = (await recentStorageKey(USER)) ?? '';
    vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError');
    });
    expect(loadRecent(key)).toEqual([]);
    expect(() => saveRecent(key, [PATIENT_A_ID])).not.toThrow();
  });
});
