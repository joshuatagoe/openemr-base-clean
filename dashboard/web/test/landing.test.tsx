// The landing page (OpenEMR's Patient Finder): input checks before a search is
// sent, the Finder's wording for the list states, errors, and the recent tab.
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Patient } from 'fhir/r4';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { App } from '../src/App';
import { createQueryClient } from '../src/data/queryClient';
import { recentStorageKey } from '../src/recent/recentPatients';
import { PATIENT_A_ID, patientA, patientB, searchset } from './fixtures/patients';
import { server, signedIn } from './msw/server';

const USER = 'https://openemr.invalid/apis/default/fhir/Practitioner/9f000000-0000-4000-8000-0000000000aa';
const signedInAsUser = http.get('*/auth/me', () =>
  HttpResponse.json({ authenticated: true, user: { displayName: 'Dana Testdoctor', fhirUser: USER }, expiresAt: '2030-01-01T00:00:00.000Z' }),
);

function patientSearch(result: () => Response | Promise<Response> = () => HttpResponse.json(searchset(patientA, patientB))) {
  const seen: Record<string, string>[] = [];
  const handler = http.get('*/api/fhir/Patient', ({ request }) => {
    seen.push(Object.fromEntries(new URL(request.url).searchParams));
    return result();
  });
  return { handler, seen };
}

function renderAt(path: string) {
  window.history.replaceState(null, '', path);
  return render(<App queryClient={createQueryClient({ retryDelay: 0 })} />);
}

/** Chrome reports an incomplete <input type="date"> as value '' with validity.badInput; jsdom can't, so the test sets it. */
function markIncomplete(input: HTMLElement) {
  Object.defineProperty(input, 'validity', { configurable: true, get: () => ({ badInput: true, valid: false }) });
}

const table = () => screen.getByRole('table', { name: 'Patient List' });
const waitPastTypingDelay = () => new Promise((r) => setTimeout(r, 500));

beforeEach(() => window.localStorage.clear());
afterEach(() => {
  document.title = 'Patient Dashboard';
});

describe('partly typed date of birth (H6)', () => {
  it('Enter blocks the search, says what to do, marks and focuses the field, and sends no request', async () => {
    const search = patientSearch();
    server.use(signedIn, search.handler);
    renderAt('/dashboard');
    await screen.findByRole('table', { name: 'Patient List' });
    expect(search.seen).toHaveLength(1);

    const dob = screen.getByLabelText('Search by Date of Birth');
    markIncomplete(dob);
    await userEvent.type(dob, '{Enter}');

    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Enter the full date of birth, or clear the field.');
    expect(dob).toHaveAttribute('aria-invalid', 'true');
    expect(dob).toHaveAccessibleDescription('Enter the full date of birth, or clear the field.');
    expect(dob).toHaveFocus();
    expect(window.location.search).toBe('');
    await waitPastTypingDelay();
    expect(search.seen).toHaveLength(1);
  });

  it('typing in another field waits for the date (no request, no message); Enter there explains', async () => {
    const search = patientSearch();
    server.use(signedIn, search.handler);
    renderAt('/dashboard');
    await screen.findByRole('table', { name: 'Patient List' });
    markIncomplete(screen.getByLabelText('Search by Date of Birth'));
    await userEvent.type(screen.getByLabelText('Search by External ID'), 'SYN-1001');
    await waitPastTypingDelay();
    expect(search.seen).toHaveLength(1);
    expect(screen.queryByRole('alert')).toBeNull();
    await userEvent.type(screen.getByLabelText('Search by External ID'), '{Enter}');
    expect(screen.getByRole('alert')).toHaveTextContent('Enter the full date of birth, or clear the field.');
    expect(search.seen).toHaveLength(1);
  });

  it('never mentions the YYYY-MM-DD format for a bad date in the URL either', async () => {
    server.use(signedIn, patientSearch().handler);
    renderAt('/dashboard?birthdate=1980-06');
    await screen.findByRole('table', { name: 'Patient List' });
    await userEvent.type(screen.getByLabelText('Search by Name'), '{Enter}');
    expect(screen.getByRole('alert')).toHaveTextContent('Enter the full date of birth, or clear the field.');
    expect(document.body).not.toHaveTextContent('YYYY-MM-DD');
  });
});

describe('search rules copy (§6)', () => {
  it('each filter says which characters to use, and nothing is sent', async () => {
    const search = patientSearch();
    server.use(signedIn, search.handler);
    renderAt('/dashboard');
    await screen.findByRole('table', { name: 'Patient List' });
    const cases: Array<[string, string, string]> = [
      ['Search by Name', '<b>', 'Use only letters, spaces, apostrophes, hyphens and dots in the name.'],
      ['Search by External ID', 'a b', 'Use only letters, digits and . _ : - in the External ID.'],
      ['Search by SSN', '900|11', 'Use only letters, digits and . _ : - in the SSN.'],
      ['Search by Home Phone', '555-0100 ext', 'Use only digits, spaces and ( ) + . - in the home phone.'],
    ];
    for (const [label, value, message] of cases) {
      const box = screen.getByLabelText(label);
      await userEvent.type(box, `${value}{Enter}`);
      expect(screen.getByRole('alert')).toHaveTextContent(message);
      expect(box).toHaveAttribute('aria-invalid', 'true');
      await userEvent.clear(box);
    }
    await waitPastTypingDelay();
    expect(search.seen).toHaveLength(1);
  });
});

