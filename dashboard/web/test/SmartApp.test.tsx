import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createQueryClient } from '../src/data/queryClient';
import { PENDING_KEY } from '../src/smart/launch';
import { SmartApp } from '../src/smart/SmartApp';
import { bundle, careTeam, medReq, ORG_1, organization, PRACT_1, practitioner, practitionerParticipant } from './fixtures/clinical';
import { PATIENT_A_ID, patientA } from './fixtures/patients';
import { server } from './msw/server';

// Modes B/C: the app is served by OpenEMR itself, so OpenEMR's FHIR API, its
// OAuth endpoints and the app share jsdom's origin (http://localhost:3000).
const ORIGIN = window.location.origin;
const ISS = `${ORIGIN}/apis/default/fhir`;
const CLIENT = 'synthetic-client-id-0001';
const DRDASH = '75000000-0000-4000-8000-000000000005';

const config = http.get('*/dashboard.config.json', () => HttpResponse.json({ clientId: CLIENT }));
const smartConfiguration = http.get('*/apis/default/fhir/.well-known/smart-configuration', () =>
  HttpResponse.json({
    authorization_endpoint: `${ORIGIN}/oauth2/default/authorize`,
    token_endpoint: `${ORIGIN}/oauth2/default/token`,
    code_challenge_methods_supported: ['S256'],
  }),
);

function idToken(claims: Record<string, unknown>): string {
  const b64 = (o: unknown) => btoa(JSON.stringify(o)).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_');
  return `${b64({ alg: 'RS256' })}.${b64(claims)}.sig`;
}

const token = http.post('*/oauth2/default/token', () =>
  HttpResponse.json({
    access_token: 'synthetic-access-token',
    token_type: 'Bearer',
    expires_in: 3600,
    patient: PATIENT_A_ID,
    scope: 'launch openid fhirUser patient/Patient.rs',
    id_token: idToken({ fhirUser: `${ISS}/Practitioner/${DRDASH}` }),
  }),
);

const emptyBundle = () => HttpResponse.json(bundle());
/** The patient-context FHIR API: every request must carry the bearer token. */
function fhirApi(seenAuth: string[]) {
  const auth = (request: Request) => seenAuth.push(request.headers.get('authorization') ?? 'none');
  return [
    http.get('*/apis/default/fhir/Patient/:id', ({ request }) => {
      auth(request);
      return HttpResponse.json(patientA);
    }),
    http.get('*/apis/default/fhir/Practitioner/:id', ({ request, params }) => {
      auth(request);
      if (params.id === DRDASH) return HttpResponse.json(practitioner(DRDASH, 'Dana', 'Dashboard'));
      return HttpResponse.json(practitioner(PRACT_1, 'Gale', 'Synthetic'));
    }),
    http.get('*/apis/default/fhir/Organization/:id', ({ request }) => {
      auth(request);
      return HttpResponse.json(organization(ORG_1, 'Synthetic Clinic'));
    }),
    http.get('*/apis/default/fhir/CareTeam', ({ request }) => {
      auth(request);
      return HttpResponse.json(bundle(careTeam('t1', { name: 'Synthetic team', participants: [practitionerParticipant(PRACT_1, { role: 'Physician', org: ORG_1 })] })));
    }),
    http.get('*/apis/default/fhir/MedicationRequest', ({ request }) => {
      auth(request);
      return HttpResponse.json(bundle(medReq('rx-1', 'Metformin HCl 500 mg', { sig: '1 tab BID' })));
    }),
    http.get('*/apis/default/fhir/AllergyIntolerance', emptyBundle),
    http.get('*/apis/default/fhir/Condition', emptyBundle),
    http.get('*/apis/default/fhir/Observation', emptyBundle),
  ];
}

function seedPending() {
  window.sessionStorage.setItem(
    PENDING_KEY,
    JSON.stringify({ state: 'state-1', verifier: 'verifier-1', fhirBaseUrl: ISS, tokenEndpoint: `${ORIGIN}/oauth2/default/token`, createdAt: Date.now() }),
  );
}

function renderAt(path: string, navigate = vi.fn()) {
  window.history.replaceState(null, '', path);
  render(<SmartApp navigate={navigate} queryClient={createQueryClient({ retryDelay: 0 })} />);
  return navigate;
}

beforeEach(() => window.localStorage.clear());
afterEach(() => window.sessionStorage.clear());

