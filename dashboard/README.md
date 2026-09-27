# Patient dashboard (React port)

A React + TypeScript port of OpenEMR's patient summary dashboard, reading OpenEMR only through its OAuth2/OIDC-protected FHIR and standard REST APIs. No OpenEMR PHP or SQL is changed.

| Folder | What |
|---|---|
| `web/` | Vite + React + TypeScript SPA. Data access goes through a `DataSource` interface with two transports: `BffDataSource` (mode A, used now) and `SmartDataSource` (modes B/C, stub until C5). |
| `bff/` | Fastify + TypeScript backend-for-frontend: serves the built SPA, runs the OAuth login as a **confidential client**, keeps tokens server-side, and proxies an allow-list of read-only GET routes. |

Why a BFF: on this OpenEMR build a public (browser) client cannot be granted clinician `user/` scopes, and every CORS preflight to `/apis/*` returns 404, so a browser on another origin cannot call the API at all. Both were measured on the dev stack.

## Scripts (run in `dashboard/`)

| Command | Does |
|---|---|
| `npm ci` | Install both workspaces |
| `npm run dev` | BFF on :3000 (reads `dashboard/.env`) + Vite on :5173. Vite proxies `/auth`, `/api` and `/healthz` to the BFF, so the browser sees one origin. Open http://localhost:5173 |
| `npm run build` | `web/dist` (static app) and `bff/dist` (server) |
| `npm start` | Run the built BFF; it also serves `web/dist` |
| `npm test` | Vitest: BFF (mock OpenEMR on a loopback port) and web (React Testing Library + MSW) |
| `npm run lint` / `npm run typecheck` | ESLint / `tsc --noEmit` for both packages |

## Configuration (BFF environment variables)

Copy `.env.example` to `.env` (git-ignored). Required:

| Variable | Meaning |
|---|---|
| `OPENEMR_BASE_URL` | OpenEMR origin, e.g. `https://localhost:9300`. The FHIR base is `<base>/apis/default/fhir` |
| `OAUTH_ISSUER` | `site_addr_oath` + `/oauth2/default`, e.g. `https://localhost:9300/oauth2/default`. Must equal the id_token `iss` |
| `CLIENT_ID`, `CLIENT_SECRET` | The confidential client registered below. The secret stays server-side and is **always** sent at the token step |
| `REDIRECT_URI` | Exactly as registered, e.g. `http://localhost:5173/auth/callback`. Its path becomes the callback route |
| `SESSION_SECRET` | 32+ random characters; signs the session cookie |
| `COOKIE_SECURE` | `true` (default: `Secure`, `__Host-` cookies, HSTS). `false` only for plain-http local dev |

Optional: `PORT` (3000), `HOST` (127.0.0.1), `OPENEMR_SITE` (default), `UPSTREAM_TIMEOUT_MS` (10000), `LOG_LEVEL` (info), `WEB_DIST_DIR`, `OAUTH_SCOPES`.

The dev stack uses a self-signed certificate. Trust it rather than switching TLS checks off: export it once (`openssl s_client -connect localhost:9300 -showcerts </dev/null | openssl x509 > openemr-dev.pem`, keep it outside the repo) and start the BFF with `NODE_EXTRA_CA_CERTS=/path/to/openemr-dev.pem` set in the shell.

## Register the OAuth client (one-time admin step)

1. Globals (*Administration → Config → Connectors*): FHIR REST API and standard REST API on, and **Site Address Override** (`site_addr_oath`) set to the public https origin. The dev-easy stack already has these (`https://localhost:9300`).
2. Register a **confidential** client with `curl` (browser registration fails the CORS preflight):

   ```sh
   curl -k -H 'Content-Type: application/json' https://localhost:9300/oauth2/default/registration -d '{
     "application_type": "private",
     "token_endpoint_auth_method": "client_secret_post",
     "client_name": "Patient Dashboard BFF (dev)",
     "redirect_uris": ["http://localhost:5173/auth/callback"],
     "post_logout_redirect_uris": ["http://localhost:5173/"],
     "contacts": ["dev@example.com"],
     "scope": "openid fhirUser api:oemr api:fhir user/Patient.rs user/AllergyIntolerance.rs user/Condition.rs user/MedicationRequest.rs user/CareTeam.rs user/Observation.rs user/Practitioner.rs user/Organization.rs user/medication.rs user/patient.rs"
   }'
   ```

   Keep `client_id` and `client_secret` from the response in your `.env` or secret store, never in Git. `application_type` must be `private`: OpenEMR refuses `user/` scopes for public clients.
3. The client is created **disabled**. Enable it in *Administration → System → API Clients → Edit → Enable Client*.
4. Sign in with a clinician account (the dev stack's test physician is `drdash`, in the Physicians group). The consent page follows the OpenEMR login.

Scopes are read-only, with no `offline_access`, so no refresh token is issued. A session lasts as long as the access token (1 h); after that the API answers 401 and the app asks the user to sign in again.

## BFF behaviour

- **Login**: `GET /auth/login` redirects to OpenEMR with authorization code + PKCE (S256), `state`, `nonce` and `aud`. The login is bound to the browser by a signed, 10-minute cookie. The callback checks `state` (single use), exchanges the code with the client secret and PKCE verifier, and checks the id_token `iss`, `aud`, `exp` and `nonce`.
- **Session**: an in-memory store keyed by a random id, sent as an HttpOnly, SameSite=Lax, signed cookie (`__Host-dash_sid` when `COOKIE_SECURE=true`). `GET /auth/me` returns the display name only. `POST /auth/logout` deletes the session. OpenEMR has no revocation endpoint, so the token is dropped and expires upstream. Restarting the BFF signs everyone out.
- **Display name**: OpenEMR's id_token has no `name` claim (it drops `profile`), so the BFF reads the user's own `fhirUser` Practitioner with the user's token. The Physicians group gets 403 there, so they see "OpenEMR user".
- **Proxy** (GET only; anything else is 404; unknown or repeated query parameters are 400):

  | BFF route | OpenEMR | Parameters |
  |---|---|---|
  | `/api/fhir/Patient/:id` | FHIR read | none |
  | `/api/fhir/Patient` | FHIR search | `name`, `birthdate`, `identifier` (at least one) |
  | `/api/fhir/AllergyIntolerance`, `MedicationRequest`, `CareTeam` | FHIR search | `patient` (required) |
  | `/api/fhir/Condition` | FHIR search | `patient` (required), `category` ∈ problem-list-item, encounter-diagnosis, health-concern |
  | `/api/fhir/Observation` | FHIR search | `patient` (required), `category=laboratory` (required) |
  | `/api/fhir/Practitioner/:id`, `/api/fhir/Organization/:id` | FHIR read | none |
  | `/api/patient/:pid/medication` | standard API | none (OpenEMR's 404 means "no list medications") |

  Browser cookies and headers are not forwarded; the BFF adds only the bearer token. OpenEMR's 401 ends the session. Its `500` on a read of an inaccessible patient becomes `403 {"error":"not_accessible"}`. Other 5xx become 502, and a timeout becomes 504. API responses are `Cache-Control: no-store`.
- **Headers**: CSP (`default-src 'self'`, `frame-ancestors 'none'`, `object-src 'none'`, `base-uri 'none'`), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, and HSTS when cookies are Secure.
- **Logs**: one JSON line per request with the request id, method, route pattern, FHIR resource type, status and duration. URLs, query strings, headers, cookies, tokens and bodies are never logged. `GET /healthz` returns `{"status":"ok"}`.
