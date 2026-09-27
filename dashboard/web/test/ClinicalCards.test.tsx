import { QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Bundle, FhirResource } from 'fhir/r4';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ClinicalCards } from '../src/components/ClinicalCards';
import type { DataSource, FhirReadType, FhirSearchType, SearchParams, StdMedicationRow } from '../src/data/DataSource';
import { DataSourceContext } from '../src/data/DataSourceContext';
import { DataSourceError } from '../src/data/errors';
import { createQueryClient } from '../src/data/queryClient';
import {
  allergy,
  bundle,
  careTeam,
  condition,
  ENC_2,
  facilityParticipant,
  labObs,
  medReq,
  ORG_1,
  organization,
  PATIENT_A_ID,
  PATIENT_B_ID,
  PID_A,
  PRACT_1,
  PRACT_2,
  practitioner,
  practitionerParticipant,
  RELATED_1,
  stdMed,
} from './fixtures/clinical';

const forbidden = () => new DataSourceError('forbidden', 'Request failed (403).', 403);
const upstream = () => new DataSourceError('upstream', 'Request failed (502).', 502);

type SearchHandler = (params: SearchParams) => Promise<Bundle>;

interface FakeOptions {
  search?: Partial<Record<FhirSearchType, SearchHandler>>;
  read?: (type: FhirReadType, id: string) => Promise<FhirResource>;
  medList?: (pid: string) => Promise<StdMedicationRow[]>;
  pid?: () => Promise<string>;
}

const empty: SearchHandler = () => Promise.resolve(bundle());

function fakeSource(o: FakeOptions = {}) {
  const search = vi.fn((type: FhirSearchType, params: SearchParams) => (o.search?.[type] ?? empty)(params));
  const read = vi.fn((type: FhirReadType, id: string) => (o.read ? o.read(type, id) : Promise.reject(forbidden())));
  const ds: DataSource = {
    transport: 'bff',
    search: search as unknown as DataSource['search'],
    read: read as unknown as DataSource['read'],
    patientMedicationList: vi.fn((pid: string) => (o.medList ? o.medList(pid) : Promise.resolve([]))),
    patientPid: vi.fn(() => (o.pid ? o.pid() : Promise.resolve(PID_A))),
  };
  return { ds, search, read };
}

function renderCards(ds: DataSource, props: { openemrWebUrl?: string; patientId?: string } = {}) {
  const qc = createQueryClient({ retryDelay: 0 });
  const utils = render(
    <QueryClientProvider client={qc}>
      <DataSourceContext.Provider value={ds}>
        <ClinicalCards patientId={props.patientId ?? PATIENT_A_ID} openemrWebUrl={props.openemrWebUrl} />
      </DataSourceContext.Provider>
    </QueryClientProvider>,
  );
  return { ...utils, qc };
}

const card = (title: string) => screen.getByRole('region', { name: title });

beforeEach(() => {
  window.localStorage.clear();
});

