// UX wave 2 (DASHBOARD_UX_PLAN.md H6, M3, M8, §6 copy): the landing page's
// search form, list card, recent-patients card, and their loading, empty and
// error states.
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

beforeEach(() => window.localStorage.clear());
afterEach(() => {
  document.title = 'Patient Dashboard';
});

describe('partly typed date of birth (H6)', () => {
  it('blocks the search, says what to do, marks and focuses the field, and sends no request', async () => {
    const search = patientSearch();
    server.use(signedIn, search.handler);
    renderAt('/dashboard');
    await screen.findByRole('table', { name: 'All patients' });
    expect(search.seen).toHaveLength(1);

    await userEvent.type(screen.getByLabelText('Name'), 'Sample');
    const dob = screen.getByLabelText('Date of birth');
    markIncomplete(dob);
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));

    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Enter the full date of birth, or clear the field.');
    expect(dob).toHaveAttribute('aria-invalid', 'true');
    expect(dob).toHaveAccessibleDescription('Enter the full date of birth, or clear the field.');
    expect(dob).toHaveFocus();
    expect(window.location.search).toBe('');
    // Give a stray request time to show up, then check none was sent.
    await new Promise((r) => setTimeout(r, 50));
    expect(search.seen).toHaveLength(1);
  });

  it('pressing Enter in another field is blocked the same way', async () => {
    const search = patientSearch();
    server.use(signedIn, search.handler);
    renderAt('/dashboard');
    await screen.findByRole('table', { name: 'All patients' });
    markIncomplete(screen.getByLabelText('Date of birth'));
    await userEvent.type(screen.getByLabelText('MRN'), 'SYN-1001{Enter}');
    expect(screen.getByRole('alert')).toHaveTextContent('Enter the full date of birth, or clear the field.');
    await new Promise((r) => setTimeout(r, 50));
    expect(search.seen).toHaveLength(1);
  });

  it('never mentions the YYYY-MM-DD format for a bad date in the URL either', async () => {
    server.use(signedIn, patientSearch().handler);
    renderAt('/dashboard?birthdate=1980-06');
    await screen.findByRole('table', { name: 'All patients' });
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Enter the full date of birth, or clear the field.');
    expect(document.body).not.toHaveTextContent('YYYY-MM-DD');
  });
});

describe('search rules copy (§6)', () => {
  it('names and MRNs say which characters to use', async () => {
    const search = patientSearch();
    server.use(signedIn, search.handler);
    renderAt('/dashboard');
    await screen.findByRole('table', { name: 'All patients' });
    await userEvent.type(screen.getByLabelText('Name'), '<b>');
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Use only letters, spaces, apostrophes, hyphens and dots in the name.');
    await userEvent.clear(screen.getByLabelText('Name'));
    await userEvent.type(screen.getByLabelText('MRN'), 'a b');
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Use only letters, digits and . _ | : - in the MRN.');
    expect(search.seen).toHaveLength(1);
  });
});

describe('list card (M3)', () => {
  it('titles the tab "Find a patient – Patient Dashboard"', async () => {
    server.use(signedIn, patientSearch().handler);
    renderAt('/dashboard');
    await screen.findByRole('table', { name: 'All patients' });
    expect(document.title).toBe('Find a patient – Patient Dashboard');
  });

  it('puts the list in an "All patients" card, headed by the count; a search heads it "Search results"', async () => {
    server.use(signedIn, patientSearch().handler);
    const view = renderAt('/dashboard');
    const card = await screen.findByRole('region', { name: 'All patients' });
    expect(within(card).getByRole('heading', { level: 2, name: 'All patients' })).toBeInTheDocument();
    expect(await within(card).findByRole('table', { name: 'All patients' })).toBeInTheDocument();
    expect(within(card).getByRole('status')).toHaveTextContent('2 patients');
    view.unmount();

    renderAt('/dashboard?name=Sample');
    const results = await screen.findByRole('region', { name: 'Search results' });
    expect(await within(results).findByRole('table', { name: 'Search results' })).toBeInTheDocument();
    expect(within(results).getByRole('status')).toHaveTextContent('2 patients found');
    expect(screen.queryByRole('region', { name: 'All patients' })).toBeNull();
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
    await screen.findByRole('table', { name: 'All patients' });
  });

  it('says "No patients to show." when the list is empty', async () => {
    server.use(signedIn, patientSearch(() => HttpResponse.json(searchset())).handler);
    renderAt('/dashboard');
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('No patients to show.'));
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('suggests what to change when nothing matches a search', async () => {
    server.use(signedIn, patientSearch(() => HttpResponse.json(searchset())).handler);
    renderAt('/dashboard?name=Nobody');
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('No patients match. Check the spelling, or search with fewer fields.'));
  });

  it('past the last page, links back to page 1 of the same search', async () => {
    const search = patientSearch(() => HttpResponse.json(searchset()));
    server.use(signedIn, search.handler);
    renderAt('/dashboard?name=Sample&page=4');
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('This page is past the end of the list.'));
    await userEvent.click(screen.getByRole('link', { name: 'Go to page 1' }));
    await waitFor(() => expect(window.location.search).toBe('?name=Sample'));
    expect(search.seen.at(-1)).toMatchObject({ name: 'Sample', _offset: '0' });
  });

  it('shows "Showing 1–20" on a list with more pages', async () => {
    const many: Patient[] = Array.from({ length: 21 }, (_, i) => ({ ...patientA, id: `9e000000-0000-4000-8000-${String(i).padStart(12, '0')}` }));
    server.use(signedIn, patientSearch(() => HttpResponse.json(searchset(...many))).handler);
    renderAt('/dashboard');
    await screen.findByRole('table', { name: 'All patients' });
    expect(screen.getByRole('status')).toHaveTextContent('Showing 1–20');
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
      "OpenEMR didn't accept these search terms. Check the name, date of birth and MRN.",
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

describe('recent patients card', () => {
  it('is its own card with a "Clear list" button, and every row opens the chart', async () => {
    const key = (await recentStorageKey(USER)) ?? '';
    window.localStorage.setItem(key, JSON.stringify([PATIENT_A_ID]));
    server.use(
      signedInAsUser,
      patientSearch(() => HttpResponse.json(searchset(patientB))).handler,
      http.get('*/api/fhir/Patient/:id', () => HttpResponse.json(patientA)),
    );
    renderAt('/dashboard');
    const recent = await screen.findByRole('region', { name: 'Recent patients' });
    const link = await within(recent).findByRole('link', { name: 'Samplefamily, Ada Quinn' });
    // Bootstrap's stretched-link: the name is the one real link, drawn over the whole row.
    expect(link).toHaveClass('stretched-link');
    expect(link.closest('tr')).toHaveClass('row-link');
    await userEvent.click(within(recent).getByRole('button', { name: 'Clear list' }));
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Recent patients' })).toBeNull());
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
