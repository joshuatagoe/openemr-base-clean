// Configuration from environment variables. Values are validated here so the
// server refuses to start misconfigured. Error messages name the variable,
// never its value (the secrets must not reach logs).

/**
 * Scopes requested at /authorize. Read-only (`.rs`) clinician scopes verified
 * in Phase B for a confidential client, plus the standard-API medication scope.
 * `profile` is not requested: OpenEMR drops it at /token even when the client
 * registered it (checked on the dev stack), so the id_token never has `name`.
 * No offline_access: no refresh token is ever issued, so a session ends when
 * the 1 h access token does.
 */
export const DEFAULT_SCOPES = [
  'openid',
  'fhirUser',
  'api:oemr',
  'api:fhir',
  'user/Patient.rs',
  'user/AllergyIntolerance.rs',
  'user/Condition.rs',
  'user/MedicationRequest.rs',
  'user/CareTeam.rs',
  'user/Observation.rs',
  'user/Practitioner.rs',
  'user/Organization.rs',
  'user/medication.rs',
  'user/patient.rs',
].join(' ');

export interface Config {
  openemrBaseUrl: string;
  fhirBaseUrl: string;
  stdApiBaseUrl: string;
  issuer: string;
  authorizeUrl: string;
  tokenUrl: string;
  clientId: string;
  clientSecret: string;
  redirectUri: string;
  callbackPath: string;
  scopes: string;
  sessionSecret: string;
  cookieSecure: boolean;
  host: string;
  port: number;
  upstreamTimeoutMs: number;
  logLevel: string;
  webDistDir: string | undefined;
}

type Env = Record<string, string | undefined>;

function required(env: Env, key: string): string {
  const v = env[key]?.trim();
  if (!v) throw new Error(`Missing required environment variable ${key}`);
  return v;
}

function isLoopback(hostname: string): boolean {
  return hostname === 'localhost' || hostname === '127.0.0.1' || hostname === '[::1]';
}

function parseUrl(key: string, value: string, { httpsUnlessLoopback }: { httpsUnlessLoopback: boolean }): URL {
  let u: URL;
  try {
    u = new URL(value);
  } catch {
    throw new Error(`${key} is not a valid URL`);
  }
  if (u.protocol !== 'https:' && u.protocol !== 'http:') throw new Error(`${key} must be an http(s) URL`);
  if (httpsUnlessLoopback && u.protocol !== 'https:' && !isLoopback(u.hostname)) {
    throw new Error(`${key} must use https (plain http is allowed only for localhost)`);
  }
  if (u.search || u.hash) throw new Error(`${key} must not have a query string or fragment`);
  return u;
}

function trimSlash(s: string): string {
  return s.replace(/\/+$/, '');
}

function parseBool(key: string, value: string | undefined, fallback: boolean): boolean {
  if (value === undefined || value.trim() === '') return fallback;
  const v = value.trim().toLowerCase();
  if (v === 'true' || v === '1') return true;
  if (v === 'false' || v === '0') return false;
  throw new Error(`${key} must be true or false`);
}

function parseIntIn(key: string, value: string | undefined, fallback: number, min: number, max: number): number {
  if (value === undefined || value.trim() === '') return fallback;
  const n = Number(value);
  if (!Number.isInteger(n) || n < min || n > max) throw new Error(`${key} must be an integer between ${min} and ${max}`);
  return n;
}

export function loadConfig(env: Env): Config {
  const base = trimSlash(parseUrl('OPENEMR_BASE_URL', required(env, 'OPENEMR_BASE_URL'), { httpsUnlessLoopback: true }).toString());
  const issuer = trimSlash(parseUrl('OAUTH_ISSUER', required(env, 'OAUTH_ISSUER'), { httpsUnlessLoopback: true }).toString());
  const clientId = required(env, 'CLIENT_ID');
  const clientSecret = required(env, 'CLIENT_SECRET');
  const redirect = parseUrl('REDIRECT_URI', required(env, 'REDIRECT_URI'), { httpsUnlessLoopback: true });
  const sessionSecret = required(env, 'SESSION_SECRET');
  if (sessionSecret.length < 32) throw new Error('SESSION_SECRET must be at least 32 characters');

  const scopes = (env.OAUTH_SCOPES?.trim() || DEFAULT_SCOPES).split(/\s+/).join(' ');
  const scopeList = scopes.split(' ');
  if (scopeList.includes('offline_access')) throw new Error('OAUTH_SCOPES must not include offline_access');
  if (!scopeList.includes('openid')) throw new Error('OAUTH_SCOPES must include openid');
  if (scopeList.some((s) => /^(user|patient|system)\/.*\.(c|u|d|write|\*)/.test(s) || s.startsWith('system/'))) {
    throw new Error('OAUTH_SCOPES must contain read-only user scopes only');
  }

  const site = env.OPENEMR_SITE?.trim() || 'default';
  if (!/^[A-Za-z0-9_-]+$/.test(site)) throw new Error('OPENEMR_SITE is invalid');

  return {
    openemrBaseUrl: base,
    fhirBaseUrl: `${base}/apis/${site}/fhir`,
    stdApiBaseUrl: `${base}/apis/${site}/api`,
    issuer,
    authorizeUrl: `${issuer}/authorize`,
    tokenUrl: `${issuer}/token`,
    clientId,
    clientSecret,
    redirectUri: redirect.toString(),
    callbackPath: redirect.pathname,
    scopes,
    sessionSecret,
    cookieSecure: parseBool('COOKIE_SECURE', env.COOKIE_SECURE, true),
    host: env.HOST?.trim() || '127.0.0.1',
    port: parseIntIn('PORT', env.PORT, 3000, 1, 65535),
    upstreamTimeoutMs: parseIntIn('UPSTREAM_TIMEOUT_MS', env.UPSTREAM_TIMEOUT_MS, 10000, 100, 120000),
    logLevel: env.LOG_LEVEL?.trim() || 'info',
    webDistDir: env.WEB_DIST_DIR?.trim() || undefined,
  };
}
