// Modes B/C: the build served by OpenEMR under our module's public folder and
// started by an EHR launch (the module's menu entry, or OpenEMR's SMART apps
// card). One page, no router: the launch, the callback and the dashboard are
// all this URL. The patient is the launch context; switching patients means
// changing the chart in OpenEMR and launching again.
import { QueryClientProvider, useQuery, type QueryClient } from '@tanstack/react-query';
import type { Practitioner } from 'fhir/r4';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { smartConfigUrl } from '../config';
import { DataSourceContext, useDataSource } from '../data/DataSourceContext';
import { createQueryClient } from '../data/queryClient';
import { SmartDataSource } from '../data/SmartDataSource';
import { beginLaunch, completeLaunch, parseLaunchParams, SmartLaunchError } from './launch';
import { Spinner } from '../components/Card';
import { SmartPatientView } from './SmartPatientView';

type Phase =
  | { s: 'starting' }
  | { s: 'redirecting' }
  | { s: 'needs_launch' }
  /** `ds` holds the token in a closure; `signOut` drops it. The token is in no React state. */
  | { s: 'ready'; patientId: string; practitionerId: string | undefined; ds: SmartDataSource; signOut: () => void }
  | { s: 'expired' }
  | { s: 'signed_out' }
  | { s: 'error'; message: string };

const RELAUNCH = "Open the dashboard again from the patient's chart in OpenEMR.";

const ASK_ADMIN = 'Ask your OpenEMR administrator';

// Each reason says what happened; the sentence after it says what to do: a
// relaunch when trying again can help, the administrator when it can't.
const OAUTH_REASONS: Readonly<Record<string, { reason: string; next: string }>> = {
  access_denied: { reason: 'access was denied', next: RELAUNCH },
  invalid_scope: { reason: "OpenEMR didn't accept the requested access", next: `${ASK_ADMIN} to check the dashboard's API client.` },
  invalid_client: { reason: "the dashboard isn't registered or enabled in OpenEMR", next: `${ASK_ADMIN} to enable the dashboard's API client.` },
  unauthorized_client: { reason: "the dashboard isn't registered or enabled in OpenEMR", next: `${ASK_ADMIN} to enable the dashboard's API client.` },
  login_required: { reason: "you're not signed in to OpenEMR", next: `Sign in to OpenEMR, then ${RELAUNCH.charAt(0).toLowerCase()}${RELAUNCH.slice(1)}` },
};

function describeOAuthError(code: string): string {
  const known = Object.hasOwn(OAUTH_REASONS, code) ? OAUTH_REASONS[code] : undefined;
  return known ? `Sign-in didn't complete: ${known.reason}. ${known.next}` : `Sign-in didn't complete: an unexpected error occurred. ${RELAUNCH}`;
}

function describeLaunchError(e: unknown): string {
  if (!(e instanceof SmartLaunchError)) return `Sign-in didn't complete: an unexpected error occurred. ${RELAUNCH}`;
  switch (e.code) {
    case 'bad_issuer':
      return `The launch didn't come from this OpenEMR server. ${RELAUNCH}`;
    case 'not_configured':
      return `The dashboard isn't set up yet (no SMART client ID). ${ASK_ADMIN}.`;
    case 'bad_configuration':
    case 'discovery_failed':
      return `Couldn't use OpenEMR's SMART configuration, so sign-in didn't start. ${ASK_ADMIN} to check the SMART on FHIR settings.`;
    case 'state_mismatch':
      return `Sign-in didn't complete: the sign-in link expired or was already used. ${RELAUNCH}`;
    case 'token_failed':
      return `Sign-in didn't complete: OpenEMR didn't issue a token. ${ASK_ADMIN} to check that the dashboard's API client is enabled.`;
    case 'no_patient':
      return "OpenEMR didn't say which patient to show. Open the dashboard from a patient's chart.";
  }
}

async function loadClientId(): Promise<string> {
  const notConfigured = new SmartLaunchError('not_configured', 'No SMART client id');
  let body: unknown;
  try {
    const res = await fetch(smartConfigUrl(import.meta.env.BASE_URL, window.location.origin), { credentials: 'omit', cache: 'no-store' });
    if (!res.ok) throw notConfigured;
    body = await res.json();
  } catch {
    throw notConfigured;
  }
  const id = body && typeof body === 'object' && 'clientId' in body ? body.clientId : undefined;
  if (typeof id !== 'string' || !/^[A-Za-z0-9_-]{8,128}$/.test(id)) throw notConfigured;
  return id;
}

/** Removes code/state (or launch/iss) from the address bar without a reload. */
function cleanUrl() {
  window.history.replaceState(null, '', window.location.pathname);
}

export interface SmartAppProps {
  /** Full-page navigation to OpenEMR's authorize endpoint (tests inject a spy). */
  navigate?: (url: string) => void;
  queryClient?: QueryClient | undefined;
}

