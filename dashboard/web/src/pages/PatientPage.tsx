import { useEffect } from 'react';
import { Link, useParams } from 'react-router';
import { Spinner } from '../components/Card';
import { ClinicalCards } from '../components/ClinicalCards';
import { PatientHeader } from '../components/PatientHeader';
import { CHART_TITLE, useDocumentTitle } from '../components/useDocumentTitle';
import { DataSourceError, isRetryable, patientErrorMessage } from '../data/errors';
import { FHIR_ID, usePatient } from '../data/hooks';
import { useRecentPatients } from '../recent/recentContext';

function FindAnother() {
  return (
    <Link className="btn btn-secondary" to="/dashboard">
      Find another patient
    </Link>
  );
}

export function PatientPage() {
  useDocumentTitle(CHART_TITLE);
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
          {patientErrorMessage(new DataSourceError('not_found', 'malformed patient id'))}
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
      // A refused id reads as a bad link, the same as one OpenEMR doesn't know.
      const error = view.error.kind === 'bad_request' ? new DataSourceError('not_found', view.error.message) : view.error;
      const text = patientErrorMessage(error); // null: the auth layer shows "session expired"
      if (text === null) return null;
      return (
        <section className="page-state">
          <p className="notice notice-warning" role="alert">
            {text}
          </p>
          <div className="page-actions">
            {isRetryable(error) && (
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
