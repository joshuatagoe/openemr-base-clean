import { useEffect, useId, useRef, useState, type FormEvent, type Ref } from 'react';
import { Link, useSearchParams } from 'react-router';
import type { Patient } from 'fhir/r4';
import { useFocusOnChange } from '../components/useFocusOnChange';
import { DATE_DISPLAY_FORMAT } from '../config';
import { patientListName, patientMrn, patientSexLabel } from '../fhir/patient';
import { formatShortDate } from '../format/date';
import { PATIENT_PAGE_SIZE, usePatientList, usePatientsById, type PatientSearchCriteria } from '../data/hooks';
import { useRecentPatients } from '../recent/recentContext';

type Field = 'name' | 'birthdate' | 'identifier';

// The same patterns the BFF allow-list enforces, so a refused search is
// explained here instead of coming back as a 400.
const RULES: Readonly<Record<Field, { pattern: RegExp; message: string }>> = {
  name: { pattern: /^[\p{L}\p{M}' .-]{1,64}$/u, message: 'Names may contain letters, spaces, apostrophes, hyphens and dots.' },
  birthdate: { pattern: /^\d{4}-\d{2}-\d{2}$/, message: 'Enter the date of birth as YYYY-MM-DD.' },
  identifier: { pattern: /^[A-Za-z0-9._|:-]{1,64}$/, message: 'MRNs may contain letters, digits and . _ | : -' },
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

/** Name (link to the chart), DOB, sex and MRN (the PT identifier; never the SSN, never a phone number). */
function PatientTable({
  caption,
  labelledBy,
  patients,
  tableRef,
}: {
  /** Visible caption, or `labelledBy`: the id of a heading that names the table. */
  caption?: string;
  labelledBy?: string;
  patients: readonly Patient[];
  tableRef?: Ref<HTMLTableElement>;
}) {
  return (
    <table className="results" ref={tableRef} tabIndex={tableRef ? -1 : undefined} aria-labelledby={labelledBy}>
      {caption && <caption>{caption}</caption>}
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
          <tr key={p.id}>
            <td>
              <Link to={`/patient/${encodeURIComponent(p.id ?? '')}`}>{patientListName(p)}</Link>
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
    <section className="recent-patients" aria-labelledby={headingId}>
      <div className="section-header">
        <h2 id={headingId}>Recent patients</h2>
        <button type="button" className="btn btn-secondary btn-sm" onClick={recent.clear} aria-label="Clear recent patients">
          Clear
        </button>
      </div>
      {ready.length > 0 ? <PatientTable labelledBy={headingId} patients={ready} /> : <p className="muted">Loading recent patients...</p>}
    </section>
  );
}

export function PatientSearchPage() {
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

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    const check = validate(values);
    if (!check.ok) {
      setProblem({ field: check.field, message: check.message });
      inputs.current[check.field]?.focus();
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

  const field = (name: Field, label: string, type: 'text' | 'date', autoComplete = 'off') => (
    <div className="form-field">
      <label htmlFor={`search-${name}`}>{label}</label>
      <input
        id={`search-${name}`}
        ref={(el) => {
          inputs.current[name] = el;
        }}
        type={type}
        value={values[name]}
        autoComplete={autoComplete}
        onChange={(e) => setValues((v) => ({ ...v, [name]: e.target.value }))}
        aria-invalid={problem?.field === name ? true : undefined}
        aria-describedby={problem?.field === name ? errorId : undefined}
      />
    </div>
  );

  const hasNext = view.status === 'ready' && view.data.hasNext;
  const patients = view.status === 'ready' ? view.data.patients : [];
  const first = (page - 1) * PATIENT_PAGE_SIZE + 1;
  let statusText = '';
  if (view.status === 'loading') statusText = filtered ? 'Searching...' : 'Loading patients...';
  if (view.status === 'empty') {
    if (page > 1) statusText = 'No patients on this page.';
    else statusText = filtered ? 'No patients found.' : 'No patients.';
  }
  if (view.status === 'ready') {
    const n = patients.length;
    const noun = n === 1 ? 'patient' : 'patients';
    if (page === 1 && !hasNext) statusText = filtered ? `${n} ${noun} found` : `${n} ${noun}`;
    else statusText = `Showing ${filtered ? 'matches' : 'patients'} ${first}–${first + n - 1}`;
  }
  const showPager = (view.status === 'ready' || view.status === 'empty') && (page > 1 || hasNext);

  return (
    <section className="patient-search">
      <h1 ref={headingRef} tabIndex={-1}>
        Find a patient
      </h1>
      <form className="search-form" role="search" aria-label="Patient search" onSubmit={onSubmit} noValidate>
        {field('name', 'Name', 'text')}
        {field('birthdate', 'Date of birth', 'date')}
        {field('identifier', 'MRN', 'text')}
        <button type="submit" className="btn btn-primary">
          Search
        </button>
        {filtered && (
          <Link className="btn btn-secondary" to="/dashboard" onClick={() => setValues({ name: '', birthdate: '', identifier: '' })}>
            Show all patients
          </Link>
        )}
      </form>
      {problem && (
        <p id={errorId} className="notice notice-warning" role="alert">
          {problem.message}
        </p>
      )}

      {!filtered && <RecentPatients />}

      <p className="muted" role="status">
        {statusText}
      </p>
      {view.status === 'error' && (
        <div className="page-state">
          <p className="notice notice-warning" role="alert">
            {filtered ? 'Patient search failed.' : 'The patient list could not be loaded.'}{' '}
            {view.error.kind === 'bad_request' ? 'The server refused the search terms.' : 'Please try again.'}
          </p>
          <button type="button" className="btn btn-primary" onClick={retry}>
            Try again
          </button>
        </div>
      )}
      {view.status === 'ready' && <PatientTable caption={filtered ? 'Search results' : 'Patients'} patients={patients} tableRef={tableRef} />}
      {showPager && (
        <nav className="pager" aria-label="Patient list pages">
          <button type="button" className="btn btn-secondary" disabled={page <= 1} onClick={() => goToPage(page - 1)} aria-label="Previous page">
            Previous
          </button>
          <span className="pager-page">Page {page}</span>
          <button type="button" className="btn btn-secondary" disabled={!hasNext} onClick={() => goToPage(page + 1)} aria-label="Next page">
            Next
          </button>
        </nav>
      )}
    </section>
  );
}
