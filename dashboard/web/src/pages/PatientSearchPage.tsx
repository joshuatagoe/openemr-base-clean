import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { Link, useSearchParams } from 'react-router';
import type { Patient } from 'fhir/r4';
import { Spinner } from '../components/Card';
import { FIND_PATIENT_TITLE, useDocumentTitle } from '../components/useDocumentTitle';
import { useFocusOnChange } from '../components/useFocusOnChange';
import { DATE_DISPLAY_FORMAT } from '../config';
import { isRetryable, searchErrorMessage } from '../data/errors';
import {
  DEFAULT_PAGE_SIZE,
  DEFAULT_SORT,
  externalIdOf,
  FULL_DATE_PATTERN,
  globalSearchKeys,
  homePhone,
  IDENTIFIER_PATTERN,
  MAX_CANDIDATES,
  NAME_PATTERN,
  PAGE_SIZES,
  PHONE_PATTERN,
  ssnOf,
  type FinderFilters,
  type FinderPage,
  type FinderSort,
  type FinderSortKey,
} from '../data/finder';
import { usePatientFinder, usePatientsById } from '../data/hooks';
import { patientListName } from '../fhir/patient';
import { formatShortDate } from '../format/date';
import { useRecentPatients } from '../recent/recentContext';
import { EMPTY_SENSITIVE, useFinderMemory, type FinderSensitive } from './finderMemory';
import './landing.css';

// OpenEMR's Patient Finder (interface/main/finder/dynamic_finder.php and
// templates/patient_finder/finder.html.twig), read-only, on FHIR. The wording
// is the Finder's; how each part maps onto FHIR is in data/finder.ts.

type Field = 'name' | 'phone' | 'ssn' | 'birthdate' | 'externalId';
type Draft = Record<Field | 'search', string>;
type Tab = 'list' | 'recent';

interface Column {
  field: Field;
  title: string;
  /** The Finder's placeholder: "Search by " + title ("Full Name" becomes "Name"). */
  filter: string;
  sortKey?: FinderSortKey;
}

// The Finder's default columns (list_options `ptlistcols`: name, phone_home,
// ss, DOB, pubpid). The SSN is not sortable: OpenEMR's FHIR sort whitelist has
// no `ss` column.
const COLUMNS: readonly Column[] = [
  { field: 'name', title: 'Full Name', filter: 'Search by Name', sortKey: 'name' },
  { field: 'phone', title: 'Home Phone', filter: 'Search by Home Phone', sortKey: 'phone' },
  { field: 'ssn', title: 'SSN', filter: 'Search by SSN' },
  { field: 'birthdate', title: 'Date of Birth', filter: 'Search by Date of Birth', sortKey: 'dob' },
  { field: 'externalId', title: 'External ID', filter: 'Search by External ID', sortKey: 'externalId' },
];

const DOB_MESSAGE = 'Enter the full date of birth, or clear the field.';
const SEARCH_MESSAGE = 'Search with letters for a name, or digits for a phone number, SSN, External ID or date of birth.';

// The same patterns the BFF allow-lists, so a refused search is explained here
// instead of coming back as a 400.
const RULES: Readonly<Record<Field, { pattern: RegExp; message: string }>> = {
  name: { pattern: NAME_PATTERN, message: 'Use only letters, spaces, apostrophes, hyphens and dots in the name.' },
  phone: { pattern: PHONE_PATTERN, message: 'Use only digits, spaces and ( ) + . - in the home phone.' },
  ssn: { pattern: IDENTIFIER_PATTERN, message: 'Use only letters, digits and . _ : - in the SSN.' },
  birthdate: { pattern: FULL_DATE_PATTERN, message: DOB_MESSAGE },
  externalId: { pattern: IDENTIFIER_PATTERN, message: 'Use only letters, digits and . _ : - in the External ID.' },
};
const FIELDS: readonly Field[] = ['name', 'phone', 'ssn', 'birthdate', 'externalId'];

/** Filters kept in the URL (so Back returns to them). The SSN, Home Phone and global search never are. */
const URL_KEYS: ReadonlyArray<[Field, string]> = [
  ['name', 'name'],
  ['birthdate', 'birthdate'],
  ['externalId', 'identifier'],
];

const SORT_CODES: Readonly<Record<FinderSortKey, string>> = { name: 'name', phone: 'phone', dob: 'dob', externalId: 'external' };