describe('page layout', () => {
  it('renders the six cards in PHP order with PHP titles', () => {
    renderCards(fakeSource().ds);
    const titles = screen.getAllByRole('region').map((r) => within(r).getByRole('heading', { level: 2 }).textContent);
    expect(titles).toEqual(['Allergies', 'Medical Problems', 'Medications', 'Prescriptions', 'Care Team', 'Labs']);
  });

  it('expands Allergies, Medical Problems, Medications and Prescriptions by default; Care Team and Labs start collapsed', () => {
    renderCards(fakeSource().ds);
    for (const t of ['Allergies', 'Medical Problems', 'Medications', 'Prescriptions']) {
      expect(within(card(t)).getByRole('button', { name: t })).toHaveAttribute('aria-expanded', 'true');
    }
    for (const t of ['Care Team', 'Labs']) {
      expect(within(card(t)).getByRole('button', { name: t })).toHaveAttribute('aria-expanded', 'false');
    }
  });

  it('toggling a card collapses it and the choice survives a remount (per browser)', async () => {
    const { ds } = fakeSource({ search: { AllergyIntolerance: () => Promise.resolve(bundle(allergy('a1', 'Penicillin'))) } });
    const first = renderCards(ds);
    await within(card('Allergies')).findByText(/Penicillin/);
    await userEvent.click(within(card('Allergies')).getByRole('button', { name: 'Allergies' }));
    expect(within(card('Allergies')).getByRole('button', { name: 'Allergies' })).toHaveAttribute('aria-expanded', 'false');
    expect(within(card('Allergies')).queryByText(/Penicillin/)).not.toBeVisible();
    first.unmount();
    renderCards(ds);
    expect(within(card('Allergies')).getByRole('button', { name: 'Allergies' })).toHaveAttribute('aria-expanded', 'false');
  });

  it('edit links go out to the OpenEMR screens for this patient when the OpenEMR URL is configured', async () => {
    renderCards(fakeSource().ds, { openemrWebUrl: 'https://emr.invalid/' });
    const dashboard = `https://emr.invalid/interface/patient_file/summary/demographics.php?set_pid=${PID_A}`;
    for (const t of ['Allergies', 'Medical Problems', 'Medications', 'Care Team']) {
      expect(await within(card(t)).findByRole('link', { name: `Edit ${t.toLowerCase()} in OpenEMR` })).toHaveAttribute('href', dashboard);
    }
    const rx = await within(card('Prescriptions')).findByRole('link', { name: 'Edit prescriptions in OpenEMR' });
    expect(rx).toHaveAttribute('href', `https://emr.invalid/controller.php?prescription&list&id=${PID_A}`);
    expect(rx).toHaveAttribute('target', '_blank');
    expect(rx).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('no edit links without a configured OpenEMR URL', async () => {
    renderCards(fakeSource().ds);
    await within(card('Allergies')).findByText('Nothing Recorded');
    expect(screen.queryAllByRole('link')).toEqual([]);
  });
});

describe('Allergies card', () => {
  it('loading, then "name (severity)" rows with the reaction tooltip and high-risk highlight', async () => {
    let resolve: (b: Bundle) => void = () => undefined;
    const { ds } = fakeSource({
      search: { AllergyIntolerance: () => new Promise<Bundle>((r) => (resolve = r)) },
    });
    renderCards(ds);
    expect(within(card('Allergies')).getByRole('status')).toHaveTextContent('Loading allergies');
    resolve(bundle(allergy('a1', 'Penicillin', { criticality: 'low', reaction: 'Rash' }), allergy('a2', 'Peanut', { criticality: 'high', reaction: 'Hives' })));
    const item = await within(card('Allergies')).findByTitle('Penicillin Reaction: Rash - Low Risk');
    expect(item).toHaveTextContent('Penicillin (Low Risk)');
    const peanut = within(card('Allergies')).getByTitle('Peanut Reaction: Hives - High Risk');
    expect(peanut).toHaveTextContent('Peanut (High Risk)');
    expect(within(peanut).getByText('High Risk')).toHaveClass('severity-alert');
    expect(within(item).getByText('Low Risk')).not.toHaveClass('severity-alert');
  });

  it('empty: "Nothing Recorded" (FHIR cannot say "No Known Allergies")', async () => {
    renderCards(fakeSource().ds);
    expect(await within(card('Allergies')).findByText('Nothing Recorded')).toBeInTheDocument();
  });

  it('403: permission message, no retry button', async () => {
    const { ds, search } = fakeSource({ search: { AllergyIntolerance: () => Promise.reject(forbidden()) } });
    renderCards(ds);
    expect(await within(card('Allergies')).findByText("You don't have permission to view allergies.")).toBeInTheDocument();
    expect(within(card('Allergies')).queryByRole('button', { name: 'Try again' })).toBeNull();
    expect(search.mock.calls.filter(([t]) => t === 'AllergyIntolerance')).toHaveLength(1);
  });

  it('upstream error: message and a working "Try again"', async () => {
    let fail = true;
    const { ds } = fakeSource({
      search: { AllergyIntolerance: () => (fail ? Promise.reject(upstream()) : Promise.resolve(bundle(allergy('a1', 'Latex')))) },
    });
    renderCards(ds);
    const retry = await within(card('Allergies')).findByRole('button', { name: 'Try again' });
    expect(within(card('Allergies')).getByText('Allergies could not be loaded.')).toBeInTheDocument();
    fail = false;
    await userEvent.click(retry);
    expect(await within(card('Allergies')).findByText(/Latex/)).toBeInTheDocument();
  });

  it('subject guard: results for another patient are dropped and only a count is logged', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const { ds } = fakeSource({
      search: {
        AllergyIntolerance: () => Promise.resolve(bundle(allergy('a1', 'Mine'), allergy('a2', 'Someone else', { patient: PATIENT_B_ID }))),
      },
    });
    renderCards(ds);
    expect(await within(card('Allergies')).findByText(/Mine/)).toBeInTheDocument();
    expect(screen.queryByText(/Someone else/)).toBeNull();
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('dropped 1 AllergyIntolerance'));
    expect(JSON.stringify(warn.mock.calls)).not.toContain('Someone else');
    warn.mockRestore();
  });

  it('asks only for this patient', async () => {
    const { ds, search } = fakeSource();
    renderCards(ds);
    await within(card('Allergies')).findByText('Nothing Recorded');
    expect(search).toHaveBeenCalledWith('AllergyIntolerance', { patient: PATIENT_A_ID }, expect.anything());
  });
});

