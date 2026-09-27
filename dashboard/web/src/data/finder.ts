// OpenEMR's Patient Finder (interface/main/finder/dynamic_finder.php) on FHIR.
//
// The Finder runs one SQL query per keystroke: prefix LIKE per column, an OR
// across every column for the global "Search:" box, ORDER BY the clicked
// column, LIMIT for paging and a COUNT for "of N entries". FHIR Patient search
// can do some of that and not the rest, so each Finder state becomes either:
//
// - 'server': one search that OpenEMR filters, sorts and pages (`_count` =
//   page size + 1 probe row, `_offset`, `_sort`). Used for the name and date of
//   birth filters, which map straight onto FHIR `name` and `birthdate`.
// - 'client': up to three searches of at most 101 rows each, merged,
//   de-duplicated, refined, sorted and paged here. Needed because
//   * FHIR has no OR across parameters, so the global search runs one search
//     per field the text could be (name, identifier, phone, birthdate);
//   * `identifier` matches the SSN OR the External ID (pubpid), and `phone`
//     matches home, work OR mobile, so the SSN, External ID and Home Phone
//     column filters are checked against the right field here.
//   At most 100 candidates are kept (MAX_CANDIDATES); beyond that the page
//   says only the first 100 are shown.
//
// Everything here is pure (no React), so it is unit-tested directly.
import type { Identifier, Patient } from 'fhir/r4';

export type FinderSortKey = 'name' | 'phone' | 'dob' | 'externalId';
export type SortDir = 'asc' | 'desc';
export interface FinderSort {
  key: FinderSortKey;
  dir: SortDir;
}

/** The Finder's column filters ("Search by …"). Values are already validated. */
export interface FinderFilters {
  name?: string;
  phone?: string;
  ssn?: string;
  birthdate?: string;
  externalId?: string;
}

export interface FinderState {
  filters: FinderFilters;
  /** The global "Search:" box ('' = none). */
  search: string;
  sort: FinderSort;
  /** 1-based. */
  page: number;
  pageSize: number;
  /**
   * The Finder's "Search with exact method": names must match whole
   * (`name:exact`, which OpenEMR runs as a case-sensitive BINARY comparison;
   * the Finder's own exact method is `LIKE` without wildcards, case-insensitive).
   * Phone, SSN and External ID always match whole on FHIR.
   */
  exact?: boolean;
}

export type SearchParamsRecord = Record<string, string>;

export type FinderPlan =
  | { mode: 'server'; params: SearchParamsRecord }
  | { mode: 'client'; requests: SearchParamsRecord[]; keep: (p: Patient) => boolean };

/** "Show [10|25|50|100] entries" (the Finder's lengthMenu). */
export const PAGE_SIZES = [10, 25, 50, 100] as const;
/** OpenEMR's default Patient List Page Size (globals gbl_pt_list_page_size). */
export const DEFAULT_PAGE_SIZE = 10;
/** The most rows a client-mode search keeps (the BFF allows _count up to 101). */
export const MAX_CANDIDATES = 100;
export const DEFAULT_SORT: FinderSort = { key: 'name', dir: 'asc' };

/**
 * `_sort` per column, as OpenEMR maps it (FhirPatientService search definitions,
 * filtered by PatientService::ALLOWED_SORT_COLUMNS): family -> lname, given ->
 * fname, mname; phone -> phone_home, phone_biz, phone_cell; birthdate -> DOB;
 * identifier -> pubpid (ss is not an allowed sort column, so the SSN column
 * cannot be sorted).
 */
export const SORT_PARAM: Readonly<Record<FinderSortKey, readonly [asc: string, desc: string]>> = {
  name: ['family,given', '-family,-given'],
  phone: ['phone', '-phone'],
  dob: ['birthdate', '-birthdate'],
  externalId: ['identifier', '-identifier'],
};

