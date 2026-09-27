import type { ReactNode } from 'react';
import { Spinner } from '../components/Card';
import { ClinicalCards } from '../components/ClinicalCards';
import { PatientHeader } from '../components/PatientHeader';
import { CHART_TITLE, useDocumentTitle } from '../components/useDocumentTitle';
import { isRetryable, patientErrorMessage } from '../data/errors';
import { usePatient } from '../data/hooks';

/**
 * The launched patient: the same header and cards as mode A, without patient
 * search. `account` ("Signed in as", Sign out) sits in the patient bar's
 * action slot: inside OpenEMR there is no separate app bar (plan L5). No edit
 * links: they need OpenEMR's numeric pid, which only the standard API gives,
 * and that refuses patient-context tokens.
 */
export function SmartPatientView({ patientId, account }: { patientId: string; account?: ReactNode }) {
  const { view, retry } = usePatient(patientId);
  useDocumentTitle(CHART_TITLE);
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
      // The launched patient came from OpenEMR itself, so "not found" means the
      // launch is stale, not that a link was mistyped.
      const message =
        view.error.kind === 'not_found'
          ? "OpenEMR couldn't find the launched patient. Open the dashboard again from the patient's chart in OpenEMR."
          : patientErrorMessage(view.error);
      if (message === null) return null; // The app shows the relaunch message.
      return (
        <section className="page-state">
          <p className="notice notice-warning" role="alert">
            {message}
          </p>
          {isRetryable(view.error) && (
            <div className="page-actions">
              <button type="button" className="btn btn-primary" onClick={retry}>
                Try again
              </button>
            </div>
          )}
        </section>
      );
    }
    case 'ready':
      return (
        <>
          <PatientHeader patient={view.data} actions={account} />
          <ClinicalCards key={patientId} patientId={patientId} />
        </>
      );
  }
}
