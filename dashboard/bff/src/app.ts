// The backend-for-frontend (BFF): serves the React build, runs the OAuth login
// with OpenEMR as a confidential client, keeps tokens server-side, and proxies
// an allow-list of read-only GET routes.
//
// Why a BFF at all (Phase B, measured on this OpenEMR build): a public browser
// client cannot be granted clinician `user/` scopes, and every CORS preflight
// to /apis/* returns 404, so a browser on another origin cannot call the API.
import { randomUUID } from 'node:crypto';
import { existsSync } from 'node:fs';
import type { Writable } from 'node:stream';
import fastifyCookie from '@fastify/cookie';
import fastifyStatic from '@fastify/static';
import Fastify, { LogController, type FastifyInstance, type FastifyReply, type FastifyRequest } from 'fastify';
import { matchFhirRequest, matchStdMedicationRequest, type MatchResult } from './allowlist.js';
import type { Config } from './config.js';
import { codeChallengeS256, randomToken, safeEqual } from './pkce.js';
import { ExpiringStore, type PendingLogin, type Session } from './store.js';

export interface BuildOptions {
  config: Config;
  clock?: () => number;
  logStream?: Writable;
}

declare module 'fastify' {
  interface FastifyRequest {
    logResource: string | undefined;
  }
}

const LOGIN_TTL_SECONDS = 600;
const TOKEN_EXPIRY_SKEW_SECONDS = 30;
const MAX_UPSTREAM_BYTES = 5 * 1024 * 1024;
const OAUTH_ERRORS = new Set(['access_denied', 'invalid_scope', 'invalid_request', 'unauthorized_client', 'server_error', 'temporarily_unavailable']);

const CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self'",
  "img-src 'self' data:",
  "font-src 'self'",
  "connect-src 'self'",
  "frame-ancestors 'none'",
  "base-uri 'none'",
  "form-action 'self'",
  "object-src 'none'",
].join('; ');

type SessionLookup = { kind: 'none' } | { kind: 'invalid' } | { kind: 'ok'; id: string; session: Session };

function decodeJwtClaims(jwt: string): Record<string, unknown> | undefined {
  const parts = jwt.split('.');
  if (parts.length !== 3 || !parts[1]) return undefined;
  try {
    const claims: unknown = JSON.parse(Buffer.from(parts[1], 'base64url').toString('utf8'));
    return claims && typeof claims === 'object' && !Array.isArray(claims) ? (claims as Record<string, unknown>) : undefined;
  } catch {
    return undefined;
  }
}

function isJson(contentType: string | null): boolean {
  return contentType !== null && /^application\/(fhir\+)?json\b/i.test(contentType);
}

