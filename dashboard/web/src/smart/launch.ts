// SMART on FHIR EHR launch for modes B/C, as a public client (no secret):
// iss + launch -> .well-known/smart-configuration -> authorize (code + PKCE
// S256, launch, aud) -> callback -> token. OpenEMR serves this app, so iss and
// both endpoints must be on the page's own origin; anything else is refused.
//
// Only the PKCE verifier, state and the two URLs survive the redirect, in
// sessionStorage, and they are removed on return. The access token is returned
// to the caller and kept in memory only (a reload means launching again).
import { challengeS256, randomToken } from './pkce';
import scopes from './scopes.json';

/**
 * Read-only, patient-context scopes, each resource listed (OpenEMR rejects
 * wildcards). No launch/patient (that would add OpenEMR's picker), no user/
 * (refused for public clients), no offline_access (no refresh token).
 * Practitioner/Organization resolve care-team names in patient context.
 * scopes.json is shared with the client registration script, so the client is
 * registered with exactly what the app asks for.
 */
export const SMART_SCOPES = (scopes as string[]).join(' ');

export const PENDING_KEY = 'dashboard.smart.pending';
const PENDING_MAX_AGE_MS = 10 * 60_000;

export type SmartLaunchErrorCode =
  | 'bad_issuer'
  | 'bad_configuration'
  | 'discovery_failed'
  | 'state_mismatch'
  | 'token_failed'
  | 'no_patient'
  | 'not_configured';

export class SmartLaunchError extends Error {
  readonly code: SmartLaunchErrorCode;
  constructor(code: SmartLaunchErrorCode, message: string) {
    super(message);
    this.name = 'SmartLaunchError';
    this.code = code;
  }
}

const OAUTH_ERRORS = new Set([
  'access_denied',
  'invalid_request',
  'invalid_scope',
  'invalid_client',
  'unauthorized_client',
  'unsupported_response_type',
  'server_error',
  'temporarily_unavailable',
  'login_required',
  'consent_required',
  'interaction_required',
]);

export type LaunchParams =
  | { kind: 'launch'; launch: string; iss: string }
  | { kind: 'callback'; code: string; state: string }
  | { kind: 'error'; error: string }
  | { kind: 'none' };

export function parseLaunchParams(search: string): LaunchParams {
  const q = new URLSearchParams(search);
  const error = q.get('error');
  if (error !== null) return { kind: 'error', error: OAUTH_ERRORS.has(error) ? error : 'unknown' };
  const code = q.get('code');
  const state = q.get('state');
  if (code && state) return { kind: 'callback', code, state };
  const launch = q.get('launch');
  const iss = q.get('iss');
  if (launch && iss) return { kind: 'launch', launch, iss };
  return { kind: 'none' };
}

const FHIR_BASE_PATH = /^(?:\/[A-Za-z0-9._~%-]+)*\/apis\/[A-Za-z0-9_-]+\/fhir\/?$/;

/** The FHIR base from `iss`, only when it is on this page's origin. Returned without a trailing slash. */
export function validateIss(iss: string, pageOrigin: string): string {
  let u: URL;
  try {
    u = new URL(iss);
  } catch {
    throw new SmartLaunchError('bad_issuer', 'iss is not a URL');
  }
  if ((u.protocol !== 'https:' && u.protocol !== 'http:') || u.origin !== pageOrigin || u.search !== '' || u.hash !== '' || u.username || u.password) {
    throw new SmartLaunchError('bad_issuer', 'iss is not on this origin');
  }
  if (!FHIR_BASE_PATH.test(u.pathname)) throw new SmartLaunchError('bad_issuer', 'iss is not a FHIR base');
  return `${u.origin}${u.pathname.replace(/\/$/, '')}`;
}

function sameOriginUrl(value: unknown, origin: string): string | undefined {
  if (typeof value !== 'string') return undefined;
  try {
    const u = new URL(value);
    return u.origin === origin && (u.protocol === 'https:' || u.protocol === 'http:') ? u.toString() : undefined;
  } catch {
    return undefined;
  }
}

interface Endpoints {
  authorizationEndpoint: string;
  tokenEndpoint: string;
}

async function discover(fhirBaseUrl: string, origin: string): Promise<Endpoints> {
  let body: Record<string, unknown>;
  try {
    const res = await fetch(`${fhirBaseUrl}/.well-known/smart-configuration`, { credentials: 'omit', headers: { accept: 'application/json' } });
    if (!res.ok) throw new Error(String(res.status));
    body = (await res.json()) as Record<string, unknown>;
  } catch {
    throw new SmartLaunchError('discovery_failed', 'The SMART configuration could not be read');
  }
  const authorizationEndpoint = sameOriginUrl(body.authorization_endpoint, origin);
  const tokenEndpoint = sameOriginUrl(body.token_endpoint, origin);
  const methods = body.code_challenge_methods_supported;
  const s256 = Array.isArray(methods) && methods.includes('S256');
  if (!authorizationEndpoint || !tokenEndpoint || !s256) {
    throw new SmartLaunchError('bad_configuration', 'The SMART configuration is not usable');
  }
  return { authorizationEndpoint, tokenEndpoint };
}

interface Pending {
  state: string;
  verifier: string;
  fhirBaseUrl: string;
  tokenEndpoint: string;
  createdAt: number;
}

export interface BeginLaunchInput {
  iss: string;
  launch: string;
  clientId: string;
  redirectUri: string;
}

