import { useEffect } from 'react';
import { Link, useParams } from 'react-router';
import { Spinner } from '../components/Card';
import { ClinicalCards } from '../components/ClinicalCards';
import { PatientHeader } from '../components/PatientHeader';
import type { DataSourceError } from '../data/errors';
import { FHIR_ID, usePatient } from '../data/hooks';
import { useRecentPatients } from '../recent/recentContext';

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

// The tab title names the page, never the patient: tab strips and browser
// history are visible to anyone at the screen (PHI).
const TITLE = 'Chart – Patient Dashboard';

function useDocumentTitle(title: string): void {
  useEffect(() => {
    const previous = document.title;
    document.title = title;
    return () => {
      document.title = previous;
    };
  }, [title]);
}

export function PatientPage() {
  useDocumentTitle(TITLE);
  const id = useParams().id ?? '';
  const { view, retry } = usePatient(id);
  const recent = useRecentPatients();
  const opened = view.status === 'ready';
  const addRecent = recent?.add;

  // Only a patient that actually opened joins the recent list (its id, nothing else).
  useEffect(() => {
    if (opened) addRecent?.(id);
  }, [opened, id, addRecent]);

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
      // Holds the patient bar's place so the page doesn't jump when the data arrives.
      return (
        <div className="patient-bar patient-bar-loading">
          <p className="muted" role="status">
            <Spinner />
            Loading patient…
          </p>
        </div>
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
          <ClinicalCards key={id} patientId={id} />
        </>
      );
  }
}