export async function buildApp(opts: BuildOptions): Promise<FastifyInstance> {
  const { config } = opts;
  const clock = opts.clock ?? Date.now;
  const reserved = ['/auth/login', '/auth/me', '/auth/logout', '/healthz'];
  if (reserved.includes(config.callbackPath) || config.callbackPath.startsWith('/api/')) {
    throw new Error('REDIRECT_URI path collides with a BFF route');
  }

  const cookiePrefix = config.cookieSecure ? '__Host-' : '';
  const SID_COOKIE = `${cookiePrefix}dash_sid`;
  const LOGIN_COOKIE = `${cookiePrefix}dash_login`;
  const baseCookie = { path: '/', httpOnly: true, secure: config.cookieSecure, sameSite: 'lax' as const, signed: true };

  const pendingLogins = new ExpiringStore<PendingLogin>(clock, 1000);
  const sessions = new ExpiringStore<Session>(clock, 10000);

  const app = Fastify({
    logger: {
      level: config.logLevel,
      ...(opts.logStream ? { stream: opts.logStream } : {}),
      // Never serialise URLs (query strings carry names / birth dates), headers or bodies.
      serializers: {
        req: (req: { method?: string; id?: string }) => ({ method: req.method, id: req.id }),
        res: (res: { statusCode?: number }) => ({ statusCode: res.statusCode }),
      },
      redact: { paths: ['req.headers', 'headers', 'authorization', 'cookie', 'access_token', 'id_token', 'client_secret'], censor: '[redacted]' },
    },
    // Our own onResponse hook logs one line per request (route pattern only).
    logController: new LogController({ disableRequestLogging: true }),
    genReqId: () => randomUUID(),
    requestIdHeader: false,
    exposeHeadRoutes: false,
    bodyLimit: 1024,
    routerOptions: { ignoreTrailingSlash: false },
  });

  app.decorateRequest('logResource', undefined);
  await app.register(fastifyCookie, { secret: config.sessionSecret });

  // ---- cross-cutting hooks --------------------------------------------------
  app.addHook('onSend', async (request, reply, payload) => {
    reply.header('x-request-id', request.id);
    reply.header('content-security-policy', CSP);
    reply.header('x-content-type-options', 'nosniff');
    reply.header('referrer-policy', 'no-referrer');
    reply.header('x-frame-options', 'DENY');
    reply.header('cross-origin-opener-policy', 'same-origin');
    reply.header('cross-origin-resource-policy', 'same-origin');
    reply.header('permissions-policy', 'camera=(), microphone=(), geolocation=()');
    if (config.cookieSecure) reply.header('strict-transport-security', 'max-age=31536000');
    const path = request.url.split('?', 1)[0] ?? '';
    if (path.startsWith('/api/') || path.startsWith('/auth/') || path === '/healthz' || path === config.callbackPath) {
      reply.header('cache-control', 'no-store');
      reply.header('pragma', 'no-cache');
    }
    return payload;
  });

  app.addHook('onResponse', async (request, reply) => {
    // Route pattern only: never the URL (query strings and ids stay out of logs).
    request.log.info(
      {
        method: request.method,
        route: request.routeOptions.url ?? 'unmatched',
        ...(request.logResource ? { resource: request.logResource } : {}),
        statusCode: reply.statusCode,
        ms: Math.round(reply.elapsedTime),
      },
      'request completed',
    );
  });

  app.setErrorHandler(async (error: { statusCode?: unknown; name?: unknown; code?: unknown }, request, reply) => {
    const status = typeof error.statusCode === 'number' && error.statusCode >= 400 && error.statusCode < 500 ? error.statusCode : 500;
    request.log.error({ errName: String(error.name), errCode: String(error.code), statusCode: status }, 'request failed');
    return reply.code(status).send({ error: status === 500 ? 'internal_error' : 'bad_request' });
  });

  // ---- session helpers -------------------------------------------------------
  function readSession(request: FastifyRequest): SessionLookup {
    const raw = request.cookies[SID_COOKIE];
    if (!raw) return { kind: 'none' };
    const unsigned = request.unsignCookie(raw);
    if (!unsigned.valid || !unsigned.value) return { kind: 'invalid' };
    const session = sessions.get(unsigned.value);
    return session ? { kind: 'ok', id: unsigned.value, session } : { kind: 'invalid' };
  }

  function clearCookie(reply: FastifyReply, name: string): void {
    reply.clearCookie(name, { path: '/', httpOnly: true, secure: config.cookieSecure, sameSite: 'lax' });
  }

  function sessionError(reply: FastifyReply, lookup: SessionLookup) {
    if (lookup.kind === 'invalid') {
      clearCookie(reply, SID_COOKIE);
      return reply.code(401).send({ error: 'session_expired' });
    }
    return reply.code(401).send({ error: 'unauthenticated' });
  }

  /**
   * OpenEMR's id_token carries no `name` claim (it drops `profile` at /token),
   * so for "Signed in as" the BFF reads the user's own fhirUser Practitioner
   * with the user's token. Only a Practitioner under our FHIR base is followed.
   * Users in the Physicians group get 403 there (Phase B T4): the caller then
   * shows a generic label. Never a system credential.
   */
  async function practitionerName(fhirUser: string | undefined, accessToken: string, request: FastifyRequest): Promise<string | undefined> {
    const prefix = `${config.fhirBaseUrl}/Practitioner/`;
    if (!fhirUser?.startsWith(prefix)) return undefined;
    const match = matchFhirRequest(fhirUser.slice(config.fhirBaseUrl.length + 1), new URLSearchParams());
    if (!match.ok) return undefined;
    try {
      const res = await fetch(`${config.fhirBaseUrl}/${match.upstreamPath}`, {
        headers: { authorization: `Bearer ${accessToken}`, accept: 'application/fhir+json' },
        redirect: 'manual',
        signal: AbortSignal.timeout(config.upstreamTimeoutMs),
      });
      if (res.status !== 200 || !isJson(res.headers.get('content-type'))) {
        await res.body?.cancel();
        request.log.info({ upstreamStatus: res.status }, 'practitioner name not available');
        return undefined;
      }
      const body = (await res.json()) as { name?: Array<{ text?: unknown; given?: unknown; family?: unknown }> };
      const n = body.name?.[0];
      if (!n) return undefined;
      if (typeof n.text === 'string' && n.text.trim()) return n.text.trim();
      const given = Array.isArray(n.given) ? n.given.filter((g): g is string => typeof g === 'string') : [];
      const full = [...given, typeof n.family === 'string' ? n.family : ''].join(' ').trim();
      return full || undefined;
    } catch {
      return undefined;
    }
  }

  // ---- auth routes -------------------------------------------------------------
  app.get('/healthz', async () => ({ status: 'ok' }));

  app.get('/auth/login', async (_request, reply) => {
    const loginId = randomToken();
    const pending: PendingLogin = {
      state: randomToken(),
      nonce: randomToken(),
      codeVerifier: randomToken(48),
      expiresAt: clock() + LOGIN_TTL_SECONDS * 1000,
    };
    pendingLogins.set(loginId, pending);
    const url = new URL(config.authorizeUrl);
    url.search = new URLSearchParams({
      response_type: 'code',
      client_id: config.clientId,
      redirect_uri: config.redirectUri,
      scope: config.scopes,
      state: pending.state,
      nonce: pending.nonce,
      code_challenge: codeChallengeS256(pending.codeVerifier),
      code_challenge_method: 'S256',
      aud: config.fhirBaseUrl,
    }).toString();
    reply.setCookie(LOGIN_COOKIE, loginId, { ...baseCookie, maxAge: LOGIN_TTL_SECONDS });
    return reply.redirect(url.toString(), 302);
  });

  app.get(config.callbackPath, async (request, reply) => {
    const query = new URL(request.url, 'http://bff.invalid').searchParams;
    const fail = (code: string) => {
      clearCookie(reply, LOGIN_COOKIE);
      request.log.warn({ authError: code }, 'login failed');
      return reply.redirect(`/?auth_error=${encodeURIComponent(code)}`, 302);
    };

    const rawLogin = request.cookies[LOGIN_COOKIE];
    const unsigned = rawLogin ? request.unsignCookie(rawLogin) : undefined;
    const pending = unsigned?.valid && unsigned.value ? pendingLogins.take(unsigned.value) : undefined;

    const oauthError = query.get('error');
    if (oauthError) return fail(OAUTH_ERRORS.has(oauthError) ? oauthError : 'authorization_failed');
    if (!pending) return fail('login_expired');
    if (!safeEqual(query.get('state') ?? '', pending.state)) return fail('state_mismatch');
    const code = query.get('code');
    if (!code) return fail('missing_code');

    // Token exchange. The client secret is ALWAYS sent: OpenEMR was observed to
    // accept a confidential client's code without it (Phase B T6), and the BFF
    // must not depend on that weakness.
    let tokenBody: Record<string, unknown>;
    try {
      const res = await fetch(config.tokenUrl, {
        method: 'POST',
        headers: { 'content-type': 'application/x-www-form-urlencoded', accept: 'application/json' },
        body: new URLSearchParams({
          grant_type: 'authorization_code',
          code,
          redirect_uri: config.redirectUri,
          client_id: config.clientId,
          client_secret: config.clientSecret,
          code_verifier: pending.codeVerifier,
        }),
        redirect: 'manual',
        signal: AbortSignal.timeout(config.upstreamTimeoutMs),
      });
      if (res.status !== 200) {
        request.log.warn({ upstreamStatus: res.status }, 'token endpoint refused the code');
        return fail('token_exchange_failed');
      }
      tokenBody = (await res.json()) as Record<string, unknown>;
    } catch (e) {
      request.log.warn({ errName: (e as Error).name }, 'token endpoint unreachable');
      return fail('token_exchange_failed');
    }

    const accessToken = tokenBody.access_token;
    const idToken = tokenBody.id_token;
    if (typeof accessToken !== 'string' || !accessToken || String(tokenBody.token_type).toLowerCase() !== 'bearer' || typeof idToken !== 'string') {
      return fail('token_exchange_failed');
    }

    // id_token checks (OIDC Core 3.1.3.7). It arrives directly from the token
    // endpoint over TLS, so per 3.1.3.7(6) the TLS server identity stands in
    // for a signature check; iss / aud / exp / nonce are verified here.
    const claims = decodeJwtClaims(idToken);
    const aud = claims?.aud;
    const audOk = aud === config.clientId || (Array.isArray(aud) && aud.includes(config.clientId));
    const exp = typeof claims?.exp === 'number' ? claims.exp : 0;
    if (!claims || claims.iss !== config.issuer || !audOk || exp * 1000 <= clock() || !safeEqual(String(claims.nonce ?? ''), pending.nonce)) {
      return fail('invalid_id_token');
    }

    const expiresIn = typeof tokenBody.expires_in === 'number' && tokenBody.expires_in > 0 && tokenBody.expires_in <= 86400 ? tokenBody.expires_in : 3600;
    const lifetime = Math.max(expiresIn - TOKEN_EXPIRY_SKEW_SECONDS, 1);
    const fhirUser = typeof claims.fhirUser === 'string' ? claims.fhirUser : undefined;
    const name =
      [claims.name, claims.preferred_username].find((v): v is string => typeof v === 'string' && v.trim() !== '') ??
      (await practitionerName(fhirUser, accessToken, request));

    // Session fixation: always a new id; drop any previous session.
    const previous = readSession(request);
    if (previous.kind === 'ok') sessions.delete(previous.id);
    const sid = randomToken();
    sessions.set(sid, {
      accessToken,
      displayName: (name ?? 'OpenEMR user').slice(0, 100),
      fhirUser,
      scope: typeof tokenBody.scope === 'string' ? tokenBody.scope : '',
      expiresAt: clock() + lifetime * 1000,
    });
    clearCookie(reply, LOGIN_COOKIE);
    reply.setCookie(SID_COOKIE, sid, { ...baseCookie, maxAge: lifetime });
    request.log.info('login succeeded');
    return reply.redirect('/', 302);
  });

  app.get('/auth/me', async (request, reply) => {
    const lookup = readSession(request);
    if (lookup.kind !== 'ok') return sessionError(reply, lookup);
    const { session } = lookup;
    return {
      authenticated: true,
      user: { displayName: session.displayName, fhirUser: session.fhirUser ?? null },
      expiresAt: new Date(session.expiresAt).toISOString(),
    };
  });

  app.post('/auth/logout', async (request, reply) => {
    const lookup = readSession(request);
    if (lookup.kind === 'ok') sessions.delete(lookup.id);
    clearCookie(reply, SID_COOKIE);
    // OpenEMR has no token revocation endpoint: the access token is dropped
    // here and simply expires upstream (<= 1 h).
    return reply.code(204).send();
  });

  // ---- allow-listed proxy ---------------------------------------------------------
  async function proxy(request: FastifyRequest, reply: FastifyReply, baseUrl: string, match: (q: URLSearchParams) => MatchResult) {
    const lookup = readSession(request);
    if (lookup.kind !== 'ok') return sessionError(reply, lookup);
    const m = match(new URL(request.url, 'http://bff.invalid').searchParams);
    if (!m.ok) {
      return reply.code(m.status).send(m.status === 400 ? { error: m.error, detail: m.detail } : { error: m.error });
    }
    request.logResource = m.resource;
    const qs = m.query.toString();
    const url = `${baseUrl}/${m.upstreamPath}${qs ? `?${qs}` : ''}`;

    let res: Response;
    try {
      res = await fetch(url, {
        method: 'GET',
        headers: { authorization: `Bearer ${lookup.session.accessToken}`, accept: 'application/fhir+json, application/json' },
        redirect: 'manual',
        signal: AbortSignal.timeout(config.upstreamTimeoutMs),
      });
    } catch (e) {
      const timeout = (e as Error).name === 'TimeoutError';
      request.log.warn({ errName: (e as Error).name }, 'upstream request failed');
      return reply.code(timeout ? 504 : 502).send({ error: timeout ? 'upstream_timeout' : 'upstream_unreachable' });
    }

    const status = res.status;
    if (status === 401) {
      await res.body?.cancel();
      sessions.delete(lookup.id);
      clearCookie(reply, SID_COOKIE);
      return reply.code(401).send({ error: 'session_expired' });
    }
    if (status >= 200 && status < 300) {
      const declared = Number(res.headers.get('content-length') ?? '0');
      const contentType = res.headers.get('content-type');
      if (!isJson(contentType) || declared > MAX_UPSTREAM_BYTES) {
        await res.body?.cancel();
        return reply.code(502).send({ error: 'upstream_invalid_response' });
      }
      const body = Buffer.from(await res.arrayBuffer());
      if (body.length > MAX_UPSTREAM_BYTES) return reply.code(502).send({ error: 'upstream_invalid_response' });
      return reply.code(status).type(contentType ?? 'application/json').send(body);
    }
    await res.body?.cancel();
    if (status === 500 && m.kind === 'read') {
      // OpenEMR answers a read of a patient outside the token's reach with
      // 500 "patient id invalid" instead of 403/404 (Phase B T4).
      request.log.warn({ upstreamStatus: status }, 'upstream 500 on read mapped to 403');
      return reply.code(403).send({ error: 'not_accessible' });
    }
    if (status >= 500 || (status >= 300 && status < 400)) {
      request.log.warn({ upstreamStatus: status }, 'upstream error');
      return reply.code(502).send({ error: 'upstream_error' });
    }
    const errorByStatus: Record<number, string> = { 400: 'bad_request', 403: 'forbidden', 404: 'not_found' };
    return reply.code(status).send({ error: errorByStatus[status] ?? 'upstream_rejected' });
  }

  app.get('/api/fhir/*', async (request, reply) => {
    const path = (request.url.split('?', 1)[0] ?? '').slice('/api/fhir/'.length);
    return proxy(request, reply, config.fhirBaseUrl, (q) => matchFhirRequest(path, q));
  });

  app.get('/api/patient/:pid/medication', async (request, reply) => {
    const { pid } = request.params as { pid: string };
    return proxy(request, reply, config.stdApiBaseUrl, (q) => matchStdMedicationRequest(pid, q));
  });

  // ---- web app ------------------------------------------------------------------
  const dist = config.webDistDir && existsSync(config.webDistDir) ? config.webDistDir : undefined;
  if (dist) {
    await app.register(fastifyStatic, {
      root: dist,
      wildcard: false,
      index: false,
      setHeaders: (res, filePath) => {
        res.header('cache-control', /[\\/]assets[\\/]/.test(filePath) ? 'public, max-age=31536000, immutable' : 'no-cache');
      },
    });
  }

  app.setNotFoundHandler(async (request, reply) => {
    const path = request.url.split('?', 1)[0] ?? '';
    const isAppRoute = request.method === 'GET' && !path.startsWith('/api/') && !path.startsWith('/auth/') && !path.startsWith('/apis/');
    if (dist && isAppRoute) {
      // SPA fallback: client-side routes get index.html (built files are
      // registered as their own routes by @fastify/static at startup).
      reply.header('cache-control', 'no-cache');
      return reply.sendFile('index.html');
    }
    return reply.code(404).send({ error: 'not_found' });
  });

  return app;
}