export function SmartApp({ navigate = (url) => window.location.assign(url), queryClient }: SmartAppProps) {
  const [qc] = useState(() => queryClient ?? createQueryClient());
  const [phase, setPhase] = useState<Phase>({ s: 'starting' });
  const started = useRef(false);
  const redirectUri = `${window.location.origin}${import.meta.env.BASE_URL}`;

  useEffect(() => {
    // Once per page: the pending launch is single use (StrictMode runs effects twice).
    if (started.current) return;
    started.current = true;
    const params = parseLaunchParams(window.location.search);
    void (async () => {
      try {
        if (params.kind === 'error') {
          cleanUrl();
          setPhase({ s: 'error', message: describeOAuthError(params.error) });
          return;
        }
        if (params.kind === 'none') {
          setPhase({ s: 'needs_launch' });
          return;
        }
        const clientId = await loadClientId();
        if (params.kind === 'launch') {
          const url = await beginLaunch({ iss: params.iss, launch: params.launch, clientId, redirectUri });
          setPhase({ s: 'redirecting' });
          navigate(url);
          return;
        }
        const session = await completeLaunch({ code: params.code, state: params.state, clientId, redirectUri });
        cleanUrl();
        // The access token lives only in this closure, in memory, for the life of the page.
        let token: string | undefined = session.accessToken;
        const end = (next: Phase) => {
          token = undefined;
          qc.clear();
          setPhase(next);
        };
        const ds = new SmartDataSource({
          fhirBaseUrl: session.fhirBaseUrl,
          getAccessToken: () => token,
          onSessionExpired: () => end({ s: 'expired' }),
        });
        setPhase({ s: 'ready', patientId: session.patientId, practitionerId: session.fhirUser?.id, ds, signOut: () => end({ s: 'signed_out' }) });
      } catch (e) {
        cleanUrl();
        setPhase({ s: 'error', message: describeLaunchError(e) });
      }
    })();
  }, [navigate, redirectUri, qc]);

  return (
    <QueryClientProvider client={qc}>
      {phase.s === 'ready' ? (
        <DataSourceContext.Provider value={phase.ds}>
          {/* OpenEMR's tab bar is already above the app: no title row (plan L5). */}
          <main className="app-main">
            <SmartPatientView
              patientId={phase.patientId}
              account={
                <div className="bar-account">
                  <SignedInAs practitionerId={phase.practitionerId} />
                  <button type="button" className="btn btn-secondary" onClick={phase.signOut}>
                    Sign out
                  </button>
                </div>
              }
            />
          </main>
        </DataSourceContext.Provider>
      ) : (
        <>
          <AppBanner />
          <main className="app-main">
            <PhaseView phase={phase} />
          </main>
        </>
      )}
    </QueryClientProvider>
  );
}

function AppBanner({ children }: { children?: ReactNode }) {
  return (
    <header className="app-header" role="banner">
      <span className="app-title">Patient Dashboard</span>
      <div className="app-auth">{children}</div>
    </header>
  );
}

/** "Signed in as": the user's own Practitioner, readable with patient/Practitioner.rs. */
function SignedInAs({ practitionerId }: { practitionerId: string | undefined }) {
  const ds = useDataSource();
  const q = useQuery({
    queryKey: ['me', practitionerId],
    enabled: !!practitionerId,
    staleTime: Infinity,
    queryFn: async ({ signal }) => {
      const p = await ds.read<Practitioner>('Practitioner', practitionerId ?? '', signal);
      const n = (p.name ?? []).find((x) => x.use === 'official') ?? p.name?.[0];
      return [n?.given?.[0], n?.family].filter(Boolean).join(' ');
    },
  });
  if (practitionerId && q.isPending) return null;
  return <span className="app-user">Signed in as {q.data || 'OpenEMR user'}</span>;
}

function PhaseView({ phase }: { phase: Phase }) {
  switch (phase.s) {
    case 'starting':
    case 'redirecting':
      return (
        <p className="muted" role="status">
          <Spinner />
          Signing in with OpenEMR…
        </p>
      );
    case 'needs_launch':
      return (
        <p className="notice notice-info">
          Open this dashboard from a patient's chart in OpenEMR: Patient menu, "Patient Dashboard (React)", or the SMART Enabled Apps card on the
          patient's dashboard.
        </p>
      );
    case 'expired':
      return (
        <p className="notice notice-warning" role="alert">
          Your session expired. {RELAUNCH}
        </p>
      );
    case 'signed_out':
      return (
        <p className="notice notice-info" role="status">
          You have signed out. {RELAUNCH}
        </p>
      );
    case 'error':
      return (
        <p className="notice notice-warning" role="alert">
          {phase.message}
        </p>
      );
    case 'ready':
      return null;
  }
}
