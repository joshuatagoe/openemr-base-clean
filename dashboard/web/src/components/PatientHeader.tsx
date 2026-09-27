import type { Patient } from 'fhir/r4';
import { useRef, type ReactNode } from 'react';
import { DATE_DISPLAY_FORMAT } from '../config';
import { patientBarName, patientDobAgeLine, patientMrn } from '../fhir/patient';
import { localToday } from '../format/date';
import { useFocusOnChange } from './useFocusOnChange';

/**
 * The persistent patient bar, matching OpenEMR's tab-frame patient bar
 * (patient_data_template.php + demographics.php setMyPatient): "First Last",
 * "(pubpid)" muted, then "DOB: <date> Age: <age>" (or "Age at death").
 * Sex and active status are deliberately not shown (PHP parity; see README).
 */
export function PatientHeader({ patient, actions }: { patient: Patient; actions?: ReactNode }) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  useFocusOnChange(headingRef, patient.id);
  const mrn = patientMrn(patient);
  return (
    <section className="patient-bar" aria-label="Patient">
      <div className="patient-bar-main">
        <h1 className="patient-name" ref={headingRef} tabIndex={-1}>
          {patientBarName(patient)}
        </h1>
        {mrn !== undefined && (
          <>
            {' '}
            <small className="patient-mrn muted">({mrn})</small>
          </>
        )}
        <div className="patient-dob">{patientDobAgeLine(patient, { today: localToday(), dateFormat: DATE_DISPLAY_FORMAT })}</div>
      </div>
      {actions && <div className="patient-bar-actions">{actions}</div>}
    </section>
  );
}
