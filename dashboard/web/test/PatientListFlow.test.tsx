// Mode A landing page: the patient list (like OpenEMR's Patient Finder) with
// paging, the search box that narrows it, and the recent-patients list.
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Patient } from 'fhir/r4';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../src/App';
import { createQueryClient } from '../src/data/queryClient';
import { recentStorageKey } from '../src/recent/recentPatients';
import { mrn, PATIENT_A_ID, PATIENT_B_ID, PATIENT_DECEASED_ID, patientA, patientB, patientDeceased, searchset, ssn } from './fixtures/patients';
import { server } from './msw/server';

const USER = 'https://openemr.invalid/apis/default/fhir/Practitioner/9f000000-0000-4000-8000-0000000000aa';
const signedInAs = (fhirUser: string | null) =>
  http.get('*/auth/me', () =>
    HttpResponse.json({ authenticated: true, user: { displayName: 'Dana Testdoctor', fhirUser }, expiresAt: '2030-01-01T00:00:00.000Z' }),
  );

/** Synthetic patient n (family names sort in n order). */
function listPatient(n: number): Patient {
  const id = `9e000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
  return {
    resourceType: 'Patient',
    id,
    identifier: [ssn(`900-00-${String(n).padStart(4, '0')}`), mrn(`LST-${n}`)],
    name: [{ use: 'official', family: `Family${String(n).padStart(3, '0')}`, given: ['Given'] }],
    gender: 'unknown',
    birthDate: '1970-01-01',
    telecom: [{ system: 'phone', value: '555-0100' }],
  };
}
const ALL = Array.from({ length: 45 }, (_, i) => listPatient(i + 1));

/** Mock OpenEMR's Patient search: applies _offset / _count to `pool`, like OpenEMR does (no next link, total = entries). */
function patientList(pool: Patient[] = ALL) {
  const seen: Record<string, string>[] = [];
  const handler = http.get('*/api/fhir/Patient', ({ request }) => {
    const q = new URL(request.url).searchParams;
    seen.push(Object.fromEntries(q));
    const name = q.get('name');
    const matches = name ? pool.filter((p) => JSON.stringify(p.name).toLowerCase().includes(name.toLowerCase())) : pool;
    const offset = Number(q.get('_offset') ?? '0');
    const count = Number(q.get('_count') ?? String(matches.length));
    return HttpResponse.json(searchset(...matches.slice(offset, offset + count)));
  });
  return { handler, seen };
}

const byId: Record<string, Patient> = { [PATIENT_A_ID]: patientA, [PATIENT_B_ID]: patientB, [PATIENT_DECEASED_ID]: patientDeceased };
function patientReads(overrides: Record<string, { status: number; error: string }> = {}) {
  const counts: Record<string, number> = {};
  const handler = http.get('*/api/fhir/Patient/:id', ({ params }) => {
    const id = String(params.id);
    counts[id] = (counts[id] ?? 0) + 1;
    const o = overrides[id];
    if (o) return HttpResponse.json({ error: o.error }, { status: o.status });
    const p = byId[id];
    return p ? HttpResponse.json(p) : HttpResponse.json({ error: 'not_found' }, { status: 404 });
  });
  return { handler, counts };
}

function renderAt(path: string) {
  window.history.replaceState(null, '', path);
  return render(<App queryClient={createQueryClient({ retryDelay: 0 })} />);
}

const listTable = () => screen.getByRole('table', { name: 'Patients' });
const rowsOf = (table: HTMLElement) => within(table).getAllByRole('row').slice(1);

beforeEach(() => window.localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe('patient list (landing page, mode A)', () => {
  it('lists the first 20 patients with no search term, sorted by last name, with name / DOB / MRN (never SSN or phone)', async () => {
    const list = patientList();
    server.use(signedInAs(USER), list.handler);
    renderAt('/dashboard');

    const table = await screen.findByRole('table', { name: 'Patients' });
    expect(list.seen[0]).toEqual({ _count: '21', _offset: '0', _sort: 'family,given' });
    const rows = rowsOf(table);
    expect(rows).toHaveLength(20);
    expect(rows[0]).toHaveTextContent('Family001, Given');
    expect(rows[0]).toHaveTextContent('1970-01-01');
    expect(rows[0]).toHaveTextContent('LST-1');
    expect(rows[19]).toHaveTextContent('Family020, Given');
    expect(table).not.toHaveTextContent('900-00-');
    expect(table).not.toHaveTextContent('555-0100');
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['Name', 'DOB', 'Sex', 'MRN']);
    expect(within(rows[0] as HTMLElement).getByRole('link', { name: 'Family001, Given' })).toHaveAttribute(
      'href',
      `/patient/${ALL[0]?.id ?? ''}`,
    );

    const pager = screen.getByRole('navigation', { name: 'Patient list pages' });
    expect(within(pager).getByText('Page 1')).toBeInTheDocument();
    expect(within(pager).getByRole('button', { name: 'Previous page' })).toBeDisabled();
    expect(within(pager).getByRole('button', { name: 'Next page' })).toBeEnabled();
    expect(screen.getByRole('status')).toHaveTextContent('Showing patients 1–20');
  });

  it('pages with Next / Previous (keyboard), keeps the page in the URL, and moves focus to the list', async () => {
    const list = patientList();
    server.use(signedInAs(USER), list.handler);
    renderAt('/dashboard');
    await screen.findByRole('table', { name: 'Patients' });

    screen.getByRole('button', { name: 'Next page' }).focus();
    await userEvent.keyboard('{Enter}');
    await waitFor(() => expect(rowsOf(listTable())[0]).toHaveTextContent('Family021, Given'));
    expect(window.location.search).toBe('?page=2');
    expect(list.seen.at(-1)).toEqual({ _count: '21', _offset: '20', _sort: 'family,given' });
    expect(screen.getByText('Page 2')).toBeInTheDocument();
    await waitFor(() => expect(listTable()).toHaveFocus());

    await userEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(rowsOf(listTable())[0]).toHaveTextContent('Family041, Given'));
    expect(rowsOf(listTable())).toHaveLength(5);
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled();
    expect(screen.getByRole('status')).toHaveTextContent('Showing patients 41–45');

    screen.getByRole('button', { name: 'Previous page' }).focus();
    await userEvent.keyboard(' ');
    await waitFor(() => expect(rowsOf(listTable())[0]).toHaveTextContent('Family021, Given'));
    expect(window.location.search).toBe('?page=2');
  });

  it('opens a page straight from the URL, and treats a bad page number as page 1', async () => {
    const list = patientList();
    server.use(signedInAs(USER), list.handler);
    const first = renderAt('/dashboard?page=3');
    await waitFor(() => expect(rowsOf(listTable())[0]).toHaveTextContent('Family041, Given'));
    expect(list.seen.at(-1)).toMatchObject({ _offset: '40' });
    first.unmount();

    for (const bad of ['0', '-1', 'abc', '1.5', '99999999']) {
      renderAt(`/dashboard?page=${bad}`).unmount();
    }
    await waitFor(() => expect(list.seen.slice(1).every((q) => q._offset === '0')).toBe(true));
  });

  it('hides the pager when everything fits on one page', async () => {
    server.use(signedInAs(USER), patientList(ALL.slice(0, 14)).handler);
    renderAt('/dashboard');
    await screen.findByRole('table', { name: 'Patients' });
    expect(rowsOf(listTable())).toHaveLength(14);
    expect(screen.queryByRole('navigation', { name: 'Patient list pages' })).toBeNull();
    expect(screen.getByRole('status')).toHaveTextContent('14 patients');
  });

  it('says so on a page past the end, with Previous still available', async () => {
    server.use(signedInAs(USER), patientList(ALL.slice(0, 5)).handler);
    renderAt('/dashboard?page=4');
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('No patients on this page.'));
    expect(screen.getByRole('button', { name: 'Previous page' })).toBeEnabled();
  });

  it('the search box narrows the list (same paging and sort) and an empty search shows everyone again', async () => {
    const list = patientList([...ALL, patientA, patientB]);
    server.use(signedInAs(USER), list.handler);
    renderAt('/dashboard?page=2');
    await screen.findByRole('table', { name: 'Patients' });

    await userEvent.type(screen.getByLabelText('Name'), 'Sample');
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));
    const results = await screen.findByRole('table', { name: 'Search results' });
    expect(window.location.search).toBe('?name=Sample');
    expect(list.seen.at(-1)).toEqual({ name: 'Sample', _count: '21', _offset: '0', _sort: 'family,given' });
    expect(rowsOf(results)).toHaveLength(1);
    expect(screen.getByRole('status')).toHaveTextContent('1 patient found');

    await userEvent.clear(screen.getByLabelText('Name'));
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));
    await screen.findByRole('table', { name: 'Patients' });
    expect(window.location.search).toBe('');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('pages through search results too', async () => {
    const list = patientList();
    server.use(signedInAs(USER), list.handler);
    renderAt('/dashboard?name=Family');
    await screen.findByRole('table', { name: 'Search results' });
    expect(screen.getByRole('status')).toHaveTextContent('Showing matches 1–20');
    await userEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(window.location.search).toBe('?name=Family&page=2'));
    expect(list.seen.at(-1)).toEqual({ name: 'Family', _count: '21', _offset: '20', _sort: 'family,given' });
  });
});

describe('recent patients (mode A)', () => {
  async function openFromList(name: string) {
    const link = await screen.findByRole('link', { name });
    await userEvent.click(link);
  }

  it('remembers opened patients (most recent first), shows them live on the landing page, and stores ids only', async () => {
    const reads = patientReads();
    server.use(signedInAs(USER), patientList([patientA, patientB, patientDeceased]).handler, reads.handler);
    renderAt('/dashboard');
    await screen.findByRole('table', { name: 'Patients' });
    expect(screen.queryByRole('heading', { name: 'Recent patients' })).toBeNull();

    await openFromList('Samplefamily, Ada Quinn');
    await screen.findByRole('heading', { level: 1, name: 'Ada Samplefamily' });
    await userEvent.click(screen.getByRole('link', { name: 'Find another patient' }));
    await openFromList('Otherfamily, Bram');
    await screen.findByRole('heading', { level: 1, name: 'Bram Otherfamily' });
    await userEvent.click(screen.getByRole('link', { name: 'Find another patient' }));

    const recent = await screen.findByRole('region', { name: 'Recent patients' });
    const table = within(recent).getByRole('table', { name: 'Recent patients' });
    await waitFor(() => expect(rowsOf(table)).toHaveLength(2));
    expect(rowsOf(table)[0]).toHaveTextContent('Otherfamily, Bram');
    expect(rowsOf(table)[0]).toHaveTextContent('SYN-2002');
    expect(rowsOf(table)[1]).toHaveTextContent('Samplefamily, Ada Quinn');
    expect(rowsOf(table)[1]).toHaveTextContent('1980-06-15');
    expect(table).not.toHaveTextContent('900-');
    expect(within(table).getByRole('link', { name: 'Otherfamily, Bram' })).toHaveAttribute('href', `/patient/${PATIENT_B_ID}`);

    // Storage: one entry, under a hashed key, holding the two ids and nothing else.
    const key = await recentStorageKey(USER);
    expect(Object.keys(window.localStorage).filter((k) => k.startsWith('dash.recentPatients'))).toEqual([key]);
    const stored = window.localStorage.getItem(key ?? '') ?? '';
    expect(JSON.parse(stored)).toEqual([PATIENT_B_ID, PATIENT_A_ID]);
    const everything = JSON.stringify({ ...window.localStorage });
    for (const phi of ['Samplefamily', 'Otherfamily', 'Ada', 'Bram', '1980-06-15', '1955-01-02', 'SYN-1001', 'SYN-2002', '900-11-2222', 'female', 'Practitioner']) {
      expect(everything).not.toContain(phi);
    }
  });

  it('reads the stored list on sign-in and drops a missing or forbidden patient silently', async () => {
    const key = (await recentStorageKey(USER)) ?? '';
    const gone = '9f000000-0000-4000-8000-00000000dead';
    const hidden = '9f000000-0000-4000-8000-00000000beef';
    window.localStorage.setItem(key, JSON.stringify([gone, PATIENT_B_ID, hidden, PATIENT_A_ID]));
    const reads = patientReads({ [hidden]: { status: 403, error: 'not_accessible' } });
    server.use(signedInAs(USER), patientList([]).handler, reads.handler);
    renderAt('/dashboard');

    const recent = await screen.findByRole('region', { name: 'Recent patients' });
    await waitFor(() => expect(rowsOf(within(recent).getByRole('table'))).toHaveLength(2));
    const rows = rowsOf(within(recent).getByRole('table'));
    expect(rows[0]).toHaveTextContent('Otherfamily, Bram');
    expect(rows[1]).toHaveTextContent('Samplefamily, Ada Quinn');
    expect(screen.queryByRole('alert')).toBeNull();
    expect(reads.counts[gone]).toBe(1);
    expect(reads.counts[hidden]).toBe(1);
    // Pruned from storage too, so they are not fetched again.
    await waitFor(() => expect(JSON.parse(window.localStorage.getItem(key) ?? '[]')).toEqual([PATIENT_B_ID, PATIENT_A_ID]));
  });

  it('Clear empties the list and the stored entry', async () => {
    const key = (await recentStorageKey(USER)) ?? '';
    window.localStorage.setItem(key, JSON.stringify([PATIENT_A_ID]));
    server.use(signedInAs(USER), patientList([]).handler, patientReads().handler);
    renderAt('/dashboard');
    const recent = await screen.findByRole('region', { name: 'Recent patients' });
    await within(recent).findByText('Samplefamily, Ada Quinn');
    await userEvent.click(within(recent).getByRole('button', { name: 'Clear recent patients' }));
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Recent patients' })).toBeNull());
    expect(window.localStorage.getItem(key)).toBeNull();
  });

  it('keeps each user apart', async () => {
    const other = await recentStorageKey('https://openemr.invalid/apis/default/fhir/Practitioner/someone-else');
    window.localStorage.setItem(other ?? '', JSON.stringify([PATIENT_A_ID]));
    server.use(signedInAs(USER), patientList(ALL.slice(0, 3)).handler, patientReads().handler);
    renderAt('/dashboard');
    await screen.findByRole('table', { name: 'Patients' });
    expect(screen.queryByRole('region', { name: 'Recent patients' })).toBeNull();
  });

  it('works without storage (blocked or throwing): the list lives in memory for the session', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError');
    });
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError');
    });
    server.use(signedInAs(USER), patientList([patientA, patientB]).handler, patientReads().handler);
    renderAt('/dashboard');
    await openFromList('Samplefamily, Ada Quinn');
    await screen.findByRole('heading', { level: 1, name: 'Ada Samplefamily' });
    await userEvent.click(screen.getByRole('link', { name: 'Find another patient' }));
    const recent = await screen.findByRole('region', { name: 'Recent patients' });
    expect(await within(recent).findByText('Samplefamily, Ada Quinn')).toBeInTheDocument();
    expect(setItem).toHaveBeenCalled();
  });

  it('without a user id (no fhirUser) nothing is stored, but the session still has a recent list', async () => {
    server.use(signedInAs(null), patientList([patientA]).handler, patientReads().handler);
    renderAt('/dashboard');
    await openFromList('Samplefamily, Ada Quinn');
    await screen.findByRole('heading', { level: 1, name: 'Ada Samplefamily' });
    await userEvent.click(screen.getByRole('link', { name: 'Find another patient' }));
    expect(await screen.findByRole('region', { name: 'Recent patients' })).toBeInTheDocument();
    expect(window.localStorage.length).toBe(0);
  });

  it('does not add a patient that could not be opened', async () => {
    const reads = patientReads({ [PATIENT_A_ID]: { status: 403, error: 'not_accessible' } });
    server.use(signedInAs(USER), patientList([patientA]).handler, reads.handler);
    renderAt(`/patient/${PATIENT_A_ID}`);
    await screen.findByText('You do not have access to this patient.');
    await userEvent.click(screen.getByRole('link', { name: 'Find another patient' }));
    await screen.findByRole('table', { name: 'Patients' });
    expect(screen.queryByRole('region', { name: 'Recent patients' })).toBeNull();
    expect(window.localStorage.length).toBe(0);
  });
});
