import { Writable } from 'node:stream';
import type { FastifyInstance, LightMyRequestResponse } from 'fastify';
import { buildApp } from '../../src/app.js';
import { loadConfig, type Config } from '../../src/config.js';
import { CLIENT_ID, CLIENT_SECRET, startMockOpenEmr, type MockOpenEmr } from './mockOpenEmr.js';

export const SESSION_SECRET = 'test-session-secret-0123456789-abcdefghij';
export const REDIRECT_URI = 'http://localhost:5173/auth/callback';

export interface Harness {
  app: FastifyInstance;
  mock: MockOpenEmr;
  config: Config;
  logs: string[];
  clock: { now: number };
  close(): Promise<void>;
}

export async function startHarness(env: Record<string, string> = {}): Promise<Harness> {
  const mock = await startMockOpenEmr();
  const config = loadConfig({
    OPENEMR_BASE_URL: mock.baseUrl,
    OAUTH_ISSUER: mock.issuer,
    CLIENT_ID,
    CLIENT_SECRET,
    REDIRECT_URI,
    SESSION_SECRET,
    COOKIE_SECURE: 'true',
    UPSTREAM_TIMEOUT_MS: '500',
    ...env,
  });
  const logs: string[] = [];
  const logStream = new Writable({
    write(chunk: Buffer, _enc, cb) {
      logs.push(chunk.toString('utf8'));
      cb();
    },
  });
  const clock = { now: Date.now() };
  const app = await buildApp({ config, logStream, clock: () => clock.now });
  await app.ready();
  return {
    app,
    mock,
    config,
    logs,
    clock,
    async close() {
      await app.close();
      await mock.close();
    },
  };
}

export interface ParsedCookie {
  name: string;
  value: string;
  attributes: Record<string, string | true>;
}

export function parseSetCookies(res: LightMyRequestResponse): ParsedCookie[] {
  const raw = res.headers['set-cookie'];
  const list = raw === undefined ? [] : Array.isArray(raw) ? raw : [raw];
  return list.map((line) => {
    const [pair, ...attrs] = line.split(';').map((s) => s.trim());
    const eq = (pair ?? '').indexOf('=');
    const attributes: Record<string, string | true> = {};
    for (const a of attrs) {
      const i = a.indexOf('=');
      if (i === -1) attributes[a.toLowerCase()] = true;
      else attributes[a.slice(0, i).toLowerCase()] = a.slice(i + 1);
    }
    return { name: (pair ?? '').slice(0, eq), value: (pair ?? '').slice(eq + 1), attributes };
  });
}

export function cookieHeader(cookies: ParsedCookie[]): string {
  return cookies.map((c) => `${c.name}=${c.value}`).join('; ');
}

export interface LoginStart {
  loginCookie: ParsedCookie;
  authorizeUrl: URL;
  state: string;
  nonce: string;
  challenge: string;
}

export async function startLogin(h: Harness): Promise<LoginStart> {
  const res = await h.app.inject({ method: 'GET', url: '/auth/login' });
  if (res.statusCode !== 302) throw new Error(`login returned ${res.statusCode}`);
  const authorizeUrl = new URL(String(res.headers.location));
  const loginCookie = parseSetCookies(res)[0];
  if (!loginCookie) throw new Error('no login cookie');
  const p = authorizeUrl.searchParams;
  return {
    loginCookie,
    authorizeUrl,
    state: p.get('state') ?? '',
    nonce: p.get('nonce') ?? '',
    challenge: p.get('code_challenge') ?? '',
  };
}

/** Runs the whole login and returns the Cookie header for the new session. */
export async function login(h: Harness, claims?: Record<string, unknown>): Promise<string> {
  const start = await startLogin(h);
  const code = h.mock.issueCode({ challenge: start.challenge, nonce: start.nonce, redirectUri: REDIRECT_URI, ...(claims ? { claims } : {}) });
  const res = await h.app.inject({
    method: 'GET',
    url: `/auth/callback?code=${encodeURIComponent(code)}&state=${encodeURIComponent(start.state)}`,
    headers: { cookie: cookieHeader([start.loginCookie]) },
  });
  if (res.statusCode !== 302 || res.headers.location !== '/') {
    throw new Error(`callback returned ${res.statusCode} -> ${String(res.headers.location)}`);
  }
  const session = parseSetCookies(res).find((c) => c.name.endsWith('dash_sid') && c.value !== '');
  if (!session) throw new Error('no session cookie');
  return cookieHeader([session]);
}
