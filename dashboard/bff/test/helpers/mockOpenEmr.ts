// A stand-in for OpenEMR's OAuth token endpoint and FHIR / standard API,
// listening on a random loopback port. It records what the BFF sends so tests
// can assert on it (secret present, cookies stripped, bearer added, params).
// All data is synthetic.
import { createHash, randomBytes } from 'node:crypto';
import Fastify, { type FastifyInstance } from 'fastify';

export const CLIENT_ID = 'test-client-id';
export const CLIENT_SECRET = 'test-client-secret-value-0123456789';
export const OTHER_PATIENT_ID = 'other-patient-0001';
export const STD_PATIENT_UUID = 'a2c3ab57-cdd6-4aad-afc9-e19c171e7ed7';
export const STD_PATIENT_NO_PID_UUID = '00000000-0000-4000-8000-000000000001';
export const STD_PATIENT_FORBIDDEN_UUID = '00000000-0000-4000-8000-000000000403';

export interface RecordedRequest {
  method: string;
  path: string;
  query: string;
  headers: Record<string, string | string[] | undefined>;
  body?: Record<string, string>;
}

interface PendingCode {
  challenge: string;
  nonce: string;
  redirectUri: string;
  claims: Record<string, unknown>;
}

export interface MockOpenEmr {
  baseUrl: string;
  issuer: string;
  requests: RecordedRequest[];
  issuedAccessTokens: string[];
  issuedIdTokens: string[];
  /** What the authorization server would hand back after the user logs in. */
  issueCode(input: { challenge: string; nonce: string; redirectUri: string; claims?: Record<string, unknown> }): string;
  /** Overrides the next token response (status + JSON body). */
  nextTokenResponse?: { status: number; body: unknown };
  tokenExpiresIn: number;
  close(): Promise<void>;
}

function b64url(input: Buffer | string): string {
  return Buffer.from(input).toString('base64url');
}

function fakeJwt(claims: Record<string, unknown>): string {
  // Unsigned-looking JWT: header.payload.signature. The BFF reads claims only
  // (the id_token comes straight from the token endpoint over TLS).
  return [b64url(JSON.stringify({ alg: 'RS256', typ: 'JWT' })), b64url(JSON.stringify(claims)), b64url(randomBytes(16))].join('.');
}