// The patterns the BFF allow-lists (identifier without `|`, which FHIR reads
// as system|code).
export const NAME_PATTERN = /^[\p{L}\p{M}' .-]{1,64}$/u;
export const IDENTIFIER_PATTERN = /^[A-Za-z0-9._:-]{1,64}$/;
export const PHONE_PATTERN = /^(?=[^0-9]*[0-9])[0-9 ()+.-]{1,32}$/;
export const FULL_DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
/** A FHIR date at year, month or day precision (the Finder matches DOB by prefix). */
const PARTIAL_DATE_PATTERN = /^\d{4}(-\d{2}(-\d{2})?)?$/;

type GlobalKey = 'name' | 'identifier' | 'phone' | 'birthdate';

/** The FHIR parameters a global search text is sent as (in this order); [] = it can't be searched. */
export function globalSearchKeys(text: string): GlobalKey[] {
  const t = text.trim();
  const keys: GlobalKey[] = [];
  if (NAME_PATTERN.test(t) && /\p{L}/u.test(t)) keys.push('name');
  if (IDENTIFIER_PATTERN.test(t)) keys.push('identifier');
  if (PHONE_PATTERN.test(t)) keys.push('phone');
  if (PARTIAL_DATE_PATTERN.test(t)) keys.push('birthdate');
  return keys;
}

// ---- reading the Finder's fields from a FHIR Patient ------------------------

function hasTypeCode(identifier: Identifier, code: string): boolean {
  return (identifier.type?.coding ?? []).some((c) => c.code === code);
}

function identifierValue(patient: Patient, code: string): string | undefined {
  return patient.identifier?.find((i) => hasTypeCode(i, code) && typeof i.value === 'string' && i.value.trim() !== '')?.value;
}

/** SSN: the identifier with type code SS (OpenEMR `ss`). */
export function ssnOf(patient: Patient): string | undefined {
  return identifierValue(patient, 'SS');
}

/** External ID: the identifier with type code PT (OpenEMR `pubpid`). */
export function externalIdOf(patient: Patient): string | undefined {
  return identifierValue(patient, 'PT');
}

function phoneOfUse(patient: Patient, use: 'home' | 'work' | 'mobile'): string | undefined {
  return patient.telecom?.find((t) => t.system === 'phone' && t.use === use && !!t.value?.trim())?.value;
}

/** Home Phone: telecom phone with use=home (OpenEMR `phone_home`). */
export function homePhone(patient: Patient): string | undefined {
  return phoneOfUse(patient, 'home');
}

// MySQL compares with a case-insensitive collation and ignores trailing spaces.
const fold = (s: string | undefined) => (s ?? '').trim().toLocaleLowerCase();
const same = (a: string | undefined, b: string) => a !== undefined && fold(a) === fold(b);

/** FHIR `name` on OpenEMR: starts-with on first, middle or last name, or title; `name:exact`: the whole part, case-sensitive. */
function nameMatches(patient: Patient, text: string, exact: boolean): boolean {
  const t = exact ? text.trim() : fold(text);
  return (patient.name ?? []).some((n) =>
    [n.family, ...(n.given ?? []), ...(n.prefix ?? [])].some((part) => (exact ? (part ?? '').trim() === t : fold(part).startsWith(t))),
  );
}

// ---- planning ------------------------------------------------------------------

function sortParam(sort: FinderSort): string {
  return SORT_PARAM[sort.key][sort.dir === 'asc' ? 0 : 1];
}

/** Column filters as FHIR parameters: SSN and External ID share `identifier` (the SSN wins; both are checked here). */
function columnParams(f: FinderFilters, nameKey: string): SearchParamsRecord {
  const out: SearchParamsRecord = {};
  if (f.name) out[nameKey] = f.name;
  if (f.birthdate) out.birthdate = f.birthdate;
  const identifier = f.ssn ?? f.externalId;
  if (identifier) out.identifier = identifier;
  if (f.phone) out.phone = f.phone;
  return out;
}

export function planFinder(state: FinderState): FinderPlan {
  const { filters: f, sort } = state;
  const search = state.search.trim();
  const exact = state.exact === true;
  const nameKey = exact ? 'name:exact' : 'name';
  const columns = columnParams(f, nameKey);
  const needsRefine = !!(f.ssn || f.externalId || f.phone);

  if (!search && !needsRefine) {
    return {
      mode: 'server',
      params: { ...columns, _count: String(state.pageSize + 1), _offset: String((state.page - 1) * state.pageSize), _sort: sortParam(sort) },
    };
  }

  const bounded = { _count: String(MAX_CANDIDATES + 1), _sort: sortParam(sort) };
  const checks: Array<(p: Patient) => boolean> = [];
  if (f.ssn) checks.push((p) => same(ssnOf(p), f.ssn ?? ''));
  if (f.externalId) checks.push((p) => same(externalIdOf(p), f.externalId ?? ''));
  if (f.phone) checks.push((p) => same(homePhone(p), f.phone ?? ''));

  let requests: SearchParamsRecord[];
  if (!search) {
    requests = [{ ...columns, ...bounded }];
  } else {
    const keys = globalSearchKeys(search);
    // A branch whose parameter is also a column filter sends the search text;
    // the column filter is then checked here (identifier and phone already are).
    if (keys.includes('name') && f.name) checks.push((p) => nameMatches(p, f.name ?? '', exact));
    if (keys.includes('birthdate') && f.birthdate) checks.push((p) => p.birthDate === f.birthdate);
    requests = keys.map((key) => ({ ...columns, [key === 'name' ? nameKey : key]: search, ...bounded }));
  }
  return { mode: 'client', requests, keep: (p) => checks.every((c) => c(p)) };
}

// ---- client mode: merge, sort, page ----------------------------------------------

export interface Candidates {
  patients: Patient[];
  /** A search came back full (more than MAX_CANDIDATES rows): there are more matches than were read. */
  truncated: boolean;
}

/** Merge the searches' rows, first seen wins, de-duplicated by id. */
export function mergeCandidates(results: readonly (readonly Patient[])[]): Candidates {
  const seen = new Set<string>();
  const patients: Patient[] = [];
  const truncated = results.some((r) => r.length > MAX_CANDIDATES);
  for (const rows of results) {
    for (const p of rows) {
      if (!p.id || seen.has(p.id)) continue;
      seen.add(p.id);
      patients.push(p);
    }
  }
  return { patients, truncated };
}

const collator = new Intl.Collator(undefined, { sensitivity: 'base', numeric: false });

function officialName(p: Patient) {
  const names = p.name ?? [];
  return names.find((n) => n.use === 'official') ?? names[0];
}

/** The ORDER BY columns OpenEMR uses for each sort. */
function sortValues(p: Patient, key: FinderSortKey): string[] {
  switch (key) {
    case 'name': {
      const n = officialName(p);
      return [n?.family ?? '', n?.given?.[0] ?? '', n?.given?.slice(1).join(' ') ?? ''];
    }
    case 'phone':
      return [homePhone(p) ?? '', phoneOfUse(p, 'work') ?? '', phoneOfUse(p, 'mobile') ?? ''];
    case 'dob':
      return [p.birthDate ?? ''];
    case 'externalId':
      return [externalIdOf(p) ?? ''];
  }
}

/** Sorts like the server would; blanks sort first ascending (as MySQL's NULL / ''). Stable. */
export function sortPatients(patients: readonly Patient[], sort: FinderSort): Patient[] {
  const sign = sort.dir === 'asc' ? 1 : -1;
  const keyed = patients.map((p, i) => ({ p, i, v: sortValues(p, sort.key) }));
  keyed.sort((a, b) => {
    for (let k = 0; k < a.v.length; k++) {
      const c = collator.compare(a.v[k] ?? '', b.v[k] ?? '');
      if (c !== 0) return sign * c;
    }
    return a.i - b.i;
  });
  return keyed.map((x) => x.p);
}

export interface FinderPage {
  patients: Patient[];
  hasNext: boolean;
  /** The number of matching rows, when known (client mode, or the last server page). */
  total?: number;
  truncated: boolean;
}

/** One page of a sorted list held in the browser (client mode); only the first MAX_CANDIDATES are ever shown. */
export function finderPage(all: readonly Patient[], page: number, pageSize: number, truncated: boolean): FinderPage {
  const start = (page - 1) * pageSize;
  const kept = all.slice(0, MAX_CANDIDATES);
  return {
    patients: kept.slice(start, start + pageSize),
    hasNext: kept.length > start + pageSize,
    total: kept.length,
    truncated: truncated || all.length > MAX_CANDIDATES,
  };
}
