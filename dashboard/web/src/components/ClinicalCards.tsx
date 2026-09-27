// The clinical cards of OpenEMR's patient dashboard (demographics.php), in the
// PHP page order: Allergies, Medical Problems, Medications (one row of three),
// Prescriptions (full width below), Care Team (full width), Labs (left column).
// Parity reference: DASHBOARD_ANALYSIS_A1_PARITY.md §2–§7; FHIR gaps: README.
import { OPENEMR_WEB_URL } from '../config';
import { useCallback, useEffect, useId, useState, type ReactNode } from 'react';
import { useAllergies, useCareTeam, useLatestLabReport, useMedicationCards, useProblems, useResourceName, type MedicationCards } from '../data/cardHooks';
import { cardErrorMessage, isRetryable } from '../data/errors';
import { usePatientPid } from '../data/hooks';
import type { QueryView } from '../data/queryView';
import type { CareTeamMemberRow } from '../fhir/careTeam';
import { Card, Spinner } from './Card';
import './cards.css';

const NOTHING_RECORDED = 'Nothing Recorded';

// "—" cells are explained by one visible footnote per card (touch and keyboard
// users never see a hover tooltip); the column header points to it.
const REFILLS_FOOTNOTE = "Refills aren't available from OpenEMR's FHIR API.";
const STATUS_NOTE_FOOTNOTE = "Status and note aren't available from OpenEMR's FHIR API.";
const ENCOUNTER_FOOTNOTE = "The encounter isn't available from OpenEMR's FHIR API.";
const NAMES_HIDDEN_NOTE = "Member and facility names are hidden: your OpenEMR role can't read provider or facility records.";

/** Loading / error / empty states shared by every card; `ready` renders `children`. */
function CardContent<T>({
  view,
  retry,
  noun,
  empty,
  children,
}: {
  view: QueryView<T>;
  retry: () => void;
  /** Lower case, for messages: "allergies", "the care team". */
  noun: string;
  empty: ReactNode;
  children: (data: T) => ReactNode;
}) {
  switch (view.status) {
    case 'idle':
    case 'loading':
      return (
        <p className="card-state muted" role="status">
          <Spinner />
          Loading {noun}…
        </p>
      );
    case 'error': {
      const message = cardErrorMessage(view.error, noun);
      if (message === null) return null; // The auth layer ends the session and says so.
      return (
        <div className="card-state card-problem">
          <p className="card-error">{message}</p>
          {isRetryable(view.error) && (
            <button type="button" className="btn btn-secondary btn-sm" onClick={retry}>
              Try again
            </button>
          )}
        </div>
      );
    }
    case 'empty':
      return <div className="card-state">{empty}</div>;
    case 'ready':
      return <>{children(view.data)}</>;
  }
}

function Footnote({ id, children }: { id: string; children: ReactNode }) {
  return (
    <p className="card-footnote" id={id}>
      {children}
    </p>
  );
}

type EditLink = { href: string; label: string } | undefined;

function AllergiesCard({ patientId, edit }: { patientId: string; edit: EditLink }) {
  const { view, retry } = useAllergies(patientId);
  return (
    <Card id="allergy_ps_expand" title="Allergies" defaultExpanded edit={edit}>
      <CardContent view={view} retry={retry} noun="allergies" empty={NOTHING_RECORDED}>
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
      </CardContent>
    </Card>
  );
}

function ProblemsCard({ patientId, edit }: { patientId: string; edit: EditLink }) {
  const { view, retry } = useProblems(patientId);
  return (
    <Card id="medical_problem_ps_expand" title="Medical Problems" defaultExpanded edit={edit}>
      <CardContent view={view} retry={retry} noun="medical problems" empty={NOTHING_RECORDED}>
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
      </CardContent>
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
      <CardContent view={v} retry={retry} noun="medications" empty={NOTHING_RECORDED}>
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
      </CardContent>
    </Card>
  );
}

