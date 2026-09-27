// The five clinical cards of OpenEMR's patient dashboard (demographics.php),
// in the PHP page order: Allergies, Medical Problems, Medications (one row of
// three), Prescriptions (full width below), Care Team (full width).
// Parity reference: DASHBOARD_ANALYSIS_A1_PARITY.md §2–§6; FHIR gaps: README.
import { OPENEMR_WEB_URL } from '../config';
import { useAllergies, useCareTeam, useMedicationCards, useProblems, useResourceName, type MedicationCards } from '../data/cardHooks';
import { usePatientPid } from '../data/hooks';
import type { QueryView } from '../data/queryView';
import type { CareTeamMemberRow } from '../fhir/careTeam';
import { Card, CardView } from './Card';

const NOTHING_RECORDED = 'Nothing Recorded';
const NOT_IN_FHIR = "Value not available: OpenEMR's FHIR API does not provide it";

type EditLink = { href: string; label: string } | undefined;

function AllergiesCard({ patientId, edit }: { patientId: string; edit: EditLink }) {
  const { view, retry } = useAllergies(patientId);
  return (
    <Card id="allergy_ps_expand" title="Allergies" defaultExpanded edit={edit}>
      <CardView view={view} retry={retry} text={{ noun: 'allergies', subject: 'Allergies', empty: NOTHING_RECORDED }}>
        {(rows) => (
          <ul className="pami-list">
            {rows.map((r) => (
              <li key={r.id} title={r.tooltip}>
                {r.name}
                {r.severity && (
                  <>
                    {' ('}
                    <span className={r.highlight ? 'severity-alert' : undefined}>{r.severity}</span>
                    {')'}
                  </>
                )}
              </li>
            ))}
          </ul>
        )}
      </CardView>
    </Card>
  );
}

function ProblemsCard({ patientId, edit }: { patientId: string; edit: EditLink }) {
  const { view, retry } = useProblems(patientId);
  return (
    <Card id="medical_problem_ps_expand" title="Medical Problems" defaultExpanded edit={edit}>
      <CardView view={view} retry={retry} text={{ noun: 'medical problems', subject: 'Medical problems', empty: NOTHING_RECORDED }}>
        {(rows) => (
          <ul className="pami-list">
            {rows.map((r) =>
              r.resolved ? (
                <li key={r.key}>
                  {r.title} <span className="muted">(Resolved)</span>
                </li>
              ) : (
                <li key={r.key}>{r.title}</li>
              ),
            )}
          </ul>
        )}
      </CardView>
    </Card>
  );
}

/** Narrows the shared medication query to one card; the combined case is rendered elsewhere. */
function splitView<K extends 'medications' | 'prescriptions'>(
  view: QueryView<MedicationCards>,
  key: K,
): QueryView<Extract<MedicationCards, { kind: 'split' }>[K]> {
  if (view.status !== 'ready') return view;
  if (view.data.kind !== 'split') return { status: 'loading' };
  return { status: 'ready', data: view.data[key] };
}

function MedicationsCard({ view, retry, edit }: { view: QueryView<MedicationCards>; retry: () => void; edit: EditLink }) {
  const v = splitView(view, 'medications');
  return (
    <Card id="medication_ps_expand" title="Medications" defaultExpanded edit={edit}>
      <CardView view={v} retry={retry} text={{ noun: 'medications', subject: 'Medications', empty: NOTHING_RECORDED }}>
        {(rows) =>
          rows.length === 0 ? (
            <div className="card-state">{NOTHING_RECORDED}</div>
          ) : (
            <ul className="pami-list">
              {rows.map((r) => (
                <li key={r.key}>
                  <span>{r.title}</span>
                  {r.dosage && (
                    <>
                      {' '}
                      <span>{r.dosage}</span>
                    </>
                  )}
                </li>
              ))}
            </ul>
          )
        }
      </CardView>
    </Card>
  );
}

