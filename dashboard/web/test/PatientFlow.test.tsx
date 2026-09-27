import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { App } from '../src/App';
import { createQueryClient } from '../src/data/queryClient';
import { patientAgeDisplay } from '../src/format/age';
import { localToday } from '../src/format/date';
import { PATIENT_A_ID, PATIENT_B_ID, PATIENT_DECEASED_ID, patientA, patientB, patientDeceased, searchset } from './fixtures/patients';
import { server, signedIn } from './msw/server';

const byId = { [PATIENT_A_ID]: patientA, [PATIENT_B_ID]: patientB, [PATIENT_DECEASED_ID]: patientDeceased };

function patientReads() {
  const counts: Record<string, number> = {};
  const handler = http.get('*/api/fhir/Patient/:id', ({ params }) => {
    const id = String(params.id);
    counts[id] = (counts[id] ?? 0) + 1;
    const p = (byId as Record<string, unknown>)[id];
    return p ? HttpResponse.json(p) : HttpResponse.json({ error: 'not_found' }, { status: 404 });
  });
  return { handler, counts };
}

function patientSearch(result = searchset(patientA, patientB)) {
  const seen: URLSearchParams[] = [];
  const handler = http.get('*/api/fhir/Patient', ({ request }) => {
    seen.push(new URL(request.url).searchParams);
    return HttpResponse.json(result);
  });
  return { handler, seen };
}

function renderAt(path: string, queryClient = createQueryClient({ retryDelay: 0 })) {
  window.history.replaceState(null, '', path);
  const utils = render(<App queryClient={queryClient} />);
  return { ...utils, queryClient };
}

const expectedAge = (dob: string) => patientAgeDisplay(dob, localToday());

describe('patient search (mode A)', () => {
  it('searches by name, lists name / DOB / sex / MRN (never the SSN), and opens the chosen patient', async () => {
    const search = patientSearch();
    const reads = patientReads();
    server.use(signedIn, search.handler, reads.handler);
    renderAt('/dashboard');

    const nameBox = await screen.findByLabelText('Name');
    await userEvent.type(nameBox, 'Sample');
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));

    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('2 patients found'));
    // The landing list, then the search (same paging and sort).
    expect(search.seen).toHaveLength(2);
    expect([...(search.seen[1]?.entries() ?? [])]).toEqual([
      ['name', 'Sample'],
      ['_count', '21'],
      ['_offset', '0'],
      ['_sort', 'family,given'],
    ]);

    const results = screen.getByRole('table', { name: 'Search results' });
    const rowA = within(results).getByRole('row', { name: /Samplefamily, Ada Quinn/ });
    expect(rowA).toHaveTextContent('1980-06-15');
    expect(rowA).toHaveTextContent('Female');
    expect(rowA).toHaveTextContent('SYN-1001');
    expect(results).not.toHaveTextContent('900-11-2222');
    expect(results).not.toHaveTextContent('900-33-4444');
    expect(window.location.search).toBe('?name=Sample');

    await userEvent.click(within(rowA).getByRole('link', { name: 'Samplefamily, Ada Quinn' }));
    expect(window.location.pathname).toBe(`/patient/${PATIENT_A_ID}`);
    const heading = await screen.findByRole('heading', { level: 1, name: 'Ada Samplefamily' });
    await waitFor(() => expect(heading).toHaveFocus());
  });

  it('sends DOB and MRN as birthdate / identifier', async () => {
    const search = patientSearch(searchset(patientA));
    server.use(signedIn, search.handler);
    renderAt('/dashboard');
    await userEvent.type(await screen.findByLabelText('Date of birth'), '1980-06-15');
    await userEvent.type(screen.getByLabelText('MRN'), 'SYN-1001');
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('1 patient found'));
    expect(Object.fromEntries(search.seen.at(-1) ?? [])).toEqual({
      birthdate: '1980-06-15',
      identifier: 'SYN-1001',
      _count: '21',
      _offset: '0',
      _sort: 'family,given',
    });
  });

  it('an empty search just shows the list; characters the server would refuse are rejected without calling it', async () => {
    const search = patientSearch();
    server.use(signedIn, search.handler);
    renderAt('/dashboard');
    await screen.findByRole('table', { name: 'All patients' });
    await userEvent.click(await screen.findByRole('button', { name: 'Search' }));
    expect(screen.queryByRole('alert')).toBeNull();
    expect(window.location.search).toBe('');
    await userEvent.type(screen.getByLabelText('Name'), '<b>x');
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Use only letters, spaces, apostrophes, hyphens and dots in the name.');
    expect(screen.getByLabelText('Name')).toHaveAttribute('aria-invalid', 'true');
    // Only the landing list was requested.
    expect(search.seen).toHaveLength(1);
    expect(Object.fromEntries(search.seen[0] ?? [])).toEqual({ _count: '21', _offset: '0', _sort: 'family,given' });
  });

  it('says so when nothing matches', async () => {
    const search = patientSearch(searchset());
    server.use(signedIn, search.handler);
    renderAt('/dashboard?name=Nobody');
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('No patients match.'));
    expect(screen.getByLabelText('Name')).toHaveValue('Nobody');
  });

  it('shows a readable error with a retry when the server fails (after retrying)', async () => {
    let calls = 0;
    server.use(
      signedIn,
      http.get('*/api/fhir/Patient', () => {
        calls += 1;
        return HttpResponse.json({ error: 'upstream_error' }, { status: 502 });
      }),
    );
    renderAt('/dashboard?name=Sample');
    expect(await screen.findByRole('alert')).toHaveTextContent('OpenEMR returned an error.');
    expect(calls).toBe(3);
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
  });
});

