import { useId, useRef, useState, type FormEvent } from 'react';
import { Link, useSearchParams } from 'react-router';
import { useFocusOnChange } from '../components/useFocusOnChange';
import { DATE_DISPLAY_FORMAT } from '../config';
import { patientListName, patientMrn, patientSexLabel } from '../fhir/patient';
import { formatShortDate } from '../format/date';
import { usePatientSearch, type PatientSearchCriteria } from '../data/hooks';

type Field = 'name' | 'birthdate' | 'identifier';

// The same patterns the BFF allow-list enforces, so a refused search is
// explained here instead of coming back as a 400.
const RULES: Readonly<Record<Field, { pattern: RegExp; message: string }>> = {
  name: { pattern: /^[\p{L}\p{M}' .-]{1,64}$/u, message: 'Names may contain letters, spaces, apostrophes, hyphens and dots.' },
  birthdate: { pattern: /^\d{4}-\d{2}-\d{2}$/, message: 'Enter the date of birth as YYYY-MM-DD.' },
  identifier: { pattern: /^[A-Za-z0-9._|:-]{1,64}$/, message: 'MRNs may contain letters, digits and . _ | : -' },
};
const FIELDS: readonly Field[] = ['name', 'birthdate', 'identifier'];

type Validation = { ok: true; criteria: PatientSearchCriteria } | { ok: false; field: Field | null; message: string };

function validate(values: Record<Field, string>): Validation {
  const criteria: PatientSearchCriteria = {};
  for (const f of FIELDS) {
    const v = values[f].trim();
    if (!v) continue;
    if (!RULES[f].pattern.test(v)) return { ok: false, field: f, message: RULES[f].message };
    criteria[f] = v;
  }
  if (Object.keys(criteria).length === 0) return { ok: false, field: null, message: 'Enter a name, date of birth or MRN.' };
  return { ok: true, criteria };
}

function fromUrl(params: URLSearchParams): Record<Field, string> {
  return { name: params.get('name') ?? '', birthdate: params.get('birthdate') ?? '', identifier: params.get('identifier') ?? '' };
}

export function PatientSearchPage() {
  const [params, setParams] = useSearchParams();
  const urlValues = fromUrl(params);
  const urlCheck = validate(urlValues);
  const [values, setValues] = useState(urlValues);
  const [problem, setProblem] = useState<{ field: Field | null; message: string } | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const inputs = useRef<Partial<Record<Field, HTMLInputElement | null>>>({});
  const errorId = useId();
  useFocusOnChange(headingRef, 'mount');

  const { view, retry } = usePatientSearch(urlCheck.ok ? urlCheck.criteria : undefined);

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    const check = validate(values);
    if (!check.ok) {
      setProblem({ field: check.field, message: check.message });
      if (check.field) inputs.current[check.field]?.focus();
      return;
    }
    setProblem(null);
    setParams(new URLSearchParams(check.criteria as Record<string, string>));
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

  let statusText = '';
  if (view.status === 'loading') statusText = 'Searching...';
  if (view.status === 'empty') statusText = 'No patients found.';
  if (view.status === 'ready') {
    const n = view.data.patients.length;
    statusText = `${n} ${n === 1 ? 'patient' : 'patients'} found`;
  }

  const patients =
    view.status === 'ready' ? [...view.data.patients].sort((a, b) => patientListName(a).localeCompare(patientListName(b))) : [];

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
      </form>
      {problem && (
        <p id={errorId} className="notice notice-warning" role="alert">
          {problem.message}
        </p>
      )}

      <p className="muted" role="status">
        {statusText}
      </p>
      {view.status === 'error' && (
        <div className="page-state">
          <p className="notice notice-warning" role="alert">
            Patient search failed. {view.error.kind === 'bad_request' ? 'The server refused the search terms.' : 'Please try again.'}
          </p>
          <button type="button" className="btn btn-primary" onClick={retry}>
            Try again
          </button>
        </div>
      )}
      {view.status === 'ready' && (
        <>
          <table className="results">
            <caption>Search results</caption>
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
          {view.data.truncated && <p className="muted">More patients match. Add a date of birth or MRN to narrow the search.</p>}
        </>
      )}
    </section>
  );
}
