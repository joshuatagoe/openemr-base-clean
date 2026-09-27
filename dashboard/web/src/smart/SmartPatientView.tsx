import { ClinicalCards } from '../components/ClinicalCards';
import { PatientHeader } from '../components/PatientHeader';
import { usePatient } from '../data/hooks';

/**
 * The launched patient: the same header and cards as mode A, without patient
 * search. No edit links: they need OpenEMR's numeric pid, which only the
 * standard API gives, and that refuses patient-context tokens.
 */
export function SmartPatientView({ patientId }: { patientId: string }) {
  const { view, retry } = usePatient(patientId);
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
      if (view.error.kind === 'session_expired') return null; // The app shows the relaunch message.
      const transient = ['upstream', 'timeout', 'network'].includes(view.error.kind);
      return (
        <section className="page-state">
          <p className="notice notice-warning" role="alert">
            {view.error.kind === 'forbidden' || view.error.kind === 'not_accessible' ? 'You do not have access to this patient.' : 'The patient could not be loaded.'}
          </p>
          {transient && (
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
          <PatientHeader patient={view.data} actions={<span className="muted">To open another patient, change the chart in OpenEMR.</span>} />
          <ClinicalCards key={patientId} patientId={patientId} />
        </>
      );
  }
}