describe('Medical Problems card', () => {
  it('fetches both categories and shows one row per problem, oldest onset first', async () => {
    const { ds, search } = fakeSource({
      search: {
        Condition: (p) =>
          Promise.resolve(
            p.category === 'problem-list-item'
              ? bundle(condition('p1', 'problem-list-item', { text: 'Asthma', onset: '2023-07-06T20:30:00+00:00' }))
              : bundle(
                  condition('e1', 'encounter-diagnosis', { text: 'Hypertension', onset: '2015-01-01T00:00:00+00:00' }),
                  condition('e2', 'encounter-diagnosis', { text: 'Hypertension', onset: '2015-01-01T00:00:00+00:00' }),
                  condition('e3', 'encounter-diagnosis', { text: 'Old sprain', status: 'inactive', onset: '2010-01-01T00:00:00+00:00' }),
                ),
          ),
      },
    });
    renderCards(ds);
    const list = await within(card('Medical Problems')).findByRole('list');
    expect(within(list).getAllByRole('listitem').map((li) => li.textContent)).toEqual(['Hypertension', 'Asthma']);
    expect(search).toHaveBeenCalledWith('Condition', { patient: PATIENT_A_ID, category: 'problem-list-item' }, expect.anything());
    expect(search).toHaveBeenCalledWith('Condition', { patient: PATIENT_A_ID, category: 'encounter-diagnosis' }, expect.anything());
  });

  it('empty and 403 states', async () => {
    renderCards(fakeSource().ds);
    expect(await within(card('Medical Problems')).findByText('Nothing Recorded')).toBeInTheDocument();
  });

  it('403 on either category: permission message', async () => {
    const { ds } = fakeSource({
      search: { Condition: (p) => (p.category === 'encounter-diagnosis' ? Promise.reject(forbidden()) : Promise.resolve(bundle())) },
    });
    renderCards(ds);
    expect(await within(card('Medical Problems')).findByText("You don't have permission to view medical problems.")).toBeInTheDocument();
  });
});