/** DataTables' search delay for server-side processing. */
const FILTER_DELAY_MS = 400;

const EMPTY_DRAFT: Draft = { name: '', phone: '', ssn: '', birthdate: '', externalId: '', search: '' };

type Validation = { ok: true; filters: FinderFilters; search: string } | { ok: false; field: Field | 'search'; message: string };

/** No filters and no search is valid: it is the whole list. */
function validate(values: Draft): Validation {
  const filters: FinderFilters = {};
  for (const f of FIELDS) {
    const v = values[f].trim();
    if (!v) continue;
    if (!RULES[f].pattern.test(v)) return { ok: false, field: f, message: RULES[f].message };
    filters[f] = v;
  }
  const search = values.search.trim();
  if (search && globalSearchKeys(search).length === 0) return { ok: false, field: 'search', message: SEARCH_MESSAGE };
  return { ok: true, filters, search };
}

/** The URL part that the in-memory values belong to. */
function urlFilterKey(params: URLSearchParams): string {
  return URL_KEYS.map(([, k]) => `${k}=${params.get(k) ?? ''}`).join('&');
}

function pageSizeFromUrl(params: URLSearchParams): number {
  const n = Number(params.get('size'));
  return (PAGE_SIZES as readonly number[]).includes(n) ? n : DEFAULT_PAGE_SIZE;
}

function sortFromUrl(params: URLSearchParams): FinderSort {
  const raw = params.get('sort') ?? '';
  const dir = raw.startsWith('-') ? 'desc' : 'asc';
  const key = (Object.keys(SORT_CODES) as FinderSortKey[]).find((k) => SORT_CODES[k] === raw.replace(/^-/, ''));
  return key ? { key, dir } : DEFAULT_SORT;
}

function pageFromUrl(params: URLSearchParams, pageSize: number): number {
  const raw = params.get('page') ?? '';
  const n = /^[1-9]\d{0,5}$/.test(raw) ? Number(raw) : 1;
  // The BFF accepts _offset up to 999999.
  return (n - 1) * pageSize <= 999_999 ? n : 1;
}

function infoText(page: number, pageSize: number, data: FinderPage): string {
  const first = (page - 1) * pageSize + 1;
  const last = first + data.patients.length - 1;
  if (data.truncated) return `Showing ${first} to ${last} of the first ${MAX_CANDIDATES} matches`;
  if (data.total !== undefined) return `Showing ${first} to ${last} of ${data.total} entries`;
  // OpenEMR's FHIR Bundle has no overall count, so "of N" is known only on the last page.
  return `Showing ${first} to ${last}`;
}

/** The chart link. Bootstrap's stretched-link draws it over the whole row, as the Finder opens a chart from anywhere on the row. */
function ChartLink({ patient, newTab, children }: { patient: Patient; newTab: boolean; children: ReactNode }) {
  return (
    <Link className="stretched-link" to={`/patient/${encodeURIComponent(patient.id ?? '')}`} {...(newTab ? { target: '_blank', rel: 'noopener' } : {})}>
      {children}
    </Link>
  );
}

/**
 * Recent Patients, the Finder's second tab, with its columns (list_options
 * `recent_patient_columns`: First Name, Middle Name, Last Name, Date of Birth).
 * Ids come from this browser's per-user list; names are read live. A patient
 * that is gone or no longer accessible is dropped silently (and from storage).
 */
