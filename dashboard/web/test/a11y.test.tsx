// Accessibility: axe-core over the rendered app (mode A routes, MSW data).
// jsdom has no layout, so axe's colour-contrast rule cannot run here; it is
// checked in a real browser (see docs/dashboard-parity/PARITY.md, a11y section).
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../src/App';
import { createQueryClient } from '../src/data/queryClient';
import { recentStorageKey } from '../src/recent/recentPatients';
import {
  allergy,
  bundle,
  careTeam,
  condition,
  medReq,
  ORG_1,
  organization,
  PRACT_1,
  practitioner,
  practitionerParticipant,
  stdMed,
} from './fixtures/clinical';
import { PATIENT_A_ID, PATIENT_B_ID, patientA, patientB, searchset } from './fixtures/patients';
import { server, signedIn, signedOut } from './msw/server';

// The context is the whole document, so page-level rules (html-has-lang,
// document-title, landmark-one-main, …) run too; on document.body they are
// skipped because they select <html>. page-has-heading-one is always
// "incomplete" in jsdom (it cannot tell whether the h1 is visible), so each
// test also asserts exactly one level-1 heading; the real-browser run checks
// the rule itself (docs/dashboard-parity/PARITY.md).
async function violations(): Promise<string[]> {
  const result = await axe.run(document, {
    rules: { 'color-contrast': { enabled: false } },
    resultTypes: ['violations'],
  });
  expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
  return result.violations.map((v) => `${v.impact ?? '?'} ${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`);
}

beforeEach(() => {
  window.localStorage.clear();
  // What index.html sets; jsdom starts with a bare <html>.
  document.documentElement.lang = 'en';
  document.title = 'Patient Dashboard';
});

describe('axe-core: no violations on the rendered pages', () => {
  it('signed-out home page', async () => {
    server.use(signedOut);
    render(<App queryClient={createQueryClient({ retryDelay: 0 })} />);
    await screen.findByRole('link', { name: 'Sign in' });
    expect(await violations()).toEqual([]);
  });

  it('patient search with results', async () => {
    server.use(
      signedIn,
      http.get('*/api/fhir/Patient', () => HttpResponse.json(searchset(patientA, patientB))),
    );
    window.history.replaceState(null, '', '/dashboard?name=Sample');
    render(<App queryClient={createQueryClient({ retryDelay: 0 })} />);
    await screen.findByRole('table');
    expect(await violations()).toEqual([]);
  });

  it('Patient Finder: list with filters, sort buttons and pager; then the Recent Patients tab', async () => {
    const fhirUser = 'https://openemr.invalid/apis/default/fhir/Practitioner/9f000000-0000-4000-8000-0000000000aa';
    const key = await recentStorageKey(fhirUser);
    window.localStorage.setItem(key ?? '', JSON.stringify([PATIENT_B_ID, PATIENT_A_ID]));
    const many = Array.from({ length: 11 }, (_, i) => ({ ...patientA, id: `9e000000-0000-4000-8000-${String(i).padStart(12, '0')}` }));
    server.use(
      http.get('*/auth/me', () =>
        HttpResponse.json({ authenticated: true, user: { displayName: 'Dana Testdoctor', fhirUser }, expiresAt: '2030-01-01T00:00:00.000Z' }),
      ),
      http.get('*/api/fhir/Patient', () => HttpResponse.json(searchset(...many))),
      http.get('*/api/fhir/Patient/:id', ({ params }) => HttpResponse.json(params.id === PATIENT_B_ID ? patientB : patientA)),
    );
    window.history.replaceState(null, '', '/dashboard?page=2&sort=-dob');
    render(<App queryClient={createQueryClient({ retryDelay: 0 })} />);
    await screen.findByRole('table', { name: 'Patient List' });
    await screen.findByRole('navigation', { name: 'Patient list pages' });
    expect(await violations()).toEqual([]);

    // With an input problem shown.
    await userEvent.type(screen.getByLabelText('Search by SSN'), 'a b{Enter}');
    await screen.findByRole('alert');
    expect(await violations()).toEqual([]);

    await userEvent.click(screen.getByRole('tab', { name: 'Recent Patients' }));
    const recent = await screen.findByRole('tabpanel', { name: 'Recent Patients' });
    await within(recent).findByText('Otherfamily');
    expect(await violations()).toEqual([]);
  });

  it('patient page with every card expanded and filled, plus a 403 card', async () => {
    server.use(
      signedIn,
      http.get('*/api/fhir/Patient/:id', () => HttpResponse.json(patientA)),
      http.get('*/api/fhir/AllergyIntolerance', () =>
        HttpResponse.json(bundle(allergy('a1', 'Penicillin', { criticality: 'high', reaction: 'Hives' }), allergy('a2', 'Latex', { criticality: 'low' }))),
      ),
      http.get('*/api/fhir/Condition', ({ request }) =>
        new URL(request.url).searchParams.get('category') === 'problem-list-item'
          ? HttpResponse.json(bundle(condition('c1', 'problem-list-item', { text: 'Type 2 diabetes', onset: '2020-01-01' })))
          : HttpResponse.json(bundle()),
      ),
      http.get('*/api/fhir/MedicationRequest', () =>
        HttpResponse.json(
          bundle(
            medReq('rx-1', 'Metformin HCl 500 mg', { sig: '1 tab BID', qty: 60, authoredOn: '2026-01-15T09:00:00+00:00' }),
            medReq('u-list', 'Lisinopril 20 mg', { intent: 'plan' }),
          ),
        ),
      ),
      http.get('*/api/patient/:pid/medication', () => HttpResponse.json([stdMed('u-list', 'Lisinopril 20 mg')])),
      http.get('*/api/fhir/CareTeam', () =>
        HttpResponse.json(bundle(careTeam('t1', { name: 'Synthetic team', participants: [practitionerParticipant(PRACT_1, { role: 'Physician', org: ORG_1 })] }))),
      ),
      http.get('*/api/fhir/Practitioner/:id', () => HttpResponse.json(practitioner(PRACT_1, 'Dana', 'Synthetic'))),
      http.get('*/api/fhir/Organization/:id', () => HttpResponse.json(organization(ORG_1, 'Synthetic Clinic'))),
      http.get('*/api/fhir/Observation', () => HttpResponse.json({ error: 'forbidden' }, { status: 403 })),
    );
    window.history.replaceState(null, '', `/patient/${PATIENT_A_ID}`);
    render(<App queryClient={createQueryClient({ retryDelay: 0 })} />);

    for (const name of ['Care Team', 'Labs']) {
      const card = await screen.findByRole('region', { name });
      await userEvent.click(within(card).getByRole('button', { name }));
    }
    await within(await screen.findByRole('region', { name: 'Prescriptions' })).findByRole('table');
    await within(await screen.findByRole('region', { name: 'Care Team' })).findByText('Synthetic, Dana');
    await within(await screen.findByRole('region', { name: 'Labs' })).findByText(/can't view lab data/);
    expect(await violations()).toEqual([]);
    // Wide tables scroll sideways on a phone; the scroll box must take keyboard focus
    // (axe scrollable-region-focusable, which needs layout and so only fires in a browser).
    for (const name of ['Prescriptions table', 'Care team members']) {
      expect(screen.getByRole('region', { name })).toHaveAttribute('tabindex', '0');
    }
  });
});