describe('the Finder list states', () => {
  it('titles the page and the tab "Patient Finder"', async () => {
    server.use(signedIn, patientSearch().handler);
    renderAt('/dashboard');
    await screen.findByRole('table', { name: 'Patient List' });
    expect(screen.getByRole('heading', { level: 1, name: 'Patient Finder' })).toBeInTheDocument();
    expect(document.title).toBe('Patient Finder – Patient Dashboard');
  });

  it('the info line gives the range, and the total when the whole list fits', async () => {
    server.use(signedIn, patientSearch().handler);
    renderAt('/dashboard');
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Showing 1 to 2 of 2 entries'));
  });

  it('shows a spinner with "Loading patients…" while the list loads', async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((r) => {
      release = r;
    });
    server.use(
      signedIn,
      patientSearch(async () => {
        await gate;
        return HttpResponse.json(searchset(patientA));
      }).handler,
    );
    renderAt('/dashboard');
    const status = await screen.findByText('Loading patients…');
    expect(status.closest('[role="status"]')?.querySelector('.spinner')).not.toBeNull();
    release();
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Showing 1 to 1 of 1 entries'));
  });

  it('says "No data available in table" when the list is empty, and keeps the filters usable', async () => {
    server.use(signedIn, patientSearch(() => HttpResponse.json(searchset())).handler);
    renderAt('/dashboard');
    await waitFor(() => expect(table()).toHaveTextContent('No data available in table'));
    expect(screen.getByRole('status')).toHaveTextContent('Showing 0 to 0 of 0 entries');
    expect(screen.getByLabelText('Search by Name')).toBeEnabled();
  });

  it('says "No matching records found" when nothing matches a search', async () => {
    server.use(signedIn, patientSearch(() => HttpResponse.json(searchset())).handler);
    renderAt('/dashboard?name=Nobody');
    await waitFor(() => expect(table()).toHaveTextContent('No matching records found'));
  });

  it('past the last page, links back to page 1 of the same search', async () => {
    const search = patientSearch(() => HttpResponse.json(searchset()));
    server.use(signedIn, search.handler);
    renderAt('/dashboard?name=Sample&page=4');
    await waitFor(() => expect(table()).toHaveTextContent('This page is past the end of the list.'));
    await userEvent.click(screen.getByRole('link', { name: 'Go to page 1' }));
    await waitFor(() => expect(window.location.search).toBe('?name=Sample'));
    expect(search.seen.at(-1)).toMatchObject({ name: 'Sample', _offset: '0' });
  });

  it('shows "Showing 1 to 10" (no total) on a list with more pages', async () => {
    const many: Patient[] = Array.from({ length: 11 }, (_, i) => ({ ...patientA, id: `9e000000-0000-4000-8000-${String(i).padStart(12, '0')}` }));
    server.use(signedIn, patientSearch(() => HttpResponse.json(searchset(...many))).handler);
    renderAt('/dashboard');
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/^Showing 1 to 10$/));
  });
});

describe('list errors say what failed (M8)', () => {
  const cases: Array<[string, () => Response, string, boolean]> = [
    ['network', () => HttpResponse.error(), "Couldn't reach OpenEMR, so the search didn't run. Try again.", true],
    ['timeout', () => HttpResponse.json({ error: 'timeout' }, { status: 504 }), 'OpenEMR took too long to answer. Try again.', true],
    [
      'upstream',
      () => HttpResponse.json({ error: 'upstream_error' }, { status: 502 }),
      'OpenEMR returned an error. Try again; if it keeps happening, tell your OpenEMR administrator.',
      true,
    ],
    [
      'bad_request',
      () => HttpResponse.json({ error: 'bad_request' }, { status: 400 }),
      "OpenEMR didn't accept these search terms. Check the name, phone number, SSN, date of birth and External ID.",
      false,
    ],
  ];

  for (const [kind, reply, message, retry] of cases) {
    it(`${kind}: "${message}"`, async () => {
      server.use(signedIn, patientSearch(reply).handler);
      renderAt('/dashboard?name=Sample');
      expect(await screen.findByRole('alert')).toHaveTextContent(message);
      if (retry) expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
      else expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
    });
  }
});

describe('Recent Patients tab', () => {
  it('has a "Clear list" button, and every row opens the chart', async () => {
    const key = (await recentStorageKey(USER)) ?? '';
    window.localStorage.setItem(key, JSON.stringify([PATIENT_A_ID]));
    server.use(
      signedInAsUser,
      patientSearch(() => HttpResponse.json(searchset(patientB))).handler,
      http.get('*/api/fhir/Patient/:id', () => HttpResponse.json(patientA)),
    );
    renderAt('/dashboard?tab=recent');
    const recent = await screen.findByRole('tabpanel', { name: 'Recent Patients' });
    const link = await within(recent).findByRole('link', { name: 'Ada' });
    // Bootstrap's stretched-link: the first column is the one real link, drawn over the whole row.
    expect(link).toHaveClass('stretched-link');
    expect(link.closest('tr')).toHaveClass('row-link');
    await userEvent.click(within(recent).getByRole('button', { name: 'Clear list' }));
    await waitFor(() => expect(recent).toHaveTextContent('No recent patients'));
  });
});

describe('home page', () => {
  it('says "Checking your sign-in…" while the session is checked', async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((r) => {
      release = r;
    });
    server.use(
      http.get('*/auth/me', async () => {
        await gate;
        return HttpResponse.json({ error: 'unauthenticated' }, { status: 401 });
      }),
    );
    renderAt('/');
    expect(await screen.findByText('Checking your sign-in…')).toBeInTheDocument();
    release();
    expect(await screen.findAllByRole('link', { name: /Sign in/ })).not.toHaveLength(0);
  });
});