describe('SmartApp (modes B/C)', () => {
  it('without launch parameters, says to open the dashboard from OpenEMR (no sign-in link, no search)', async () => {
    server.use(config);
    renderAt('/');
    expect(await screen.findByText(/Open this dashboard from a patient's chart in OpenEMR/)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /sign in/i })).toBeNull();
    expect(screen.queryByRole('searchbox')).toBeNull();
  });

  it('on an EHR launch, redirects to OpenEMR authorize with the configured client, launch and aud', async () => {
    server.use(config, smartConfiguration);
    const navigate = renderAt(`/?launch=opaque-launch&iss=${encodeURIComponent(ISS)}`);
    await vi.waitFor(() => expect(navigate).toHaveBeenCalledTimes(1));
    const url = new URL(String(navigate.mock.calls[0]?.[0]));
    expect(url.pathname).toBe('/oauth2/default/authorize');
    expect(url.searchParams.get('client_id')).toBe(CLIENT);
    expect(url.searchParams.get('launch')).toBe('opaque-launch');
    expect(url.searchParams.get('aud')).toBe(ISS);
    expect(url.searchParams.get('redirect_uri')).toBe(`${ORIGIN}/`);
    expect(screen.getByRole('status')).toHaveTextContent('Signing in with OpenEMR');
  });

  it('refuses a launch whose iss is another origin', async () => {
    server.use(config);
    const navigate = renderAt(`/?launch=l&iss=${encodeURIComponent('https://evil.example/apis/default/fhir')}`);
    expect(await screen.findByRole('alert')).toHaveTextContent('The launch did not come from this OpenEMR server.');
    expect(navigate).not.toHaveBeenCalled();
  });

  it('reports a missing client configuration', async () => {
    server.use(http.get('*/dashboard.config.json', () => new HttpResponse(null, { status: 404 })));
    renderAt(`/?launch=l&iss=${encodeURIComponent(ISS)}`);
    expect(await screen.findByRole('alert')).toHaveTextContent('The dashboard is not configured');
  });

  it('after the callback, shows the launched patient with every card, care-team names resolved and medications combined', async () => {
    const seenAuth: string[] = [];
    server.use(config, token, ...fhirApi(seenAuth));
    seedPending();
    renderAt('/?code=code-1&state=state-1');

    expect(await screen.findByRole('heading', { level: 1, name: /Ada/ })).toBeInTheDocument();
    expect(await screen.findByText('Signed in as Dana Dashboard')).toBeInTheDocument();
    // Code and state are removed from the address bar.
    expect(window.location.search).toBe('');
    // The patient is fixed to the launch context: no search, no switching in the app.
    expect(screen.queryByRole('link', { name: 'Find another patient' })).toBeNull();
    expect(screen.getByText(/To open another patient, change the chart in OpenEMR/)).toBeInTheDocument();

    const combined = await screen.findByRole('region', { name: 'Medications and prescriptions (combined)' });
    expect(within(combined).getByText(/could not be read with this sign-in/)).toBeInTheDocument();
    expect(within(combined).getByText('Metformin HCl 500 mg')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Prescriptions' })).toBeNull();

    const careTeamCard = await screen.findByRole('region', { name: 'Care Team' });
    await userEvent.click(within(careTeamCard).getByRole('button', { name: 'Care Team' }));
    expect(await within(careTeamCard).findByText('Synthetic, Gale')).toBeInTheDocument();
    expect(await within(careTeamCard).findByText('Synthetic Clinic')).toBeInTheDocument();

    expect(seenAuth.length).toBeGreaterThan(0);
    expect(new Set(seenAuth)).toEqual(new Set(['Bearer synthetic-access-token']));
    // The token lives in memory only.
    expect(JSON.stringify({ ...window.sessionStorage, ...window.localStorage })).not.toContain('synthetic-access-token');
  });

  it('asks for a relaunch when OpenEMR answers 401', async () => {
    server.use(
      config,
      token,
      http.get('*/apis/default/fhir/*', () => HttpResponse.json({ message: 'Unauthorized' }, { status: 401 })),
    );
    seedPending();
    renderAt('/?code=code-1&state=state-1');
    expect(await screen.findByRole('alert')).toHaveTextContent('Your session has expired. Open the dashboard again from the patient\'s chart in OpenEMR.');
    expect(screen.queryByRole('heading', { level: 1 })).toBeNull();
  });

  it('shows a readable message for an OAuth error on the callback', async () => {
    server.use(config);
    renderAt('/?error=access_denied&state=s');
    expect(await screen.findByRole('alert')).toHaveTextContent('Sign-in did not complete: access was denied.');
  });

  it('refuses a callback whose state does not match', async () => {
    server.use(config);
    seedPending();
    renderAt('/?code=code-1&state=forged');
    expect(await screen.findByRole('alert')).toHaveTextContent('Sign-in did not complete: the sign-in link expired or was already used.');
  });

  it('signing out drops the token and says how to come back', async () => {
    const seenAuth: string[] = [];
    server.use(config, token, ...fhirApi(seenAuth));
    seedPending();
    renderAt('/?code=code-1&state=state-1');
    await userEvent.click(await screen.findByRole('button', { name: 'Sign out' }));
    expect(await screen.findByText(/You have signed out/)).toBeInTheDocument();
    expect(screen.queryByRole('heading', { level: 1 })).toBeNull();
  });
});
