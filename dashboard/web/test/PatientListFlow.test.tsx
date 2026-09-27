// Mode A landing page: OpenEMR's Patient Finder (Patient List and Recent
// Patients tabs), with paging, page size, sorting, the column filters, the
// global search, and the recent-patients list.
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
    birthDate: `19${String(40 + (n % 50)).padStart(2, '0')}-01-01`,
    telecom: [
      { system: 'phone', value: `555-0${String(n).padStart(3, '0')}`, use: 'home' },
      { system: 'phone', value: '555-9999', use: 'work' },
    ],
  };
}
const ALL = Array.from({ length: 45 }, (_, i) => listPatient(i + 1));

const officialOf = (p: Patient) => p.name?.find((n) => n.use === 'official') ?? p.name?.[0];
const idValues = (p: Patient) => (p.identifier ?? []).map((i) => i.value);
const criteriaOf = (q: Record<string, string>) => Object.keys(q).filter((k) => !k.startsWith('_'));

/**
 * Mock OpenEMR's Patient search: name (prefix on any name part), identifier
 * (SSN or External ID, exact), phone (home, work or mobile, exact), birthdate
 * (prefix), then _sort (a leading '-' reverses), _offset and _count. Like
 * OpenEMR: no next link, total = entries returned.
 */
function patientList(pool: Patient[] = ALL) {
  const seen: Record<string, string>[] = [];
  const handler = http.get('*/api/fhir/Patient', ({ request }) => {
    const q = new URL(request.url).searchParams;
    seen.push(Object.fromEntries(q));
    let matches = pool;
    const name = q.get('name')?.toLowerCase();
    if (name) matches = matches.filter((p) => [officialOf(p)?.family, ...(officialOf(p)?.given ?? [])].some((x) => x?.toLowerCase().startsWith(name)));
    const identifier = q.get('identifier');
    if (identifier) matches = matches.filter((p) => idValues(p).includes(identifier));
    const phone = q.get('phone');
    if (phone) matches = matches.filter((p) => (p.telecom ?? []).some((t) => t.value === phone));
    const birthdate = q.get('birthdate');
    if (birthdate) matches = matches.filter((p) => p.birthDate?.startsWith(birthdate));
    if (q.get('_sort')?.startsWith('-')) matches = [...matches].reverse();
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
    const p = byId[id] ?? ALL.find((x) => x.id === id);
    return p ? HttpResponse.json(p) : HttpResponse.json({ error: 'not_found' }, { status: 404 });
  });
  return { handler, counts };
}

function renderAt(path: string) {
  window.history.replaceState(null, '', path);
  return render(<App queryClient={createQueryClient({ retryDelay: 0 })} />);
}

const listTable = () => screen.getByRole('table', { name: 'Patient List' });
/** Body rows (the head has the filter row and the title row). */
const bodyRows = (table: HTMLElement) => within(table).getAllByRole('row').slice(2);
const info = () => screen.getByRole('status');