describe('Medications and Prescriptions cards (uuid split)', () => {
  const medReqs = () =>
    bundle(
      medReq('u-list', 'Lisinopril 20 mg', { intent: 'plan', sig: '1 tab daily' }),
      medReq('rx-1', 'Metformin HCl 500 mg', { sig: '1 tab BID', qty: 60, authoredOn: '2026-01-15T09:00:00+00:00' }),
      medReq('rx-2', 'Stopped Rx', { status: 'stopped' }),
    );

  it('list uuids go to Medications (name + dosage), the rest to Prescriptions (Drug / Details / Qty / Refills / Filled)', async () => {
    const { ds } = fakeSource({
      search: { MedicationRequest: () => Promise.resolve(medReqs()) },
      medList: (pid) => Promise.resolve(pid === PID_A ? [stdMed('u-list', 'Lisinopril 20 mg')] : []),
    });
    renderCards(ds);
    const meds = await within(card('Medications')).findByRole('list');
    expect(within(meds).getAllByRole('listitem').map((li) => li.textContent)).toEqual(['Lisinopril 20 mg 1 tab daily']);

    const table = await within(card('Prescriptions')).findByRole('table');
    expect(within(table).getAllByRole('columnheader').map((th) => th.textContent)).toEqual(['Drug', 'Details', 'Qty', 'Refills', 'Filled']);
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(1);
    const cells = within(rows[0] as HTMLElement).getAllByRole('cell');
    expect(cells.map((c) => c.textContent)).toEqual(['Metformin HCl 500 mg', '1 tab BID', '60', '—', '2026-01-15 09:00:00']);
    // Refills: OpenEMR's FHIR always says 0, so the number is not shown (gap G8).
    expect(cells[3]).toHaveAttribute('title', expect.stringContaining('not available'));
    expect(screen.queryByText('Stopped Rx')).toBeNull();
    expect(ds.patientMedicationList).toHaveBeenCalledWith(PID_A, expect.anything());
  });

  it('no list medications (the route answers 404, returned as []): everything is a prescription', async () => {
    const { ds } = fakeSource({ search: { MedicationRequest: () => Promise.resolve(medReqs()) }, medList: () => Promise.resolve([]) });
    renderCards(ds);
    expect(await within(card('Medications')).findByText('Nothing Recorded')).toBeInTheDocument();
    const table = await within(card('Prescriptions')).findByRole('table');
    expect(within(table).getByText('Lisinopril 20 mg')).toBeInTheDocument();
    expect(within(table).getByText('Metformin HCl 500 mg')).toBeInTheDocument();
  });

  it('no prescriptions at all: "None"', async () => {
    renderCards(fakeSource().ds);
    expect(await within(card('Prescriptions')).findByText('None')).toBeInTheDocument();
  });

  it('only inactive prescriptions: the table header with no rows (PHP parity)', async () => {
    const { ds } = fakeSource({ search: { MedicationRequest: () => Promise.resolve(bundle(medReq('rx', 'Stopped', { status: 'stopped' }))) } });
    renderCards(ds);
    const table = await within(card('Prescriptions')).findByRole('table');
    expect(within(table).getAllByRole('row')).toHaveLength(1);
  });

  it('standard API forbidden: one clearly labelled combined list, nothing guessed', async () => {
    const { ds } = fakeSource({
      search: { MedicationRequest: () => Promise.resolve(medReqs()) },
      medList: () => Promise.reject(forbidden()),
    });
    renderCards(ds);
    const combined = await screen.findByRole('region', { name: 'Medications and prescriptions (combined)' });
    expect(within(combined).getByText(/cannot be told apart/)).toBeInTheDocument();
    expect(within(combined).getAllByRole('listitem').map((li) => li.textContent)).toEqual(['Lisinopril 20 mg 1 tab daily', 'Metformin HCl 500 mg 1 tab BID']);
    expect(screen.queryByRole('region', { name: 'Medications' })).toBeNull();
    expect(screen.queryByRole('region', { name: 'Prescriptions' })).toBeNull();
  });

  it('pid lookup forbidden: the same combined fallback', async () => {
    const { ds } = fakeSource({ search: { MedicationRequest: () => Promise.resolve(medReqs()) }, pid: () => Promise.reject(forbidden()) });
    renderCards(ds);
    expect(await screen.findByRole('region', { name: 'Medications and prescriptions (combined)' })).toBeInTheDocument();
    expect(ds.patientMedicationList).not.toHaveBeenCalled();
  });

  it('MedicationRequest forbidden: permission message on both cards', async () => {
    const { ds } = fakeSource({ search: { MedicationRequest: () => Promise.reject(forbidden()) } });
    renderCards(ds);
    expect(await within(card('Medications')).findByText("You don't have permission to view medications.")).toBeInTheDocument();
    expect(within(card('Prescriptions')).getByText("You don't have permission to view prescriptions.")).toBeInTheDocument();
  });

  it('subject guard on both sources: other patients\' MedicationRequests and list rows are dropped', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const { ds } = fakeSource({
      search: { MedicationRequest: () => Promise.resolve(bundle(medReq('rx-mine', 'Mine Rx'), medReq('rx-other', 'Other Rx', { patient: PATIENT_B_ID }))) },
      medList: () => Promise.resolve([stdMed('u-mine', 'Mine list'), stdMed('u-other', 'Other list', { pid: '999' })]),
    });
    renderCards(ds);
    expect(await within(card('Prescriptions')).findByText('Mine Rx')).toBeInTheDocument();
    expect(within(card('Medications')).getByText('Mine list')).toBeInTheDocument();
    expect(screen.queryByText('Other Rx')).toBeNull();
    expect(screen.queryByText('Other list')).toBeNull();
    expect(warn).toHaveBeenCalledTimes(2);
    warn.mockRestore();
  });
});