describe('patient header (PHP patient-bar parity)', () => {
  it('shows first + last name, (MRN from the PT identifier), "DOB: … Age: …" and the birth sex; no active status, no SSN', async () => {
    const reads = patientReads();
    server.use(signedIn, reads.handler);
    renderAt(`/patient/${PATIENT_A_ID}`);
    const bar = await screen.findByRole('region', { name: 'Patient' });
    await within(bar).findByRole('heading', { level: 1, name: 'Ada Samplefamily' });
    expect(within(bar).getByText('(SYN-1001)')).toBeInTheDocument();
    // Same text as the PHP bar: "First Last (pubpid)" with a space before the MRN.
    expect(bar).toHaveTextContent(/^Ada Samplefamily \(SYN-1001\)\s*DOB:/);
    expect(bar).toHaveTextContent(`DOB: 1980-06-15 Age: ${expectedAge('1980-06-15')}`);
    // Sex from FHIR Patient.gender, labelled as OpenEMR's Demographics card labels patient_data.sex.
    expect(within(bar).getByText('Birth Sex:')).toBeInTheDocument();
    expect(bar).toHaveTextContent('Birth Sex: Female');
    expect(bar).not.toHaveTextContent(/\bmale\b/i);
    // Active status is intentionally not shown: OpenEMR's FHIR hard-codes active = true.
    expect(bar).not.toHaveTextContent(/active/i);
    expect(bar).not.toHaveTextContent('900-11-2222');
    expect(bar).not.toHaveTextContent('Quinn');
    expect(bar.className).toContain('patient-bar');
  });

  it('shows "Age at death" for a deceased patient', async () => {
    const reads = patientReads();
    server.use(signedIn, reads.handler);
    renderAt(`/patient/${PATIENT_DECEASED_ID}`);
    const bar = await screen.findByRole('region', { name: 'Patient' });
    await within(bar).findByRole('heading', { name: 'Cleo Pastfamily' });
    expect(bar).toHaveTextContent('(SYN-3003)');
    expect(bar).toHaveTextContent('DOB: 1930-03-20 Age at death: 70');
    expect(bar).toHaveTextContent('Birth Sex: Female');
    expect(bar).not.toHaveTextContent(/Age: /);
  });

  it('shows "Birth Sex: Male" for a male patient and leaves the item out when gender is absent', async () => {
    server.use(
      signedIn,
      http.get('*/api/fhir/Patient/:id', ({ params }) =>
        HttpResponse.json(String(params.id) === PATIENT_B_ID ? patientB : { ...patientA, gender: undefined }),
      ),
    );
    const first = renderAt(`/patient/${PATIENT_B_ID}`);
    const bar = await screen.findByRole('region', { name: 'Patient' });
    await within(bar).findByRole('heading', { name: 'Bram Otherfamily' });
    expect(bar).toHaveTextContent('Birth Sex: Male');
    first.unmount();

    renderAt(`/patient/${PATIENT_A_ID}`);
    const barA = await screen.findByRole('region', { name: 'Patient' });
    await within(barA).findByRole('heading', { name: 'Ada Samplefamily' });
    expect(barA).not.toHaveTextContent('Birth Sex');
  });

  it.each([
    [403, 'not_accessible', 'You do not have access to this patient.'],
    [403, 'forbidden', 'You do not have access to this patient.'],
    [404, 'not_found', 'Patient not found.'],
  ])('%i %s: shows "%s" and does not retry', async (status, error, text) => {
    let calls = 0;
    server.use(
      signedIn,
      http.get('*/api/fhir/Patient/:id', () => {
        calls += 1;
        return HttpResponse.json({ error }, { status });
      }),
    );
    renderAt(`/patient/${PATIENT_A_ID}`);
    expect(await screen.findByRole('alert')).toHaveTextContent(text);
    expect(calls).toBe(1);
    expect(screen.getByRole('link', { name: 'Find another patient' })).toHaveAttribute('href', '/dashboard');
  });

  it('401: ends the session and asks the user to sign in again (no retry)', async () => {
    let calls = 0;
    server.use(
      signedIn,
      http.get('*/api/fhir/Patient/:id', () => {
        calls += 1;
        return HttpResponse.json({ error: 'session_expired' }, { status: 401 });
      }),
    );
    renderAt(`/patient/${PATIENT_A_ID}`);
    expect(await screen.findByRole('alert')).toHaveTextContent('Your session has expired. Please sign in again.');
    expect(screen.getByRole('link', { name: 'Sign in' })).toBeInTheDocument();
    expect(calls).toBe(1);
    await waitFor(() => expect(window.location.pathname).toBe('/'));
  });

  it('treats a malformed patient id in the URL as not found without calling the server', async () => {
    server.use(signedIn);
    renderAt('/patient/not%20an%20id');
    expect(await screen.findByRole('alert')).toHaveTextContent('Patient not found.');
  });
});