beforeEach(() => window.localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe('Patient Finder: Patient List tab (landing page, mode A)', () => {
  it('lists the first 10 patients sorted by last name, with the Finder columns, tabs, filters and "Show 10 entries"', async () => {
    const list = patientList();
    server.use(signedInAs(USER), list.handler);
    renderAt('/dashboard');

    const table = await screen.findByRole('table', { name: 'Patient List' });
    await waitFor(() => expect(bodyRows(table)).toHaveLength(10));
    expect(list.seen[0]).toEqual({ _count: '11', _offset: '0', _sort: 'family,given' });
    const rows = bodyRows(table);
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['Full Name', 'Home Phone', 'SSN', 'Date of Birth', 'External ID']);
    expect(rows[0]).toHaveTextContent('Family001, Given');
    expect(rows[0]).toHaveTextContent('555-0001');
    expect(rows[0]).toHaveTextContent('900-00-0001');
    expect(rows[0]).toHaveTextContent('1941-01-01');
    expect(rows[0]).toHaveTextContent('LST-1');
    // The home number only (not the work number).
    expect(table).not.toHaveTextContent('555-9999');
    expect(rows[9]).toHaveTextContent('Family010, Given');
    expect(within(rows[0] as HTMLElement).getByRole('link', { name: 'Family001, Given' })).toHaveAttribute('href', `/patient/${ALL[0]?.id ?? ''}`);

    for (const f of ['Search by Name', 'Search by Home Phone', 'Search by SSN', 'Search by Date of Birth', 'Search by External ID']) {
      expect(screen.getByLabelText(f)).toBeInTheDocument();
    }
    expect(screen.getByPlaceholderText('Search by SSN')).toBeInTheDocument();
    expect(screen.getByLabelText('Search:')).toHaveAttribute('type', 'search');
    const size = screen.getByRole('combobox', { name: 'Show entries' });
    expect(size).toHaveValue('10');
    expect(within(size).getAllByRole('option').map((o) => o.textContent)).toEqual(['10', '25', '50', '100']);
    const tabs = screen.getAllByRole('tab');
    expect(tabs.map((t) => t.textContent)).toEqual(['Patient List', 'Recent Patients']);
    expect(tabs[0]).toHaveAttribute('aria-selected', 'true');
    // Read-only port: no Add New Patient.
    expect(screen.queryByText('Add New Patient')).toBeNull();

    const pager = screen.getByRole('navigation', { name: 'Patient list pages' });
    expect(within(pager).getByText('Page 1')).toBeInTheDocument();
    expect(within(pager).getByRole('button', { name: 'Previous page' })).toBeDisabled();
    expect(within(pager).getByRole('button', { name: 'Next page' })).toBeEnabled();
    // No overall count from OpenEMR: no "of N" until the last page.
    expect(info()).toHaveTextContent(/^Showing 1 to 10$/);
  });

  it('pages with Next / Previous (keyboard), keeps the page in the URL, moves focus to the list, and gives the total on the last page', async () => {
    const list = patientList();
    server.use(signedInAs(USER), list.handler);
    renderAt('/dashboard?size=25');
    await waitFor(() => expect(bodyRows(listTable())).toHaveLength(25));

    screen.getByRole('button', { name: 'Next page' }).focus();
    await userEvent.keyboard('{Enter}');
    await waitFor(() => expect(bodyRows(listTable())[0]).toHaveTextContent('Family026, Given'));
    expect(window.location.search).toBe('?size=25&page=2');
    expect(list.seen.at(-1)).toEqual({ _count: '26', _offset: '25', _sort: 'family,given' });
    expect(bodyRows(listTable())).toHaveLength(20);
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled();
    expect(info()).toHaveTextContent('Showing 26 to 45 of 45 entries');
    await waitFor(() => expect(listTable()).toHaveFocus());

    screen.getByRole('button', { name: 'Previous page' }).focus();
    await userEvent.keyboard(' ');
    await waitFor(() => expect(bodyRows(listTable())[0]).toHaveTextContent('Family001, Given'));
    expect(window.location.search).toBe('?size=25');
  });

  it('"Show [10|25|50|100] entries" sets the page size (one probe row more) and returns to page 1', async () => {
    const list = patientList();
    server.use(signedInAs(USER), list.handler);
    renderAt('/dashboard?page=2');
    await waitFor(() => expect(bodyRows(listTable())[0]).toHaveTextContent('Family011, Given'));

    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Show entries' }), '50');
    await waitFor(() => expect(bodyRows(listTable())).toHaveLength(45));
    expect(window.location.search).toBe('?size=50');
    expect(list.seen.at(-1)).toEqual({ _count: '51', _offset: '0', _sort: 'family,given' });
    expect(info()).toHaveTextContent('Showing 1 to 45 of 45 entries');
    expect(screen.queryByRole('navigation', { name: 'Patient list pages' })).toBeNull();

    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Show entries' }), '100');
    await waitFor(() => expect(list.seen.at(-1)).toEqual({ _count: '101', _offset: '0', _sort: 'family,given' }));
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Show entries' }), '10');
    await waitFor(() => expect(window.location.search).toBe(''));
  });

  it('sorts by the columns OpenEMR can sort (first click ascending, again descending, back to page 1); SSN is not sortable', async () => {
    const list = patientList();
    server.use(signedInAs(USER), list.handler);
    renderAt('/dashboard?page=3');
    await screen.findByRole('table', { name: 'Patient List' });
    const header = (name: string) => within(listTable()).getByRole('columnheader', { name });
    expect(header('Full Name')).toHaveAttribute('aria-sort', 'ascending');

    await userEvent.click(within(header('Full Name')).getByRole('button', { name: 'Full Name' }));
    await waitFor(() => expect(list.seen.at(-1)).toEqual({ _count: '11', _offset: '0', _sort: '-family,-given' }));
    expect(window.location.search).toBe('?sort=-name');
    await waitFor(() => expect(bodyRows(listTable())[0]).toHaveTextContent('Family045, Given'));
    expect(header('Full Name')).toHaveAttribute('aria-sort', 'descending');

    for (const [title, sort, code] of [
      ['Home Phone', 'phone', 'phone'],
      ['Date of Birth', 'birthdate', 'dob'],
      ['External ID', 'identifier', 'external'],
    ] as const) {
      await userEvent.click(within(header(title)).getByRole('button', { name: title }));
      await waitFor(() => expect(list.seen.at(-1)).toMatchObject({ _sort: sort }));
      expect(window.location.search).toBe(`?sort=${code}`);
      expect(header(title)).toHaveAttribute('aria-sort', 'ascending');
      expect(header('Full Name')).not.toHaveAttribute('aria-sort');
    }
    expect(within(header('SSN')).queryByRole('button')).toBeNull();
    expect(header('SSN')).not.toHaveAttribute('aria-sort');
  });

  it('opens a page straight from the URL, and treats a bad page number as page 1', async () => {
    const list = patientList();
    server.use(signedInAs(USER), list.handler);
    const first = renderAt('/dashboard?page=5');
    await waitFor(() => expect(bodyRows(listTable())[0]).toHaveTextContent('Family041, Given'));
    expect(list.seen.at(-1)).toMatchObject({ _offset: '40' });
    first.unmount();

    for (const bad of ['0', '-1', 'abc', '1.5', '99999999']) {
      renderAt(`/dashboard?page=${bad}`).unmount();
    }
    await waitFor(() => expect(list.seen.slice(1).every((q) => q._offset === '0')).toBe(true));
  });

  it('says so on a page past the end, with Previous still available', async () => {
    server.use(signedInAs(USER), patientList(ALL.slice(0, 5)).handler);
    renderAt('/dashboard?page=4');
    await waitFor(() => expect(listTable()).toHaveTextContent('This page is past the end of the list.'));
    expect(screen.getByRole('button', { name: 'Previous page' })).toBeEnabled();
  });

  it('filters as you type (after a short pause), like the Finder; Clear search shows everyone again', async () => {
    const list = patientList([...ALL, patientA, patientB]);
    server.use(signedInAs(USER), list.handler);
    renderAt('/dashboard?page=2');
    await screen.findByRole('table', { name: 'Patient List' });

    await userEvent.type(screen.getByLabelText('Search by Name'), 'Sample');
    await waitFor(() => expect(window.location.search).toBe('?name=Sample'));
    await waitFor(() => expect(list.seen.at(-1)).toEqual({ name: 'Sample', _count: '11', _offset: '0', _sort: 'family,given' }));
    // One request for the finished word, not one per letter.
    expect(list.seen.filter((q) => q.name !== undefined)).toHaveLength(1);
    await waitFor(() => expect(bodyRows(listTable())).toHaveLength(1));
    expect(info()).toHaveTextContent('Showing 1 to 1 of 1 entries');

    await userEvent.click(screen.getByRole('button', { name: 'Clear search' }));
    await waitFor(() => expect(bodyRows(listTable())).toHaveLength(10));
    expect(window.location.search).toBe('');
    expect(screen.getByLabelText('Search by Name')).toHaveValue('');
  });

  it('pages through filtered results too', async () => {
    const list = patientList();
    server.use(signedInAs(USER), list.handler);
    renderAt('/dashboard?name=Family');
    await waitFor(() => expect(info()).toHaveTextContent('Showing 1 to 10'));
    await userEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(window.location.search).toBe('?name=Family&page=2'));
    expect(list.seen.at(-1)).toEqual({ name: 'Family', _count: '11', _offset: '10', _sort: 'family,given' });
  });

  it('"Open in New Browser Tab" opens charts in a new tab', async () => {
    server.use(signedInAs(USER), patientList(ALL.slice(0, 2)).handler);
    renderAt('/dashboard');
    const link = await screen.findByRole('link', { name: 'Family001, Given' });
    expect(link).not.toHaveAttribute('target');
    await userEvent.click(screen.getByRole('checkbox', { name: 'Open in New Browser Tab' }));
    expect(screen.getByRole('link', { name: 'Family001, Given' })).toHaveAttribute('target', '_blank');
    expect(screen.getByRole('link', { name: 'Family001, Given' })).toHaveAttribute('rel', 'noopener');
  });
});