/** Returns the authorize URL to navigate to; stores the PKCE verifier and state for the callback. */
export async function beginLaunch(input: BeginLaunchInput): Promise<string> {
  const origin = window.location.origin;
  const fhirBaseUrl = validateIss(input.iss, origin);
  const { authorizationEndpoint, tokenEndpoint } = await discover(fhirBaseUrl, origin);
  const verifier = randomToken();
  const state = randomToken();
  const pending: Pending = { state, verifier, fhirBaseUrl, tokenEndpoint, createdAt: Date.now() };
  window.sessionStorage.setItem(PENDING_KEY, JSON.stringify(pending));

  const url = new URL(authorizationEndpoint);
  url.search = new URLSearchParams({
    response_type: 'code',
    client_id: input.clientId,
    redirect_uri: input.redirectUri,
    scope: SMART_SCOPES,
    state,
    aud: fhirBaseUrl,
    launch: input.launch,
    code_challenge: await challengeS256(verifier),
    code_challenge_method: 'S256',
  }).toString();
  return url.toString();
}

/** Reads and removes the pending launch: it is single use whatever happens next. */
function takePending(state: string): Pending {
  const raw = window.sessionStorage.getItem(PENDING_KEY);
  window.sessionStorage.removeItem(PENDING_KEY);
  const expired = new SmartLaunchError('state_mismatch', 'No matching launch is pending');
  if (!raw) throw expired;
  let p: Partial<Pending>;
  try {
    p = JSON.parse(raw) as Partial<Pending>;
  } catch {
    throw expired;
  }
  const origin = window.location.origin;
  if (
    typeof p.state !== 'string' ||
    p.state !== state ||
    typeof p.verifier !== 'string' ||
    typeof p.createdAt !== 'number' ||
    Date.now() - p.createdAt > PENDING_MAX_AGE_MS ||
    typeof p.fhirBaseUrl !== 'string' ||
    !sameOriginUrl(p.tokenEndpoint, origin)
  ) {
    throw expired;
  }
  let fhirBaseUrl: string;
  try {
    fhirBaseUrl = validateIss(p.fhirBaseUrl, origin);
  } catch {
    throw expired;
  }
  return { state: p.state, verifier: p.verifier, fhirBaseUrl, tokenEndpoint: String(p.tokenEndpoint), createdAt: p.createdAt };
}

export interface SmartSession {
  accessToken: string;
  /** FHIR id of the launched patient (token response `patient`). */
  patientId: string;
  fhirBaseUrl: string;
  /** Epoch ms. */
  expiresAt: number;
  /** The signed-in user's own resource, when it is a Practitioner. */
  fhirUser?: { type: 'Practitioner'; id: string } | undefined;
}

const FHIR_ID = /^[A-Za-z0-9.-]{1,64}$/;

/**
 * The id_token comes straight from the token endpoint over TLS in this code
 * flow, so its claims are read without checking the signature (OIDC Core
 * 3.1.3.7). Only `fhirUser` is used, for the "Signed in as" name.
 */
function fhirUserFrom(idToken: unknown): SmartSession['fhirUser'] {
  if (typeof idToken !== 'string') return undefined;
  try {
    const payload = idToken.split('.')[1] ?? '';
    const json = atob(payload.replace(/-/g, '+').replace(/_/g, '/'));
    const claims = JSON.parse(json) as { fhirUser?: unknown };
    const m = typeof claims.fhirUser === 'string' ? /(?:^|\/)Practitioner\/([A-Za-z0-9.-]{1,64})$/.exec(claims.fhirUser) : null;
    return m?.[1] ? { type: 'Practitioner', id: m[1] } : undefined;
  } catch {
    return undefined;
  }
}

export interface CompleteLaunchInput {
  code: string;
  state: string;
  clientId: string;
  redirectUri: string;
}

export async function completeLaunch(input: CompleteLaunchInput): Promise<SmartSession> {
  const pending = takePending(input.state);
  let body: Record<string, unknown>;
  try {
    const res = await fetch(pending.tokenEndpoint, {
      method: 'POST',
      credentials: 'omit',
      headers: { 'content-type': 'application/x-www-form-urlencoded', accept: 'application/json' },
      body: new URLSearchParams({
        grant_type: 'authorization_code',
        code: input.code,
        redirect_uri: input.redirectUri,
        client_id: input.clientId,
        code_verifier: pending.verifier,
      }).toString(),
    });
    if (!res.ok) throw new Error(String(res.status));
    body = (await res.json()) as Record<string, unknown>;
  } catch {
    throw new SmartLaunchError('token_failed', 'OpenEMR did not issue a token');
  }
  if (typeof body.access_token !== 'string' || body.access_token === '' || String(body.token_type).toLowerCase() !== 'bearer') {
    throw new SmartLaunchError('token_failed', 'OpenEMR did not issue a bearer token');
  }
  if (typeof body.patient !== 'string' || !FHIR_ID.test(body.patient)) {
    throw new SmartLaunchError('no_patient', 'The token has no patient context');
  }
  const expiresIn = typeof body.expires_in === 'number' && body.expires_in > 0 ? body.expires_in : 3600;
  return {
    accessToken: body.access_token,
    patientId: body.patient,
    fhirBaseUrl: pending.fhirBaseUrl,
    expiresAt: Date.now() + expiresIn * 1000,
    fhirUser: fhirUserFrom(body.id_token),
  };
}
