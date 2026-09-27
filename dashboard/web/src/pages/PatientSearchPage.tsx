import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode, type Ref } from 'react';
import { Link, useSearchParams } from 'react-router';
import type { Patient } from 'fhir/r4';
import { Spinner } from '../components/Card';
import { FIND_PATIENT_TITLE, useDocumentTitle } from '../components/useDocumentTitle';
import { useFocusOnChange } from '../components/useFocusOnChange';
import { DATE_DISPLAY_FORMAT } from '../config';
import { isRetryable, searchErrorMessage } from '../data/errors';
import { patientListName, patientMrn, patientSexLabel } from '../fhir/patient';
import { formatShortDate } from '../format/date';
import { PATIENT_PAGE_SIZE, usePatientList, usePatientsById, type PatientSearchCriteria } from '../data/hooks';
import { useRecentPatients } from '../recent/recentContext';
import './landing.css';

type Field = 'name' | 'birthdate' | 'identifier';

const DOB_MESSAGE = 'Enter the full date of birth, or clear the field.';

// The same patterns the BFF allow-list enforces, so a refused search is
// explained here instead of coming back as a 400.
const RULES: Readonly<Record<Field, { pattern: RegExp; message: string }>> = {
  name: { pattern: /^[\p{L}\p{M}' .-]{1,64}$/u, message: 'Use only letters, spaces, apostrophes, hyphens and dots in the name.' },
  birthdate: { pattern: /^\d{4}-\d{2}-\d{2}$/, message: DOB_MESSAGE },
  identifier: { pattern: /^[A-Za-z0-9._|:-]{1,64}$/, message: 'Use only letters, digits and . _ | : - in the MRN.' },
};
const FIELDS: readonly Field[] = ['name', 'birthdate', 'identifier'];

// The BFF accepts _offset up to 999999.
const MAX_PAGE = Math.floor(999_999 / PATIENT_PAGE_SIZE) + 1;

type Validation = { ok: true; criteria: PatientSearchCriteria } | { ok: false; field: Field; message: string };

/** No criteria is valid: it is the whole list. */
function validate(values: Record<Field, string>): Validation {
  const criteria: PatientSearchCriteria = {};
  for (const f of FIELDS) {
    const v = values[f].trim();
    if (!v) continue;
    if (!RULES[f].pattern.test(v)) return { ok: false, field: f, message: RULES[f].message };
    criteria[f] = v;
  }
  return { ok: true, criteria };
}

function fromUrl(params: URLSearchParams): Record<Field, string> {
  return { name: params.get('name') ?? '', birthdate: params.get('birthdate') ?? '', identifier: params.get('identifier') ?? '' };
}

function pageFromUrl(params: URLSearchParams): number {
  const raw = params.get('page') ?? '';
  const n = /^[1-9]\d{0,5}$/.test(raw) ? Number(raw) : 1;
  return n <= MAX_PAGE ? n : 1;
}

/**
 * Name (the chart link), DOB, sex and MRN (the PT identifier; never the SSN,
 * never a phone number). The name is the one real link; Bootstrap's
 * stretched-link draws it over the whole row, as OpenEMR's Patient Finder
 * opens a chart from anywhere on the row.
 */
function PatientTable({ labelledBy, patients, tableRef }: { labelledBy: string; patients: readonly Patient[]; tableRef?: Ref<HTMLTableElement> }) {
  return (
    <table className="landing-table" ref={tableRef} tabIndex={tableRef ? -1 : undefined} aria-labelledby={labelledBy}>
      <thead>
        <tr>
          <th scope="col">Name</th>
          <th scope="col">DOB</th>
          <th scope="col">Sex</th>
          <th scope="col">MRN</th>
        </tr>
      </thead>
      <tbody>
        {patients.map((p) => (
          <tr key={p.id} className="row-link">
            <td>
              <Link className="stretched-link" to={`/patient/${encodeURIComponent(p.id ?? '')}`}>
                {patientListName(p)}
              </Link>
            </td>
            <td>{formatShortDate(p.birthDate, DATE_DISPLAY_FORMAT)}</td>
            <td>{patientSexLabel(p)}</td>
            <td>{patientMrn(p) ?? ''}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** OpenEMR card chrome (square, 1px shadow) with a plain title and one item on the right. */
function LandingCard({ headingId, title, aside, children }: { headingId: string; title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="card landing-card" aria-labelledby={headingId}>
      <div className="landing-card-head">
        <h2 id={headingId}>{title}</h2>
        {aside}
      </div>
      <div className="landing-card-body">{children}</div>
    </section>
  );
}

/**
 * Recent patients (like the Patient Finder's "Recent Patients" in OpenEMR): ids
 * from this browser's per-user list, names read live. A patient that is gone or
 * no longer accessible is dropped silently (and from the stored list).
 */
function RecentPatients() {
  const recent = useRecentPatients();
  const rows = usePatientsById(recent?.ids ?? []);
  const headingId = useId();
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
  if (!recent || (ready.length === 0 && !loading)) return null;

  return (
    <LandingCard
      headingId={headingId}
      title="Recent patients"
      aside={
        <button type="button" className="btn btn-secondary btn-sm" onClick={recent.clear}>
          Clear list
        </button>
      }
    >
      {ready.length > 0 ? (
        <PatientTable labelledBy={headingId} patients={ready} />
      ) : (
        <p className="landing-message muted">
          <Spinner />
          Loading recent patients…
        </p>
      )}
    </LandingCard>
  );
}

export function PatientSearchPage() {
  useDocumentTitle(FIND_PATIENT_TITLE);
  const [params, setParams] = useSearchParams();
  const urlValues = fromUrl(params);
  const urlCheck = validate(urlValues);
  const criteria = urlCheck.ok ? urlCheck.criteria : {};
  const filtered = Object.keys(criteria).length > 0;
  const page = pageFromUrl(params);
  const [values, setValues] = useState(urlValues);
  const [problem, setProblem] = useState<{ field: Field; message: string } | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const tableRef = useRef<HTMLTableElement>(null);
  const focusListAfterPaging = useRef(false);
  const inputs = useRef<Partial<Record<Field, HTMLInputElement | null>>>({});
  const errorId = useId();
  const listHeadingId = useId();
  useFocusOnChange(headingRef, 'mount');

  const { view, retry } = usePatientList({ criteria, page });

  // After Previous / Next, focus moves to the new page's table (the pressed
  // button may have become disabled), so keyboard users continue from the list.
  useEffect(() => {
    if (focusListAfterPaging.current && view.status === 'ready') {
      focusListAfterPaging.current = false;
      tableRef.current?.focus();
    }
  }, [view.status, page]);

  function block(field: Field, message: string) {
    setProblem({ field, message });
    inputs.current[field]?.focus();
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    // A partly typed date reads as '' (the browser only reports badInput), so
    // without this check the search would silently run without the DOB.
    if (inputs.current.birthdate?.validity.badInput) {
      block('birthdate', DOB_MESSAGE);
      return;
    }
    const check = validate(values);
    if (!check.ok) {
      block(check.field, check.message);
      return;
    }
    setProblem(null);
    // New criteria start again at page 1; no criteria = the whole list.
    setParams(new URLSearchParams(check.criteria as Record<string, string>));
  }

  function goToPage(n: number) {
    const next = new URLSearchParams(params);
    if (n <= 1) next.delete('page');
    else next.set('page', String(n));
    focusListAfterPaging.current = true;
    setParams(next);
  }

  const field = (name: Field, label: string, type: 'text' | 'date') => (
    <div className={`landing-field landing-field-${name}`}>
      <label htmlFor={`search-${name}`}>{label}</label>
      <input
        id={`search-${name}`}
        ref={(el) => {
          inputs.current[name] = el;
        }}
        type={type}
        value={values[name]}
        autoComplete="off"
        onChange={(e) => setValues((v) => ({ ...v, [name]: e.target.value }))}
        aria-invalid={problem?.field === name ? true : undefined}
        aria-describedby={problem?.field === name ? errorId : undefined}
      />
    </div>
  );

  const hasNext = view.status === 'ready' && view.data.hasNext;
  const patients = view.status === 'ready' ? view.data.patients : [];
  const first = (page - 1) * PATIENT_PAGE_SIZE + 1;
  let count = '';
  if (view.status === 'ready') {
    const n = patients.length;
    const noun = n === 1 ? 'patient' : 'patients';
    if (page === 1 && !hasNext) count = filtered ? `${n} ${noun} found` : `${n} ${noun}`;
    else count = `Showing ${first}–${first + n - 1}`;
  }
  const pageOne = new URLSearchParams(params);
  pageOne.delete('page');

  // One live region: the loading and empty messages, or (read only by screen
  // readers) the count that the card header shows.
  let status: ReactNode = null;
  if (view.status === 'loading' || view.status === 'idle') {
    status = (
      <p className="landing-message muted">
        <Spinner />
        {filtered ? 'Searching…' : 'Loading patients…'}
      </p>
    );
  } else if (view.status === 'empty') {
    status =
      page > 1 ? (
        <p className="landing-message">
          This page is past the end of the list. <Link to={{ search: pageOne.toString() }}>Go to page 1</Link>
        </p>
      ) : (
        <p className="landing-message">{filtered ? 'No patients match. Check the spelling, or search with fewer fields.' : 'No patients to show.'}</p>
      );
  } else if (view.status === 'ready') {
    status = <span className="visually-hidden">{count}</span>;
  }
  const errorMessage = view.status === 'error' ? searchErrorMessage(view.error, { filtered }) : null;
  const showPager = (view.status === 'ready' || view.status === 'empty') && (page > 1 || hasNext);

  return (
    <section className="landing">
      <h1 className="page-title" ref={headingRef} tabIndex={-1}>
        Find a patient
      </h1>
      <form className="landing-form" role="search" aria-label="Patient search" onSubmit={onSubmit} noValidate>
        {field('name', 'Name', 'text')}
        {field('birthdate', 'Date of birth', 'date')}
        {field('identifier', 'MRN', 'text')}
        <div className="landing-actions">
          <button type="submit" className="btn btn-primary">
            Search
          </button>
          {filtered && (
            <Link className="btn btn-secondary" to="/dashboard" onClick={() => setValues({ name: '', birthdate: '', identifier: '' })}>
              Show all patients
            </Link>
          )}
        </div>
      </form>
      {problem && (
        <p id={errorId} className="notice notice-warning landing-problem" role="alert">
          {problem.message}
        </p>
      )}

      {!filtered && <RecentPatients />}

      <LandingCard
        headingId={listHeadingId}
        title={filtered ? 'Search results' : 'All patients'}
        aside={
          count ? (
            <span className="landing-count" aria-hidden="true">
              {count}
            </span>
          ) : undefined
        }
      >
        <div role="status">{status}</div>
        {view.status === 'error' && errorMessage && (
          <div className="landing-error">
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
        {view.status === 'ready' && <PatientTable labelledBy={listHeadingId} patients={patients} tableRef={tableRef} />}
      </LandingCard>
      {showPager && (
        <nav className="landing-pager" aria-label="Patient list pages">
          <button type="button" className="btn btn-secondary" disabled={page <= 1} onClick={() => goToPage(page - 1)} aria-label="Previous page">
            Previous
          </button>
          <span>Page {page}</span>
          <button type="button" className="btn btn-secondary" disabled={!hasNext} onClick={() => goToPage(page + 1)} aria-label="Next page">
            Next
          </button>
        </nav>
      )}
    </section>
  );
}