describe('Patient Finder: Search with exact method', () => {
  it('matches whole names (name:exact), kept in the URL', async () => {
    const list = patientList([...ALL, patientA]);
    server.use(signedInAs(USER), list.handler);
    renderAt('/dashboard?name=Samplefamily');
    await screen.findByRole('table', { name: 'Patient List' });
    await userEvent.click(screen.getByRole('checkbox', { name: 'Search with exact method' }));
    await waitFor(() => expect(list.seen.at(-1)).toEqual({ 'name:exact': 'Samplefamily', _count: '11', _offset: '0', _sort: 'family,given' }));
    expect(window.location.search).toBe('?name=Samplefamily&exact=1');
    expect(screen.getByRole('checkbox', { name: 'Search with exact method' })).toBeChecked();
  });
});

describe('Patient Finder: SSN, Home Phone and the global search (kept out of the URL and storage)', () => {
  // Its External ID equals patient 7's SSN: OpenEMR's identifier search returns both.
  const twin: Patient = { ...listPatient(46), identifier: [ssn('111-22-3333'), mrn('900-00-0007')] };

  it('Search by SSN matches the SSN only (not an External ID with the same value), and never reaches the URL', async () => {
    const list = patientList([...ALL, twin]);
    server.use(signedInAs(USER), list.handler);
    renderAt('/dashboard?name=Family');
    await screen.findByRole('table', { name: 'Patient List' });

    await userEvent.type(screen.getByLabelText('Search by SSN'), '900-00-0007{Enter}');
    await waitFor(() => expect(list.seen.at(-1)).toEqual({ name: 'Family', identifier: '900-00-0007', _count: '101', _sort: 'family,given' }));
    await waitFor(() => expect(bodyRows(listTable())).toHaveLength(1));
    expect(bodyRows(listTable())[0]).toHaveTextContent('Family007, Given');
    expect(info()).toHaveTextContent('Showing 1 to 1 of 1 entries');
    expect(window.location.href).not.toContain('900-00-0007');
    expect(window.location.search).toBe('?name=Family');

    await userEvent.clear(screen.getByLabelText('Search by SSN'));
    await userEvent.type(screen.getByLabelText('Search by External ID'), '900-00-0007{Enter}');
    await waitFor(() => expect(bodyRows(listTable())[0]).toHaveTextContent('Family046, Given'));
    expect(bodyRows(listTable())).toHaveLength(1);
    // The External ID may be in the URL (as the MRN was); the SSN never is.
    expect(window.location.search).toBe('?name=Family&identifier=900-00-0007');
  });

  it('Search by Home Phone matches the home number only, never reaches the URL, and comes back after visiting a chart', async () => {
    const list = patientList();
    server.use(signedInAs(USER), list.handler, patientReads().handler);
    renderAt('/dashboard');
    await screen.findByRole('table', { name: 'Patient List' });

    // 555-9999 is every patient's work number: OpenEMR's phone search matches it, the Home Phone column must not.
    await userEvent.type(screen.getByLabelText('Search by Home Phone'), '555-9999{Enter}');
    await waitFor(() => expect(list.seen.at(-1)).toEqual({ phone: '555-9999', _count: '101', _sort: 'family,given' }));
    await waitFor(() => expect(listTable()).toHaveTextContent('No matching records found'));

    await userEvent.clear(screen.getByLabelText('Search by Home Phone'));
    await userEvent.type(screen.getByLabelText('Search by Home Phone'), '555-0003{Enter}');
    const link = await screen.findByRole('link', { name: 'Family003, Given' });
    expect(window.location.search).toBe('');

    await userEvent.click(link);
    await screen.findByRole('heading', { level: 1, name: 'Given Family003' });
    expect(window.location.href).not.toContain('555-0003');
    await userEvent.click(screen.getByRole('link', { name: 'Find another patient' }));
    await waitFor(() => expect(screen.getByLabelText('Search by Home Phone')).toHaveValue('555-0003'));
    await waitFor(() => expect(bodyRows(listTable())).toHaveLength(1));
    const everything = JSON.stringify({ ...window.localStorage, ...window.sessionStorage });
    expect(everything).not.toContain('555-0003');
  });

  it('the global "Search:" box runs name / identifier / phone / birthdate searches in parallel and merges them by patient', async () => {
    const list = patientList([...ALL, twin, patientA]);
    server.use(signedInAs(USER), list.handler);
    renderAt('/dashboard');
    await screen.findByRole('table', { name: 'Patient List' });
    const box = screen.getByLabelText('Search:');
    expect(box).toHaveAccessibleDescription(/names that start with the text/);

    // Digits and a hyphen: identifier (SSN or External ID) and phone. Patient 7 (SSN) and the twin (External ID) match.
    await userEvent.type(box, '900-00-0007{Enter}');
    await waitFor(() => expect(bodyRows(listTable())).toHaveLength(2));
    expect(list.seen.slice(-2).map((q) => criteriaOf(q)[0]).sort()).toEqual(['identifier', 'phone']);
    expect(bodyRows(listTable())[0]).toHaveTextContent('Family007, Given');
    expect(bodyRows(listTable())[1]).toHaveTextContent('Family046, Given');
    expect(window.location.search).toBe('');

    // Letters: name and identifier.
    await userEvent.clear(box);
    await userEvent.type(box, 'Sample{Enter}');
    await waitFor(() => expect(bodyRows(listTable())).toHaveLength(1));
    expect(bodyRows(listTable())[0]).toHaveTextContent('Samplefamily, Ada Quinn');
    expect(list.seen.slice(-2).map((q) => criteriaOf(q)[0]).sort()).toEqual(['identifier', 'name']);
    expect(window.location.href).not.toContain('Sample');

    // A year: identifier, phone and birthdate.
    await userEvent.clear(box);
    await userEvent.type(box, '1941{Enter}');
    await waitFor(() => expect(bodyRows(listTable())[0]).toHaveTextContent('Family001, Given'));
    expect(list.seen.slice(-3).map((q) => criteriaOf(q)[0]).sort()).toEqual(['birthdate', 'identifier', 'phone']);

    // Text that can't be any field.
    await userEvent.clear(box);
    await userEvent.type(box, '<b>{Enter}');
    expect(screen.getByRole('alert')).toHaveTextContent('Search with letters for a name, or digits for a phone number, SSN, External ID or date of birth.');
    expect(box).toHaveAttribute('aria-invalid', 'true');
  });

  it('says when more than 100 patients match a search done in the browser', async () => {
    const many = Array.from({ length: 120 }, (_, i) => listPatient(i + 1));
    server.use(signedInAs(USER), patientList(many).handler);
    renderAt('/dashboard');
    await screen.findByRole('table', { name: 'Patient List' });
    await userEvent.type(screen.getByLabelText('Search:'), 'Family{Enter}');
    await waitFor(() => expect(info()).toHaveTextContent('Showing 1 to 10 of the first 100 matches'));
    expect(screen.getByText(/More than 100 patients match, so only the first 100 are shown/)).toBeInTheDocument();
  });
});