function PrescriptionsCard({ view, retry, edit }: { view: QueryView<MedicationCards>; retry: () => void; edit: EditLink }) {
  const v = splitView(view, 'prescriptions');
  const footnoteId = useId();
  return (
    <Card id="prescriptions_ps_expand" title="Prescriptions" defaultExpanded edit={edit} className="card-wide">
      <CardContent view={v} retry={retry} noun="prescriptions" empty="None">
        {(table) =>
          table.total === 0 ? (
            <div className="card-state">None</div>
          ) : (
            <>
              <div className="table-responsive" tabIndex={0} role="region" aria-label="Prescriptions table">
                <table className="card-table">
                  <thead>
                    <tr>
                      <th scope="col">Drug</th>
                      <th scope="col">Details</th>
                      <th scope="col">Qty</th>
                      <th scope="col" aria-describedby={footnoteId}>
                        Refills
                      </th>
                      <th scope="col">Filled</th>
                    </tr>
                  </thead>
                  <tbody>
                    {table.rows.map((r) => (
                      <tr key={r.id}>
                        <td>{r.drug}</td>
                        <td>{r.details}</td>
                        <td>{r.qty}</td>
                        <td>—</td>
                        <td>{r.filled}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <Footnote id={footnoteId}>{REFILLS_FOOTNOTE}</Footnote>
            </>
          )
        }
      </CardContent>
    </Card>
  );
}

function CombinedMedicationsCard({ view, edit }: { view: QueryView<MedicationCards>; edit: EditLink }) {
  if (view.status !== 'ready' || view.data.kind !== 'combined') return null;
  const rows = view.data.rows;
  return (
    <Card id="medication_ps_expand" title="Medications and prescriptions (combined)" defaultExpanded edit={edit}>
      <p className="notice notice-warning card-note">
        This sign-in can't read OpenEMR's medication list, so medications and prescriptions are shown together, each order once.
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

/**
 * A member or facility name. A name the user's role can't read shows "—" and
 * reports it, so the card explains it once instead of in every row.
 */
function NameCell({ type, id, onForbidden }: { type: 'Practitioner' | 'Organization'; id: string; onForbidden: () => void }) {
  const { view } = useResourceName(type, id);
  const forbidden = view.status === 'error' && (view.error.kind === 'forbidden' || view.error.kind === 'not_accessible');
  useEffect(() => {
    if (forbidden) onForbidden();
  }, [forbidden, onForbidden]);
  switch (view.status) {
    case 'ready':
      return <>{view.data}</>;
    case 'idle':
    case 'loading':
      return <span className="muted">Loading…</span>;
    case 'error':
      return forbidden ? <>—</> : <span className="muted">Name not available</span>;
    case 'empty':
      return <span className="muted">Name not available</span>;
  }
}

function MemberRow({ m, onForbidden }: { m: CareTeamMemberRow; onForbidden: () => void }) {
  return (
    <tr>
      <td>{m.type === 'provider' ? <span className="badge badge-primary">Provider</span> : <span className="badge badge-info">Related Person</span>}</td>
      <td>
        {m.type === 'provider' ? (
          <NameCell type="Practitioner" id={m.memberId} onForbidden={onForbidden} />
        ) : (
          // RelatedPerson reads are not in the dashboard's scope set.
          <span className="muted">Name not available</span>
        )}
      </td>
      <td>{m.role}</td>
      <td>{m.facilityId ? <NameCell type="Organization" id={m.facilityId} onForbidden={onForbidden} /> : ''}</td>
      <td>{m.since}</td>
      <td>—</td>
      <td>—</td>
    </tr>
  );
}

function CareTeamCard({ patientId, edit }: { patientId: string; edit: EditLink }) {
  const { view, retry } = useCareTeam(patientId);
  // Set once any member or facility name comes back forbidden (mode A, Physicians: gap G9).
  const [namesHidden, setNamesHidden] = useState(false);
  const markNamesHidden = useCallback(() => setNamesHidden(true), []);
  const uid = useId();
  const hiddenNoteId = `${uid}-hidden`;
  const footnoteId = `${uid}-footnote`;
  const hiddenRef = namesHidden ? hiddenNoteId : undefined;
  return (
    <Card id="careteam_ps_expand" title="Care Team" defaultExpanded={false} edit={edit} className="card-wide">
      <CardContent view={view} retry={retry} noun="the care team" empty={NOTHING_RECORDED}>
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
              {namesHidden && (
                <p className="card-footnote care-team-hidden" id={hiddenNoteId}>
                  {NAMES_HIDDEN_NOTE}
                </p>
              )}
              <div className="table-responsive" tabIndex={0} role="region" aria-label="Care team members">
                <table className="card-table">
                  <thead>
                    <tr>
                      <th scope="col">Type</th>
                      <th scope="col" aria-describedby={hiddenRef}>
                        Member
                      </th>
                      <th scope="col">Role</th>
                      <th scope="col" aria-describedby={hiddenRef}>
                        Facility
                      </th>
                      <th scope="col">Since</th>
                      <th scope="col" aria-describedby={footnoteId}>
                        Status
                      </th>
                      <th scope="col" aria-describedby={footnoteId}>
                        Note
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {team.members.map((m) => (
                      <MemberRow key={m.key} m={m} onForbidden={markNamesHidden} />
                    ))}
                  </tbody>
                </table>
              </div>
              <Footnote id={footnoteId}>{STATUS_NOTE_FOOTNOTE}</Footnote>
            </>
          )
        }
      </CardContent>
    </Card>
  );
}

/**
 * PHP labdata_fragment.php: "Most recent lab data:", "Procedure: <name> (<raw
 * date_collected>)", "Encounter: <id>" and a link to labdata.php. The FHIR
 * equivalents are the report's test names and date_report (G16, G17); the
 * encounter number is not in FHIR. labdata.php reads the patient from the
 * OpenEMR session, so the link goes to the PHP dashboard with set_pid instead,
 * whose Labs card links on to it. PHP shows no pencil (its "Trend" button is
 * not rendered by card_base), and neither does this card.
 */
function LabsCard({ patientId, allLabsHref }: { patientId: string; allLabsHref: string | undefined }) {
  const { view, retry } = useLatestLabReport(patientId);
  const footnoteId = useId();
  return (
    <Card id="labdata_ps_expand" title="Labs" defaultExpanded={false}>
      <CardContent view={view} retry={retry} noun="lab data" empty="No lab data documented.">
        {(report) =>
          report && (
            <div className="labdata">
              <p>
                <b>Most recent lab data:</b>
                <br />
                <span>{`Tests: ${report.tests}${report.date ? ` (${report.date})` : ''}`}</span>
                <br />
                <span>
                  Encounter: <span aria-describedby={footnoteId}>—</span>
                </span>
              </p>
              {allLabsHref && (
                <p>
                  <a href={allLabsHref} target="_blank" rel="noopener noreferrer">
                    View and graph all lab data in OpenEMR
                  </a>
                </p>
              )}
              <Footnote id={footnoteId}>{ENCOUNTER_FOOTNOTE}</Footnote>
            </div>
          )
        }
      </CardContent>
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
      <div className="cards-row cards-row-left">
        <LabsCard patientId={patientId} allLabsHref={dashboardHref} />
      </div>
    </div>
  );
}
