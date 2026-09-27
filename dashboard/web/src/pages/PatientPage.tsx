import { Link, useParams } from 'react-router';
import { PatientHeader } from '../components/PatientHeader';
import type { DataSourceError } from '../data/errors';
import { FHIR_ID, usePatient } from '../data/hooks';

function FindAnother() {
  return (
    <Link className="btn btn-secondary" to="/dashboard">
      Find another patient
    </Link>
  );
}

function errorText(error: DataSourceError): string | null {
  switch (error.kind) {
    case 'forbidden':
    case 'not_accessible':
      return 'You do not have access to this patient.';
    case 'not_found':
    case 'bad_request':
      return 'Patient not found.';
    case 'session_expired':
    case 'unauthenticated':
      return null; // The auth layer shows "session expired" and returns to sign-in.
    default:
      return 'The patient could not be loaded.';
  }
}

export function PatientPage() {
  const id = useParams().id ?? '';
  const { view, retry } = usePatient(id);

  if (!FHIR_ID.test(id)) {
    return (
      <section className="page-state">
        <p className="notice notice-warning" role="alert">
          Patient not found.
        </p>
        <FindAnother />
      </section>
    );
  }

  switch (view.status) {
    case 'idle':
    case 'loading':
    case 'empty':
      return (
        <p className="muted" role="status">
          Loading patient...
        </p>
      );
    case 'error': {
      const text = errorText(view.error);
      if (text === null) return null;
      const transient = ['upstream', 'timeout', 'network'].includes(view.error.kind);
      return (
        <section className="page-state">
          <p className="notice notice-warning" role="alert">
            {text}
          </p>
          <div className="page-actions">
            {transient && (
              <button type="button" className="btn btn-primary" onClick={retry}>
                Try again
              </button>
            )}
            <FindAnother />
          </div>
        </section>
      );
    }
    case 'ready':
      return (
        <>
          <PatientHeader patient={view.data} actions={<FindAnother />} />
          <p className="muted">The clinical cards are added in the next milestones.</p>
        </>
      );
  }
}