function RecentPatientsPanel() {
  const recent = useRecentPatients();
  const rows = usePatientsById(recent?.ids ?? []);
  const remove = recent?.remove;
  const goneKey = rows
    .filter((r) => r.status === 'gone')
    .map((r) => r.id)
    .join(',');

  useEffect(() => {
    if (!remove || !goneKey) return;
    for (const id of goneKey.split(',')) remove(id);
  }, [goneKey, remove]);

  const ready = rows.flatMap((r) => (r.status === 'ready' ? [r.patient] : []));
  const loading = rows.some((r) => r.status === 'loading');

  if (ready.length === 0) {
    return loading ? (
      <p className="finder-message muted" role="status">
        <Spinner />
        Loading recent patients…
      </p>
    ) : (
      <p className="finder-empty">No recent patients</p>
    );
  }
  return (
    <>
      <div className="table-responsive finder-scroll" role="region" aria-label="Recent Patients table" tabIndex={0}>
        <table className="finder-table finder-recent" aria-label="Recent Patients">
          <thead>
            <tr>
              <th scope="col">First Name</th>
              <th scope="col">Middle Name</th>
              <th scope="col">Last Name</th>
              <th scope="col">Date of Birth</th>
            </tr>
          </thead>
          <tbody>
            {ready.map((p) => {
              const n = p.name?.find((x) => x.use === 'official') ?? p.name?.[0];
              const first = n?.given?.[0] ?? '';
              return (
                <tr key={p.id} className="row-link">
                  <td>
                    {/* The first column is the link, as in the Finder; a patient without a first name shows the full name there. */}
                    <ChartLink patient={p} newTab={false}>
                      {first || patientListName(p)}
                    </ChartLink>
                  </td>
                  <td>{n?.given?.slice(1).join(' ') ?? ''}</td>
                  <td>{first ? (n?.family ?? '') : ''}</td>
                  <td>{formatShortDate(p.birthDate, DATE_DISPLAY_FORMAT)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {recent && (
        <div className="finder-options">
          <button type="button" className="btn btn-secondary btn-sm" onClick={recent.clear}>
            Clear list
          </button>
        </div>
      )}
    </>
  );
}

const TABS: readonly Tab[] = ['list', 'recent'];
const TAB_LABELS: Readonly<Record<Tab, string>> = { list: 'Patient List', recent: 'Recent Patients' };

/** The Finder's nav-tabs, as a WAI-ARIA tab list (arrow keys, Home / End). */
function FinderTabs({ selected, onSelect, ids }: { selected: Tab; onSelect: (t: Tab) => void; ids: Record<Tab, { tab: string; panel: string }> }) {
  const refs = useRef<Partial<Record<Tab, HTMLButtonElement | null>>>({});
  function onKeyDown(e: KeyboardEvent<HTMLButtonElement>) {
    const i = TABS.indexOf(selected);
    let next: Tab | undefined;
    if (e.key === 'ArrowRight') next = TABS[(i + 1) % TABS.length];
    else if (e.key === 'ArrowLeft') next = TABS[(i + TABS.length - 1) % TABS.length];
    else if (e.key === 'Home') next = TABS[0];
    else if (e.key === 'End') next = TABS[TABS.length - 1];
    if (!next) return;
    e.preventDefault();
    onSelect(next);
    refs.current[next]?.focus();
  }
  return (
    <div className="finder-tabs" role="tablist" aria-label="Patient Finder">
      {TABS.map((t) => (
        <button
          key={t}
          ref={(el) => {
            refs.current[t] = el;
          }}
          type="button"
          role="tab"
          id={ids[t].tab}
          aria-controls={ids[t].panel}
          aria-selected={selected === t}
          tabIndex={selected === t ? 0 : -1}
          className={`finder-tab${selected === t ? ' active' : ''}`}
          onClick={() => onSelect(t)}
          onKeyDown={onKeyDown}
        >
          {TAB_LABELS[t]}
        </button>
      ))}
    </div>
  );
}

export function PatientSearchPage() {
  useDocumentTitle(FIND_PATIENT_TITLE);
  const [params, setParams] = useSearchParams();
  const memory = useFinderMemory();
  const tab: Tab = params.get('tab') === 'recent' ? 'recent' : 'list';
  const pageSize = pageSizeFromUrl(params);
  const sort = sortFromUrl(params);
  const page = pageFromUrl(params, pageSize);
  const exact = params.get('exact') === '1';

  // Applied values: name / DOB / External ID from the URL, the rest from memory.
  const [sensitive, setSensitive] = useState<FinderSensitive>(() => memory.read(urlFilterKey(params)));
  const appliedDraft: Draft = {
    name: params.get('name') ?? '',
    birthdate: params.get('birthdate') ?? '',
    externalId: params.get('identifier') ?? '',
    ssn: sensitive.ssn,
    phone: sensitive.phone,
    search: sensitive.search,
  };
  const applied = validate(appliedDraft);
  const filters = applied.ok ? applied.filters : {};
  const search = applied.ok ? applied.search : '';
  const filtered = Object.keys(filters).length > 0 || search !== '';

  const [values, setValues] = useState<Draft>(appliedDraft);
  const [problem, setProblem] = useState<{ field: Field | 'search'; message: string } | null>(null);
  const [newTab, setNewTab] = useState(false);
  const dirty = useRef(false);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const tableRef = useRef<HTMLTableElement>(null);
  const focusListAfterPaging = useRef(false);
  const inputs = useRef<Partial<Record<Field | 'search', HTMLInputElement | null>>>({});
  const errorId = useId();
  const searchHintId = useId();
  const filterHintId = useId();
  const sizeLabelId = useId();
  const sizeEndId = useId();
  const baseId = useId();
  const ids: Record<Tab, { tab: string; panel: string }> = {
    list: { tab: `${baseId}-list-tab`, panel: `${baseId}-list` },
    recent: { tab: `${baseId}-recent-tab`, panel: `${baseId}-recent` },
  };
  useFocusOnChange(headingRef, 'mount');

  const { view, retry, updating } = usePatientFinder({ filters, search, sort, page, pageSize, exact });

  // After Previous / Next, focus moves to the new page's table (the pressed
  // button may have become disabled), so keyboard users continue from the list.
  useEffect(() => {
    if (focusListAfterPaging.current && view.status === 'ready' && !updating) {
      focusListAfterPaging.current = false;
      tableRef.current?.focus();
    }
  }, [view.status, updating, page]);

  function update(mutate: (next: URLSearchParams) => void) {
    const next = new URLSearchParams(params);
    mutate(next);
    if (next.toString() !== params.toString()) setParams(next, { replace: true });
  }

  function block(field: Field | 'search', message: string) {
    setProblem({ field, message });
    inputs.current[field]?.focus();
  }

  /** Applies the typed filters. `explicit` (Enter) explains a problem; the typing delay just waits for valid input. */
  function apply(explicit: boolean) {
    // A partly typed date reads as '' (the browser only reports badInput), so
    // without this check the search would silently run without the DOB.
    if (inputs.current.birthdate?.validity.badInput) {
      if (explicit) block('birthdate', DOB_MESSAGE);
      return;
    }
    const check = validate(values);
    if (!check.ok) {
      if (explicit) block(check.field, check.message);
      return;
    }
    setProblem(null);
    const next = new URLSearchParams(params);
    for (const [field, key] of URL_KEYS) {
      const v = check.filters[field];
      if (v) next.set(key, v);
      else next.delete(key);
    }
    const nextSensitive: FinderSensitive = { ssn: check.filters.ssn ?? '', phone: check.filters.phone ?? '', search: check.search };
    const changed =
      urlFilterKey(next) !== urlFilterKey(params) ||
      nextSensitive.ssn !== sensitive.ssn ||
      nextSensitive.phone !== sensitive.phone ||
      nextSensitive.search !== sensitive.search;
    if (!changed) return;
    // New filters start again at page 1.
    next.delete('page');
    memory.write(urlFilterKey(next), nextSensitive);
    setSensitive(nextSensitive);
    setParams(next, { replace: true });
  }

  // Like the Finder's DataTable, the list follows the filters as they are typed.
  useEffect(() => {
    if (!dirty.current) return;
    const timer = setTimeout(() => {
      dirty.current = false;
      apply(false);
    }, FILTER_DELAY_MS);
    return () => clearTimeout(timer);
  });

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    dirty.current = false;
    apply(true);
  }

  function change(field: Field | 'search', value: string) {
    dirty.current = true;
    setValues((v) => ({ ...v, [field]: value }));
  }

  function onSort(key: FinderSortKey) {
    // As DataTables: a new column sorts ascending, the sorted one flips; back to page 1.
    const dir = sort.key === key && sort.dir === 'asc' ? 'desc' : 'asc';
    update((next) => {
      if (key === DEFAULT_SORT.key && dir === DEFAULT_SORT.dir) next.delete('sort');
      else next.set('sort', `${dir === 'desc' ? '-' : ''}${SORT_CODES[key]}`);
      next.delete('page');
    });
  }

  function onPageSize(size: number) {
    update((next) => {
      if (size === DEFAULT_PAGE_SIZE) next.delete('size');
      else next.set('size', String(size));
      next.delete('page');
    });
  }

  function goToPage(n: number) {
    focusListAfterPaging.current = true;
    update((next) => {
      if (n <= 1) next.delete('page');
      else next.set('page', String(n));
    });
  }

  function setExact(on: boolean) {
    update((next) => {
      if (on) next.set('exact', '1');
      else next.delete('exact');
      next.delete('page');
    });
  }

  function selectTab(t: Tab) {
    update((next) => {
      if (t === 'recent') next.set('tab', 'recent');
      else next.delete('tab');
    });
  }

  function clearSearch() {
    dirty.current = false;
    setValues(EMPTY_DRAFT);
    setProblem(null);
    memory.write('', EMPTY_SENSITIVE);
    setSensitive(EMPTY_SENSITIVE);
    update((next) => {
      for (const key of [...URL_KEYS.map(([, k]) => k), 'page']) next.delete(key);
    });
  }

  const invalid = (field: Field | 'search') => (problem?.field === field ? ({ 'aria-invalid': true, 'aria-describedby': errorId } as const) : null);

  const hasNext = view.status === 'ready' && view.data.hasNext;
  const patients = view.status === 'ready' ? view.data.patients : [];
  const errorMessage = view.status === 'error' ? searchErrorMessage(view.error, { filtered }) : null;
  const showPager = (view.status === 'ready' || view.status === 'empty') && (page > 1 || hasNext);
  const pageOne = new URLSearchParams(params);
  pageOne.delete('page');

  // The info line (DataTables' "Showing x to y of z entries") is the live region.
  let status: ReactNode = null;
  if (view.status === 'loading' || view.status === 'idle') {
    status = (
      <>
        <Spinner />
        {filtered ? 'Searching…' : 'Loading patients…'}
      </>
    );
  } else if (view.status === 'ready') {
    status = infoText(page, pageSize, view.data);
  } else if (view.status === 'empty' && page === 1) {
    status = 'Showing 0 to 0 of 0 entries';
  }

  let emptyRow: ReactNode = null;
  if (view.status === 'empty') {
    if (page > 1) {
      emptyRow = (
        <>
          This page is past the end of the list. <Link to={{ search: pageOne.toString() }}>Go to page 1</Link>
        </>
      );
    } else {
      emptyRow = filtered ? 'No matching records found' : 'No data available in table';
    }
  }

  const filterCell = (c: Column) => {
    const bad = invalid(c.field);
    return (
      <td key={c.field}>
        <input
          ref={(el) => {
            inputs.current[c.field] = el;
          }}
          className={`finder-filter finder-filter-${c.field}`}
          type={c.field === 'birthdate' ? 'date' : 'text'}
          placeholder={c.field === 'birthdate' ? undefined : c.filter}
          aria-label={c.filter}
          value={values[c.field]}
          autoComplete="off"
          onChange={(e) => change(c.field, e.target.value)}
          onKeyDown={onKeyDown}
          aria-describedby={bad ? errorId : c.field === 'name' || c.field === 'birthdate' ? undefined : filterHintId}
          aria-invalid={bad ? true : undefined}
        />
      </td>
    );
  };

  const headerCell = (c: Column) => {
    const key = c.sortKey;
    if (!key) {
      return (
        <th key={c.field} scope="col">
          {c.title}
        </th>
      );
    }
    const active = sort.key === key;
    return (
      <th key={c.field} scope="col" aria-sort={active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : undefined}>
        <button type="button" className={`finder-sort${active ? ` sorted-${sort.dir}` : ''}`} onClick={() => onSort(key)}>
          {c.title}
        </button>
      </th>
    );
  };

  const searchBad = invalid('search');

  return (
    <section className="finder">
      <h1 className="page-title" ref={headingRef} tabIndex={-1}>
        Patient Finder
      </h1>
      <FinderTabs selected={tab} onSelect={selectTab} ids={ids} />

      <div role="tabpanel" id={ids.list.panel} aria-labelledby={ids.list.tab} hidden={tab !== 'list'} className="finder-panel">
        {tab === 'list' && (
          <>
            <form role="search" aria-label="Patient List search" onSubmit={(e) => e.preventDefault()} noValidate>
              <div className="finder-toolbar">
                <div className="finder-length">
                  <span id={sizeLabelId}>Show</span>
                  <select aria-labelledby={`${sizeLabelId} ${sizeEndId}`} value={pageSize} onChange={(e) => onPageSize(Number(e.target.value))}>
                    {PAGE_SIZES.map((n) => (
                      <option key={n} value={n}>
                        {n}
                      </option>
                    ))}
                  </select>
                  <span id={sizeEndId}>entries</span>
                </div>
                <div className="finder-search">
                  <label htmlFor={`${baseId}-search`}>Search:</label>
                  <input
                    id={`${baseId}-search`}
                    ref={(el) => {
                      inputs.current.search = el;
                    }}
                    type="search"
                    value={values.search}
                    autoComplete="off"
                    onChange={(e) => change('search', e.target.value)}
                    onKeyDown={onKeyDown}
                    aria-describedby={searchBad ? errorId : searchHintId}
                    aria-invalid={searchBad ? true : undefined}
                  />
                </div>
              </div>
              {problem && (
                <p id={errorId} className="notice notice-warning finder-problem" role="alert">
                  {problem.message}
                </p>
              )}

              <div className="table-responsive finder-scroll" role="region" aria-label="Patient List table" tabIndex={0}>
                <table className="finder-table" ref={tableRef} tabIndex={-1} aria-label="Patient List" aria-busy={updating || undefined}>
                  <thead>
                    <tr className="finder-filters">{COLUMNS.map(filterCell)}</tr>
                    <tr>{COLUMNS.map(headerCell)}</tr>
                  </thead>
                  <tbody>
                    {patients.map((p) => (
                      <tr key={p.id} className="row-link">
                        <td>
                          <ChartLink patient={p} newTab={newTab}>
                            {patientListName(p)}
                          </ChartLink>
                        </td>
                        <td>{homePhone(p) ?? ''}</td>
                        <td>{ssnOf(p) ?? ''}</td>
                        <td>{formatShortDate(p.birthDate, DATE_DISPLAY_FORMAT)}</td>
                        <td>{externalIdOf(p) ?? ''}</td>
                      </tr>
                    ))}
                    {emptyRow && (
                      <tr>
                        <td colSpan={COLUMNS.length} className="finder-empty-cell">
                          {emptyRow}
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </form>

            {view.status === 'error' && errorMessage && (
              <div className="finder-error">
                <p className="notice notice-warning" role="alert">
                  {errorMessage}
                </p>
                {isRetryable(view.error) && (
                  <button type="button" className="btn btn-secondary btn-sm" onClick={retry}>
                    Try again
                  </button>
                )}
              </div>
            )}
            {view.status === 'ready' && view.data.truncated && (
              <p className="finder-note">
                More than {MAX_CANDIDATES} patients match, so only the first {MAX_CANDIDATES} are shown. Add a filter to narrow the search.
              </p>
            )}
            <div className="finder-options">
              <div className="finder-checks">
                <label className="finder-check">
                  <input type="checkbox" checked={newTab} onChange={(e) => setNewTab(e.target.checked)} />
                  Open in New Browser Tab
                </label>
                <label className="finder-check">
                  <input type="checkbox" checked={exact} onChange={(e) => setExact(e.target.checked)} />
                  Search with exact method
                </label>
              </div>
              {filtered && (
                <button type="button" className="btn btn-secondary btn-sm" onClick={clearSearch}>
                  Clear search
                </button>
              )}
            </div>
            <div className="finder-footer">
              <p className="finder-info" role="status">
                {status}
              </p>
              {showPager && (
                <nav className="finder-pager" aria-label="Patient list pages">
                  <button type="button" className="btn btn-secondary btn-sm" disabled={page <= 1} onClick={() => goToPage(page - 1)} aria-label="Previous page">
                    Previous
                  </button>
                  <span>Page {page}</span>
                  <button type="button" className="btn btn-secondary btn-sm" disabled={!hasNext} onClick={() => goToPage(page + 1)} aria-label="Next page">
                    Next
                  </button>
                </nav>
              )}
            </div>
            <div className="finder-notes">
              <p id={searchHintId}>
                Search: finds names that start with the text, dates of birth (1980, 1980-06 or 1980-06-15), and whole phone numbers, SSNs and External
                IDs.
              </p>
              <p id={filterHintId}>
                Search by Home Phone, SSN and External ID match the whole value; Search by Name matches the start of a name (the whole name, with
                exact method).
              </p>
            </div>
          </>
        )}
      </div>

      <div role="tabpanel" id={ids.recent.panel} aria-labelledby={ids.recent.tab} hidden={tab !== 'recent'} className="finder-panel">
        {tab === 'recent' && <RecentPatientsPanel />}
      </div>
    </section>
  );
}