describe('switching patients', () => {
  it('opens a second patient without a new sign-in, and clears patient data on sign-out', async () => {
    let meCalls = 0;
    const search = patientSearch();
    const reads = patientReads();
    server.use(
      http.get('*/auth/me', () => {
        meCalls += 1;
        return HttpResponse.json({ authenticated: true, user: { displayName: 'Dana Testdoctor', fhirUser: null }, expiresAt: '2030-01-01T00:00:00.000Z' });
      }),
      http.post('*/auth/logout', () => new HttpResponse(null, { status: 204 })),
      search.handler,
      reads.handler,
    );
    const { queryClient } = renderAt(`/patient/${PATIENT_A_ID}`);
    await screen.findByRole('heading', { level: 1, name: 'Ada Samplefamily' });

    await userEvent.click(screen.getByRole('link', { name: 'Find another patient' }));
    const searchHeading = await screen.findByRole('heading', { level: 1, name: 'Find a patient' });
    await waitFor(() => expect(searchHeading).toHaveFocus());
    await userEvent.type(screen.getByLabelText('Name'), 'Other');
    await userEvent.keyboard('{Enter}');
    const rowB = await screen.findByRole('row', { name: /Otherfamily, Bram/ });
    await userEvent.click(within(rowB).getByRole('link'));

    const bar = await screen.findByRole('region', { name: 'Patient' });
    await within(bar).findByRole('heading', { level: 1, name: 'Bram Otherfamily' });
    expect(bar).toHaveTextContent('(SYN-2002)');
    expect(bar).not.toHaveTextContent('Samplefamily');
    expect(screen.getByText('Signed in as Dana Testdoctor')).toBeInTheDocument();
    expect(meCalls).toBe(1);
    expect(reads.counts[PATIENT_B_ID]).toBe(1);

    expect(queryClient.getQueryCache().getAll().length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    await screen.findByText('You have signed out.');
    await waitFor(() => expect(queryClient.getQueryCache().getAll()).toHaveLength(0));
  });
});