describe('Care Team card', () => {
  const team = () =>
    careTeam('t1', {
      name: 'Primary team',
      participants: [
        practitionerParticipant(PRACT_1, { role: 'Family medicine', org: ORG_1, since: '2024-03-01' }),
        facilityParticipant(ORG_1),
        practitionerParticipant(PRACT_2, { role: 'Cardiology', org: ORG_1 }),
        { member: { reference: `RelatedPerson/${RELATED_1}` }, role: [{ text: 'Mother' }] },
      ],
    });

  async function openCareTeam() {
    const btn = within(card('Care Team')).getByRole('button', { name: 'Care Team' });
    await userEvent.click(btn);
    expect(btn).toHaveAttribute('aria-expanded', 'true');
  }

  it('team name + status badge; one row per person; Role / Facility / Since from FHIR; names 403 → "Name not available (permission)"', async () => {
    const { ds, read } = fakeSource({
      search: { CareTeam: () => Promise.resolve(bundle(team())) },
      read: (type, id) =>
        type === 'Organization' && id === ORG_1 ? Promise.resolve(organization(ORG_1, 'Synthetic Clinic')) : Promise.reject(forbidden()),
    });
    renderCards(ds);
    await openCareTeam();
    const c = card('Care Team');
    expect(await within(c).findByRole('heading', { name: /Primary team/ })).toHaveTextContent('Primary team Active');
    const table = within(c).getByRole('table');
    expect(within(table).getAllByRole('columnheader').map((th) => th.textContent)).toEqual(['Type', 'Member', 'Role', 'Facility', 'Since', 'Status', 'Note']);
    await waitFor(() => expect(within(table).getAllByText('Name not available (permission)')).toHaveLength(2));
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(3);
    const first = within(rows[0] as HTMLElement).getAllByRole('cell').map((td) => td.textContent);
    expect(first.slice(0, 5)).toEqual(['Provider', 'Name not available (permission)', 'Family medicine', 'Synthetic Clinic', '2024-03-01']);
    const related = within(rows[2] as HTMLElement).getAllByRole('cell').map((td) => td.textContent);
    expect(related.slice(0, 4)).toEqual(['Related Person', 'Name not available', 'Mother', '']);
    // The facility is read once for both members.
    expect(read.mock.calls.filter(([t]) => t === 'Organization')).toHaveLength(1);
  });

  it('resolves practitioner names when allowed', async () => {
    const { ds } = fakeSource({
      search: { CareTeam: () => Promise.resolve(bundle(team())) },
      read: (type, id) => {
        if (type === 'Practitioner' && id === PRACT_1) return Promise.resolve(practitioner(PRACT_1, 'Dana', 'Synthdoc'));
        if (type === 'Practitioner' && id === PRACT_2) return Promise.resolve(practitioner(PRACT_2, 'Lee', 'Heartwell'));
        return Promise.reject(forbidden());
      },
    });
    renderCards(ds);
    await openCareTeam();
    expect(await within(card('Care Team')).findByText('Synthdoc, Dana')).toBeInTheDocument();
    expect(within(card('Care Team')).getByText('Heartwell, Lee')).toBeInTheDocument();
    expect(within(card('Care Team')).getAllByText('Name not available (permission)')).toHaveLength(2); // the facility
  });

  it('member Status and Note are not in OpenEMR FHIR: shown as not available', async () => {
    const { ds } = fakeSource({ search: { CareTeam: () => Promise.resolve(bundle(team())) } });
    renderCards(ds);
    await openCareTeam();
    const rows = within(await within(card('Care Team')).findByRole('table')).getAllByRole('row').slice(1);
    const cells = within(rows[0] as HTMLElement).getAllByRole('cell');
    expect(cells[5]).toHaveTextContent('—');
    expect(cells[6]).toHaveTextContent('—');
  });

  it('empty: "Nothing Recorded"; 403: permission message', async () => {
    renderCards(fakeSource().ds);
    await openCareTeam();
    expect(await within(card('Care Team')).findByText('Nothing Recorded')).toBeInTheDocument();
  });

  it('403 on CareTeam', async () => {
    const { ds } = fakeSource({ search: { CareTeam: () => Promise.reject(forbidden()) } });
    renderCards(ds);
    await openCareTeam();
    expect(await within(card('Care Team')).findByText("You don't have permission to view the care team.")).toBeInTheDocument();
  });

  it('drops a care team that belongs to another patient', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const { ds } = fakeSource({ search: { CareTeam: () => Promise.resolve(bundle(careTeam('t9', { name: 'Not ours', patient: PATIENT_B_ID }))) } });
    renderCards(ds);
    await openCareTeam();
    expect(await within(card('Care Team')).findByText('Nothing Recorded')).toBeInTheDocument();
    expect(screen.queryByText('Not ours')).toBeNull();
    warn.mockRestore();
  });
});

