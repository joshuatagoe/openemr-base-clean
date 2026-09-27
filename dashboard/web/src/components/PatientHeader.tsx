import type { Patient } from 'fhir/r4';
import { useRef, type ReactNode } from 'react';
import { DATE_DISPLAY_FORMAT } from '../config';
import { patientBarName, patientDobAgeLine, patientMrn, patientSexLabel } from '../fhir/patient';
import { localToday } from '../format/date';
import { useFocusOnChange } from './useFocusOnChange';

/**
 * The persistent patient bar, matching OpenEMR's tab-frame patient bar
 * (patient_data_template.php + demographics.php setMyPatient): "First Last",
 * "(pubpid)" muted, then "DOB: <date> Age: <age>" (or "Age at death").
 * Added for the challenge: "Birth Sex: <sex>" from FHIR Patient.gender, which
 * OpenEMR fills from patient_data.sex, the field its Demographics card labels
 * "Birth Sex". Active status is deliberately not shown: OpenEMR's FHIR
 * hard-codes active = true, and the PHP bar does not show it (see README).
 */
export function PatientHeader({ patient, actions }: { patient: Patient; actions?: ReactNode }) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  useFocusOnChange(headingRef, patient.id);
  const mrn = patientMrn(patient);
  const sex = patientSexLabel(patient);
  return (
    <section className="patient-bar" aria-label="Patient">
      <div className="patient-bar-main">
        <div className="patient-bar-name">
          <h1 className="patient-name" ref={headingRef} tabIndex={-1}>
            {patientBarName(patient)}
          </h1>
          {mrn !== undefined && (
            <>
              {' '}
              <small className="patient-mrn muted">({mrn})</small>
            </>
          )}
        </div>
        <div className="patient-dob">
          {patientDobAgeLine(patient, { today: localToday(), dateFormat: DATE_DISPLAY_FORMAT })}
          {sex && (
            <>
              {' '}
              <span className="patient-sex">
                <span>Birth Sex:</span> {sex}
              </span>
            </>
          )}
        </div>
      </div>
      {actions && <div className="patient-bar-actions">{actions}</div>}
    </section>
  );
}
