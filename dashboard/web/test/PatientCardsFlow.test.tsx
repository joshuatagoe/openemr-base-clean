import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../src/App';
import { createQueryClient } from '../src/data/queryClient';
import { bundle, careTeam, labObs, medReq, ORG_1, PRACT_1, practitionerParticipant, stdMed } from './fixtures/clinical';
import { PATIENT_A_ID, patientA } from './fixtures/patients';
import { server, signedIn } from './msw/server';

beforeEach(() => window.localStorage.clear());

describe('patient page with the clinical cards (mode A, through the BFF routes)', () => {
  it('renders every card from the BFF: 403 message, std-API 404 = no list meds, Rx table, care-team names forbidden', async () => {
    const seen: string[] = [];
    server.use(
      signedIn,
      http.get('*/api/fhir/Patient/:id', () => HttpResponse.json(patientA)),
      http.get('*/api/fhir/AllergyIntolerance', () => HttpResponse.json({ error: 'forbidden' }, { status: 403 })),
      http.get('*/api/fhir/MedicationRequest', ({ request }) => {
        seen.push(new URL(request.url).search);
        return HttpResponse.json(bundle(medReq('rx-1', 'Metformin HCl 500 mg', { sig: '1 tab BID', qty: 60, authoredOn: '2026-01-15T09:00:00+00:00' })));
      }),
      http.get('*/api/patient/:pid/medication', ({ params }) => {
        seen.push(`medication:${String(params.pid)}`);
        return new HttpResponse(null, { status: 404 });
      }),
      http.get('*/api/fhir/CareTeam', () =>
        HttpResponse.json(bundle(careTeam('t1', { name: 'Synthetic team', participants: [practitionerParticipant(PRACT_1, { role: 'Physician', org: ORG_1 })] }))),
      ),
      http.get('*/api/fhir/Practitioner/:id', () => HttpResponse.json({ error: 'forbidden' }, { status: 403 })),
      http.get('*/api/fhir/Organization/:id', () => HttpResponse.json({ error: 'forbidden' }, { status: 403 })),
    );
    window.history.replaceState(null, '', `/patient/${PATIENT_A_ID}`);
    render(<App queryClient={createQueryClient({ retryDelay: 0 })} />);

    const region = (name: string) => screen.findByRole('region', { name });
    expect(await within(await region('Allergies')).findByText(/^Your OpenEMR role can't view allergies\./)).toBeInTheDocument();
    expect(await within(await region('Medical Problems')).findByText('Nothing Recorded')).toBeInTheDocument();
    expect(await within(await region('Medications')).findByText('Nothing Recorded')).toBeInTheDocument();
    const rx = await within(await region('Prescriptions')).findByRole('table');
    expect(within(rx).getByText('Metformin HCl 500 mg')).toBeInTheDocument();
    expect(seen).toContain(`?patient=${PATIENT_A_ID}`);
    expect(seen).toContain('medication:42');

    const careTeamCard = await region('Care Team');
    await userEvent.click(within(careTeamCard).getByRole('button', { name: 'Care Team' }));
    expect(await within(careTeamCard).findByText("Member and facility names are hidden: your OpenEMR role can't read provider or facility records.")).toBeInTheDocument();
    expect(within(careTeamCard).queryByText(/Name not available/)).toBeNull();
  });

  it('Labs: calls the allow-listed Observation route and shows the latest report, entered-in-error dropped', async () => {
    const seen: string[] = [];
    server.use(
      signedIn,
      http.get('*/api/fhir/Patient/:id', () => HttpResponse.json(patientA)),
      http.get('*/api/fhir/Observation', ({ request }) => {
        seen.push(new URL(request.url).search);
        return HttpResponse.json(
          bundle(
            labObs('eie', { name: 'Glucose', status: 'entered-in-error', effective: '2026-09-20T08:00:00+00:00' }),
            labObs('a1c', { name: 'Hemoglobin A1c', effective: '2026-09-12T09:15:00+00:00' }),
          ),
        );
      }),
    );
    window.history.replaceState(null, '', `/patient/${PATIENT_A_ID}`);
    render(<App queryClient={createQueryClient({ retryDelay: 0 })} />);
    const labs = await screen.findByRole('region', { name: 'Labs' });
    await userEvent.click(within(labs).getByRole('button', { name: 'Labs' }));
    expect(await within(labs).findByText('Tests: Hemoglobin A1c (2026-09-12 09:15:00)')).toBeInTheDocument();
    expect(within(labs).queryByText(/Glucose/)).toBeNull();
    expect(seen).toEqual([`?patient=${PATIENT_A_ID}&category=laboratory`]);
  });

  it('list medications from the standard API go to Medications, not Prescriptions', async () => {
    server.use(
      signedIn,
      http.get('*/api/fhir/Patient/:id', () => HttpResponse.json(patientA)),
      http.get('*/api/fhir/MedicationRequest', () => HttpResponse.json(bundle(medReq('u-list', 'Lisinopril 20 mg', { intent: 'plan' })))),
      http.get('*/api/patient/:pid/medication', () => HttpResponse.json([stdMed('u-list', 'Lisinopril 20 mg')])),
    );
    window.history.replaceState(null, '', `/patient/${PATIENT_A_ID}`);
    render(<App queryClient={createQueryClient({ retryDelay: 0 })} />);
    expect(await within(await screen.findByRole('region', { name: 'Medications' })).findByText('Lisinopril 20 mg')).toBeInTheDocument();
    expect(await within(await screen.findByRole('region', { name: 'Prescriptions' })).findByText('None')).toBeInTheDocument();
  });
});