export async function startMockOpenEmr(): Promise<MockOpenEmr> {
  const app: FastifyInstance = Fastify({ logger: false });
  const codes = new Map<string, PendingCode>();
  const mock = {
    baseUrl: '',
    issuer: '',
    requests: [] as RecordedRequest[],
    issuedAccessTokens: [] as string[],
    issuedIdTokens: [] as string[],
    tokenExpiresIn: 3600,
    nextTokenResponse: undefined as { status: number; body: unknown } | undefined,
    issueCode(input: { challenge: string; nonce: string; redirectUri: string; claims?: Record<string, unknown> }): string {
      const code = 'code-' + randomBytes(12).toString('hex');
      codes.set(code, { challenge: input.challenge, nonce: input.nonce, redirectUri: input.redirectUri, claims: input.claims ?? {} });
      return code;
    },
    close: () => app.close(),
  };

  const record = (req: { method: string; url: string; headers: RecordedRequest['headers'] }, body?: Record<string, string>) => {
    const [path, query = ''] = req.url.split('?', 2) as [string, string?];
    const entry: RecordedRequest = { method: req.method, path, query, headers: { ...req.headers } };
    if (body) entry.body = body;
    mock.requests.push(entry);
  };

  app.addContentTypeParser('application/x-www-form-urlencoded', { parseAs: 'string' }, (_req, body, done) => {
    done(null, Object.fromEntries(new URLSearchParams(body as string)));
  });

  app.post('/oauth2/default/token', async (req, reply) => {
    const body = req.body as Record<string, string>;
    record(req, body);
    if (mock.nextTokenResponse) {
      const r = mock.nextTokenResponse;
      mock.nextTokenResponse = undefined;
      return reply.code(r.status).send(r.body);
    }
    if (body.client_id !== CLIENT_ID || (body.client_secret !== undefined && body.client_secret !== CLIENT_SECRET)) {
      return reply.code(401).send({ error: 'invalid_client' });
    }
    const pending = body.code ? codes.get(body.code) : undefined;
    if (!pending || body.grant_type !== 'authorization_code') {
      return reply.code(400).send({ error: 'invalid_grant' });
    }
    codes.delete(body.code as string);
    const challenge = createHash('sha256').update(body.code_verifier ?? '').digest('base64url');
    if (challenge !== pending.challenge || body.redirect_uri !== pending.redirectUri) {
      return reply.code(400).send({ error: 'invalid_grant' });
    }
    const now = Math.floor(Date.now() / 1000);
    const accessToken = 'at-' + randomBytes(24).toString('hex');
    const idToken = fakeJwt({
      iss: mock.issuer,
      aud: CLIENT_ID,
      sub: 'user-uuid-1',
      nonce: pending.nonce,
      iat: now,
      exp: now + 3600,
      fhirUser: `${mock.baseUrl}/apis/default/fhir/Practitioner/prac-1`,
      name: 'Dana Testdoctor',
      ...pending.claims,
    });
    mock.issuedAccessTokens.push(accessToken);
    mock.issuedIdTokens.push(idToken);
    return reply
      .header('cache-control', 'no-store')
      .send({
        access_token: accessToken,
        token_type: 'Bearer',
        expires_in: mock.tokenExpiresIn,
        id_token: idToken,
        scope: 'openid fhirUser user/Patient.rs nonce',
      });
  });

  const authorized = (auth: string | undefined) => auth !== undefined && mock.issuedAccessTokens.some((t) => auth === `Bearer ${t}`);

  app.get('/apis/default/fhir/*', async (req, reply) => {
    record(req);
    if (!authorized(req.headers.authorization)) return reply.code(401).send({ message: 'Unauthorized' });
    const path = (req.params as { '*': string })['*'];
    if (path === `Patient/${OTHER_PATIENT_ID}`) return reply.code(500).send({ message: 'patient id invalid' });
    if (path === 'Patient/revoked') return reply.code(401).send({ message: 'Unauthorized' });
    if (path === 'Patient/slow') {
      await new Promise((r) => setTimeout(r, 1500));
      return reply.send({ resourceType: 'Patient', id: 'slow' });
    }
    if (path === 'Patient/boom') return reply.code(503).send({ message: 'internal detail that must not leak' });
    if (path === 'Practitioner/prac-named') {
      return reply.type('application/fhir+json').send({ resourceType: 'Practitioner', id: 'prac-named', name: [{ use: 'official', given: ['Dana'], family: 'Named' }] });
    }
    if (path.startsWith('Practitioner/')) return reply.code(403).send({ message: 'Organization policy does not have permit access resource' });
    if (path.startsWith('Patient/')) {
      return reply.type('application/fhir+json').send({ resourceType: 'Patient', id: path.slice('Patient/'.length) });
    }
    return reply.type('application/fhir+json').send({ resourceType: 'Bundle', type: 'searchset', total: 0, entry: [] });
  });

  // Standard API patient read. OpenEMR answers the whole patient_data row
  // (including `ss`) inside the standard envelope; values here are synthetic.
  app.get('/apis/default/api/patient/:puuid', async (req, reply) => {
    record(req);
    if (!authorized(req.headers.authorization)) return reply.code(401).send({ message: 'Unauthorized' });
    const { puuid } = req.params as { puuid: string };
    if (puuid === STD_PATIENT_UUID) {
      return reply.type('application/json').send({
        validationErrors: [],
        internalErrors: [],
        data: { id: '1', uuid: STD_PATIENT_UUID, pid: '7', pubpid: 'MRN-SYN-7', fname: 'Synthetica', lname: 'Testpatient', ss: '999-00-1234', DOB: '1970-01-01' },
      });
    }
    if (puuid === STD_PATIENT_NO_PID_UUID) {
      return reply.type('application/json').send({ validationErrors: [], internalErrors: [], data: { uuid: STD_PATIENT_NO_PID_UUID } });
    }
    if (puuid === STD_PATIENT_FORBIDDEN_UUID) return reply.code(403).send({ message: 'forbidden' });
    return reply.code(404).type('application/json').send({ validationErrors: [], internalErrors: [], data: [] });
  });

  app.get('/apis/default/api/patient/:pid/medication', async (req, reply) => {
    record(req);
    if (!authorized(req.headers.authorization)) return reply.code(401).send({ message: 'Unauthorized' });
    const { pid } = req.params as { pid: string };
    if (pid === '7') return reply.code(404).send('');
    // ListRestController::getAll answers a bare JSON array (verified on the dev stack).
    return reply.type('application/json').send([{ uuid: 'med-uuid-1', title: 'Synthetic med' }]);
  });

  await app.listen({ port: 0, host: '127.0.0.1' });
  const addr = app.server.address();
  if (!addr || typeof addr === 'string') throw new Error('mock did not bind');
  mock.baseUrl = `http://127.0.0.1:${addr.port}`;
  mock.issuer = `${mock.baseUrl}/oauth2/default`;
  return mock;
}