describe('Patient Finder: Recent Patients tab (mode A)', () => {
  async function openFromList(name: string) {
    const link = await screen.findByRole('link', { name });
    await userEvent.click(link);
  }
  async function showRecent() {
    await userEvent.click(await screen.findByRole('tab', { name: 'Recent Patients' }));
    return screen.getByRole('tabpanel', { name: 'Recent Patients' });
  }

  it('remembers opened patients (most recent first), shows them live with the Finder recent columns, and stores ids only', async () => {
    const reads = patientReads();
    server.use(signedInAs(USER), patientList([patientA, patientB, patientDeceased]).handler, reads.handler);
    renderAt('/dashboard');
    await screen.findByRole('table', { name: 'Patient List' });

    await openFromList('Samplefamily, Ada Quinn');
    await screen.findByRole('heading', { level: 1, name: 'Ada Samplefamily' });
    await userEvent.click(screen.getByRole('link', { name: 'Find another patient' }));
    await openFromList('Otherfamily, Bram');
    await screen.findByRole('heading', { level: 1, name: 'Bram Otherfamily' });
    await userEvent.click(screen.getByRole('link', { name: 'Find another patient' }));

    const panel = await showRecent();
    expect(window.location.search).toBe('?tab=recent');
    const table = await within(panel).findByRole('table', { name: 'Recent Patients' });
    await waitFor(() => expect(within(table).getAllByRole('row').slice(1)).toHaveLength(2));
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['First Name', 'Middle Name', 'Last Name', 'Date of Birth']);
    const rows = within(table).getAllByRole('row').slice(1);
    expect(within(rows[0] as HTMLElement).getAllByRole('cell').map((c) => c.textContent)).toEqual(['Bram', '', 'Otherfamily', '1955-01-02']);
    expect(within(rows[1] as HTMLElement).getAllByRole('cell').map((c) => c.textContent)).toEqual(['Ada', 'Quinn', 'Samplefamily', '1980-06-15']);
    expect(table).not.toHaveTextContent('900-');
    expect(within(table).getByRole('link', { name: 'Bram' })).toHaveAttribute('href', `/patient/${PATIENT_B_ID}`);

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

  it('switches tabs with the arrow keys (WAI-ARIA tabs)', async () => {
    server.use(signedInAs(USER), patientList(ALL.slice(0, 3)).handler);
    renderAt('/dashboard');
    const listTab = await screen.findByRole('tab', { name: 'Patient List' });
    listTab.focus();
    await userEvent.keyboard('{ArrowRight}');
    const recentTab = screen.getByRole('tab', { name: 'Recent Patients' });
    expect(recentTab).toHaveFocus();
    expect(recentTab).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tabpanel', { name: 'Recent Patients' })).toHaveTextContent('No recent patients');
    await userEvent.keyboard('{Home}');
    expect(screen.getByRole('tab', { name: 'Patient List' })).toHaveFocus();
    expect(window.location.search).toBe('');
  });

  it('reads the stored list on sign-in and drops a missing or forbidden patient silently', async () => {
    const key = (await recentStorageKey(USER)) ?? '';
    const gone = '9f000000-0000-4000-8000-00000000dead';
    const hidden = '9f000000-0000-4000-8000-00000000beef';
    window.localStorage.setItem(key, JSON.stringify([gone, PATIENT_B_ID, hidden, PATIENT_A_ID]));
    const reads = patientReads({ [hidden]: { status: 403, error: 'not_accessible' } });
    server.use(signedInAs(USER), patientList([]).handler, reads.handler);
    renderAt('/dashboard?tab=recent');

    const panel = await screen.findByRole('tabpanel', { name: 'Recent Patients' });
    const table = await within(panel).findByRole('table', { name: 'Recent Patients' });
    await waitFor(() => expect(within(table).getAllByRole('row').slice(1)).toHaveLength(2));
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows[0]).toHaveTextContent('Otherfamily');
    expect(rows[1]).toHaveTextContent('Samplefamily');
    expect(screen.queryByRole('alert')).toBeNull();
    expect(reads.counts[gone]).toBe(1);
    expect(reads.counts[hidden]).toBe(1);
    // Pruned from storage too, so they are not fetched again.
    await waitFor(() => expect(JSON.parse(window.localStorage.getItem(key) ?? '[]')).toEqual([PATIENT_B_ID, PATIENT_A_ID]));
  });

  it('Clear list empties the list and the stored entry', async () => {
    const key = (await recentStorageKey(USER)) ?? '';
    window.localStorage.setItem(key, JSON.stringify([PATIENT_A_ID]));
    server.use(signedInAs(USER), patientList([]).handler, patientReads().handler);
    renderAt('/dashboard?tab=recent');
    const panel = await screen.findByRole('tabpanel', { name: 'Recent Patients' });
    await within(panel).findByText('Samplefamily');
    await userEvent.click(within(panel).getByRole('button', { name: 'Clear list' }));
    await waitFor(() => expect(panel).toHaveTextContent('No recent patients'));
    expect(window.localStorage.getItem(key)).toBeNull();
  });

  it('keeps each user apart', async () => {
    const other = await recentStorageKey('https://openemr.invalid/apis/default/fhir/Practitioner/someone-else');
    window.localStorage.setItem(other ?? '', JSON.stringify([PATIENT_A_ID]));
    server.use(signedInAs(USER), patientList(ALL.slice(0, 3)).handler, patientReads().handler);
    renderAt('/dashboard?tab=recent');
    expect(await screen.findByText('No recent patients')).toBeInTheDocument();
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
    const panel = await showRecent();
    expect(await within(panel).findByText('Samplefamily')).toBeInTheDocument();
    expect(setItem).toHaveBeenCalled();
  });

  it('without a user id (no fhirUser) nothing is stored, but the session still has a recent list', async () => {
    server.use(signedInAs(null), patientList([patientA]).handler, patientReads().handler);
    renderAt('/dashboard');
    await openFromList('Samplefamily, Ada Quinn');
    await screen.findByRole('heading', { level: 1, name: 'Ada Samplefamily' });
    await userEvent.click(screen.getByRole('link', { name: 'Find another patient' }));
    const panel = await showRecent();
    expect(await within(panel).findByRole('table', { name: 'Recent Patients' })).toBeInTheDocument();
    expect(window.localStorage.length).toBe(0);
  });

  it('does not add a patient that could not be opened', async () => {
    const reads = patientReads({ [PATIENT_A_ID]: { status: 403, error: 'not_accessible' } });
    server.use(signedInAs(USER), patientList([patientA]).handler, reads.handler);
    renderAt(`/patient/${PATIENT_A_ID}`);
    await screen.findByText("Your OpenEMR account doesn't have access to this patient's chart.");
    await userEvent.click(screen.getByRole('link', { name: 'Find another patient' }));
    await screen.findByRole('table', { name: 'Patient List' });
    const panel = await showRecent();
    expect(panel).toHaveTextContent('No recent patients');
    expect(window.localStorage.length).toBe(0);
  });
});