function PrescriptionsCard({ view, retry, edit }: { view: QueryView<MedicationCards>; retry: () => void; edit: EditLink }) {
  const v = splitView(view, 'prescriptions');
  return (
    <Card id="prescriptions_ps_expand" title="Prescriptions" defaultExpanded edit={edit} className="card-wide">
      <CardView view={v} retry={retry} text={{ noun: 'prescriptions', subject: 'Prescriptions', empty: 'None' }}>
        {(table) =>
          table.total === 0 ? (
            <div className="card-state">None</div>
          ) : (
            <div className="table-responsive">
              <table className="card-table">
                <thead>
                  <tr>
                    <th scope="col">Drug</th>
                    <th scope="col">Details</th>
                    <th scope="col">Qty</th>
                    <th scope="col">Refills</th>
                    <th scope="col">Filled</th>
                  </tr>
                </thead>
                <tbody>
                  {table.rows.map((r) => (
                    <tr key={r.id}>
                      <td>{r.drug}</td>
                      <td>{r.details}</td>
                      <td>{r.qty}</td>
                      <td title={NOT_IN_FHIR}>—</td>
                      <td>{r.filled}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </CardView>
    </Card>
  );
}

function CombinedMedicationsCard({ view, edit }: { view: QueryView<MedicationCards>; edit: EditLink }) {
  if (view.status !== 'ready' || view.data.kind !== 'combined') return null;
  const rows = view.data.rows;
  return (
    <Card id="medication_ps_expand" title="Medications and prescriptions (combined)" defaultExpanded edit={edit}>
      <p className="notice notice-warning card-note">
        OpenEMR's medication list could not be read with this sign-in, so medications and prescriptions cannot be told apart. Every current
        medication order is listed once.
      </p>
      {rows.length === 0 ? (
        <div className="card-state">{NOTHING_RECORDED}</div>
      ) : (
        <ul className="pami-list">
          {rows.map((r) => (
            <li key={r.id}>
              <span>{r.name}</span>
              {r.dosage && (
                <>
                  {' '}
                  <span>{r.dosage}</span>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function NameCell({ type, id }: { type: 'Practitioner' | 'Organization'; id: string }) {
  const { view } = useResourceName(type, id);
  switch (view.status) {
    case 'ready':
      return <>{view.data}</>;
    case 'idle':
    case 'loading':
      return <span className="muted">Loading…</span>;
    case 'error':
      return (
        <span className="muted">
          {view.error.kind === 'forbidden' || view.error.kind === 'not_accessible' ? 'Name not available (permission)' : 'Name not available'}
        </span>
      );
    case 'empty':
      return <span className="muted">Name not available</span>;
  }
}

function MemberRow({ m }: { m: CareTeamMemberRow }) {
  return (
    <tr>
      <td>{m.type === 'provider' ? <span className="badge badge-primary">Provider</span> : <span className="badge badge-info">Related Person</span>}</td>
      <td>
        {m.type === 'provider' ? (
          <NameCell type="Practitioner" id={m.memberId} />
        ) : (
          // RelatedPerson reads are not in the dashboard's scope set.
          <span className="muted">Name not available</span>
        )}
      </td>
      <td>{m.role}</td>
      <td>{m.facilityId ? <NameCell type="Organization" id={m.facilityId} /> : ''}</td>
      <td>{m.since}</td>
      <td title={NOT_IN_FHIR}>—</td>
      <td title={NOT_IN_FHIR}>—</td>
    </tr>
  );
}

function CareTeamCard({ patientId, edit }: { patientId: string; edit: EditLink }) {
  const { view, retry } = useCareTeam(patientId);
  return (
    <Card id="careteam_ps_expand" title="Care Team" defaultExpanded={false} edit={edit} className="card-wide">
      <CardView view={view} retry={retry} text={{ noun: 'the care team', subject: 'The care team', empty: NOTHING_RECORDED }}>
        {(team) =>
          team && (
            <>
              <h3 className="care-team-name">
                {team.name}
                {team.name && (
                  <>
                    {' '}
                    <span className={`badge ${team.badgeClass}`}>{team.statusLabel}</span>
                  </>
                )}
              </h3>
              <div className="table-responsive">
                <table className="card-table">
                  <thead>
                    <tr>
                      <th scope="col">Type</th>
                      <th scope="col">Member</th>
                      <th scope="col">Role</th>
                      <th scope="col">Facility</th>
                      <th scope="col">Since</th>
                      <th scope="col">Status</th>
                      <th scope="col">Note</th>
                    </tr>
                  </thead>
                  <tbody>
                    {team.members.map((m) => (
                      <MemberRow key={m.key} m={m} />
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )
        }
      </CardView>
    </Card>
  );
}

export interface ClinicalCardsProps {
  patientId: string;
  /** OpenEMR web origin for the edit links (VITE_OPENEMR_WEB_URL); no links when unset. */
  openemrWebUrl?: string | undefined;
}

export function ClinicalCards({ patientId, openemrWebUrl = OPENEMR_WEB_URL }: ClinicalCardsProps) {
  const pidView = usePatientPid(patientId).view;
  const meds = useMedicationCards(patientId);
  const pid = pidView.status === 'ready' ? pidView.data : undefined;
  const base = openemrWebUrl?.replace(/\/+$/, '');
  // The PHP issue editor (stats_full.php) reads the patient from the OpenEMR
  // session, so a direct link could open another patient's list. The links go
  // to the PHP dashboard with set_pid (which selects this patient first) or to
  // a screen that takes the patient id explicitly (prescriptions).
  const dashboardHref = base && pid ? `${base}/interface/patient_file/summary/demographics.php?set_pid=${encodeURIComponent(pid)}` : undefined;
  const edit = (label: string): EditLink => (dashboardHref ? { href: dashboardHref, label } : undefined);
  const rxEdit: EditLink = base && pid ? { href: `${base}/controller.php?prescription&list&id=${encodeURIComponent(pid)}`, label: 'Edit prescriptions in OpenEMR' } : undefined;
  const combined = meds.view.status === 'ready' && meds.view.data.kind === 'combined';

  return (
    <div className="clinical-cards">
      <div className="cards-row cards-row-pami">
        <AllergiesCard patientId={patientId} edit={edit('Edit allergies in OpenEMR')} />
        <ProblemsCard patientId={patientId} edit={edit('Edit medical problems in OpenEMR')} />
        {combined ? (
          <CombinedMedicationsCard view={meds.view} edit={edit('Edit medications in OpenEMR')} />
        ) : (
          <MedicationsCard view={meds.view} retry={meds.retry} edit={edit('Edit medications in OpenEMR')} />
        )}
      </div>
      {!combined && (
        <div className="cards-row">
          <PrescriptionsCard view={meds.view} retry={meds.retry} edit={rxEdit} />
        </div>
      )}
      <div className="cards-row">
        <CareTeamCard patientId={patientId} edit={edit('Edit care team in OpenEMR')} />
      </div>
    </div>
  );
}