describe('Labs card (PHP labdata_fragment.php parity)', () => {
  const a1c = () => labObs('o-a1c', { name: 'Hemoglobin A1c', effective: '2026-09-12T09:15:00+00:00', value: { value: 8.9, unit: '%' } });

  async function openLabs() {
    const btn = within(card('Labs')).getByRole('button', { name: 'Labs' });
    await userEvent.click(btn);
    expect(btn).toHaveAttribute('aria-expanded', 'true');
  }

  it("asks for this patient's laboratory Observations only", async () => {
    const { ds, search } = fakeSource();
    renderCards(ds);
    await openLabs();
    await within(card('Labs')).findByText('No lab data documented.');
    expect(search).toHaveBeenCalledWith('Observation', { patient: PATIENT_A_ID, category: 'laboratory' }, expect.anything());
  });

  it('loading, then "Most recent lab data:", the tests with the raw report date, and the encounter as not available', async () => {
    let resolve: (b: Bundle) => void = () => undefined;
    const { ds } = fakeSource({ search: { Observation: () => new Promise<Bundle>((r) => (resolve = r)) } });
    renderCards(ds);
    await openLabs();
    expect(within(card('Labs')).getByRole('status')).toHaveTextContent('Loading lab data');
    resolve(bundle(labObs('old', { name: 'Potassium', effective: '2026-06-25T09:10:00+00:00', encounter: ENC_2 }), a1c()));
    const c = card('Labs');
    expect(await within(c).findByText('Most recent lab data:')).toBeInTheDocument();
    expect(within(c).getByText('Tests: Hemoglobin A1c (2026-09-12 09:15:00)')).toBeInTheDocument();
    const enc = within(c).getByText('—');
    expect(enc.parentElement).toHaveTextContent('Encounter: —');
    expect(enc).toHaveAttribute('title', expect.stringContaining('not available'));
    // Parity card only: no values table.
    expect(within(c).queryByRole('table')).toBeNull();
    expect(within(c).queryByText(/8\.9/)).toBeNull();
    expect(within(c).queryByText(/Potassium/)).toBeNull();
  });

  it('empty: "No lab data documented." and no link', async () => {
    renderCards(fakeSource().ds, { openemrWebUrl: 'https://emr.invalid' });
    await openLabs();
    expect(await within(card('Labs')).findByText('No lab data documented.')).toBeInTheDocument();
    expect(within(card('Labs')).queryByRole('link')).toBeNull();
  });

  it('only entered-in-error results: "No lab data documented."', async () => {
    const { ds } = fakeSource({ search: { Observation: () => Promise.resolve(bundle(labObs('x', { status: 'entered-in-error', name: 'Glucose' }))) } });
    renderCards(ds);
    await openLabs();
    expect(await within(card('Labs')).findByText('No lab data documented.')).toBeInTheDocument();
    expect(screen.queryByText(/Glucose/)).toBeNull();
  });

  it('403: permission message, no retry', async () => {
    const { ds, search } = fakeSource({ search: { Observation: () => Promise.reject(forbidden()) } });
    renderCards(ds);
    await openLabs();
    expect(await within(card('Labs')).findByText("You don't have permission to view lab data.")).toBeInTheDocument();
    expect(within(card('Labs')).queryByRole('button', { name: 'Try again' })).toBeNull();
    expect(search.mock.calls.filter(([t]) => t === 'Observation')).toHaveLength(1);
  });

  it('upstream error: message and a working "Try again"', async () => {
    let fail = true;
    const { ds } = fakeSource({ search: { Observation: () => (fail ? Promise.reject(upstream()) : Promise.resolve(bundle(a1c()))) } });
    renderCards(ds);
    await openLabs();
    const retry = await within(card('Labs')).findByRole('button', { name: 'Try again' });
    expect(within(card('Labs')).getByText('Lab data could not be loaded.')).toBeInTheDocument();
    fail = false;
    await userEvent.click(retry);
    expect(await within(card('Labs')).findByText(/Hemoglobin A1c/)).toBeInTheDocument();
  });

  it("subject guard: another patient's newer result is dropped, only a count is logged", async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const { ds } = fakeSource({
      search: {
        Observation: () => Promise.resolve(bundle(a1c(), labObs('theirs', { name: 'Not ours', effective: '2026-09-25T08:00:00+00:00', patient: PATIENT_B_ID }))),
      },
    });
    renderCards(ds);
    await openLabs();
    expect(await within(card('Labs')).findByText(/Hemoglobin A1c/)).toBeInTheDocument();
    expect(screen.queryByText(/Not ours/)).toBeNull();
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('dropped 1 Observation'));
    expect(JSON.stringify(warn.mock.calls)).not.toContain('Not ours');
    warn.mockRestore();
  });

  it('no pencil (PHP renders none for its "Trend" button); the view-all link goes to the PHP dashboard for this patient', async () => {
    const { ds } = fakeSource({ search: { Observation: () => Promise.resolve(bundle(a1c())) } });
    renderCards(ds, { openemrWebUrl: 'https://emr.invalid' });
    await openLabs();
    const link = await within(card('Labs')).findByRole('link', { name: 'View and graph all lab data in OpenEMR' });
    expect(link).toHaveAttribute('href', `https://emr.invalid/interface/patient_file/summary/demographics.php?set_pid=${PID_A}`);
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(within(card('Labs')).getAllByRole('link')).toHaveLength(1);
  });

  it('no link without a configured OpenEMR URL', async () => {
    const { ds } = fakeSource({ search: { Observation: () => Promise.resolve(bundle(a1c())) } });
    renderCards(ds);
    await openLabs();
    await within(card('Labs')).findByText(/Hemoglobin A1c/);
    expect(within(card('Labs')).queryByRole('link')).toBeNull();
  });
});
