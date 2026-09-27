#!/usr/bin/env node
// Registers the patient dashboard's SMART client (modes B and C) with OpenEMR
// and writes the module's dashboard.config.json. One-time admin step.
//
//   NODE_EXTRA_CA_CERTS=/path/openemr-dev.pem node scripts/register-smart-client.mjs \
//     --openemr https://localhost:9300 [--webroot /openemr] [--contact admin@example.org] \
//     [--write-config ../interface/modules/custom_modules/oe-module-copilot/public/dashboard.config.json]
//   node scripts/register-smart-client.mjs --dry-run      (print the request body only)
//
// The client is public (no secret) and its launch URI and redirect URI are the
// same-origin app URL. OpenEMR creates clients with the `launch` scope
// DISABLED: enable it in Administration > System > API Clients > Edit >
// "Enable Client" (optionally "Enable Authorization Flow Skip", so a clinician
// who is signed in to OpenEMR is not asked to sign in again). Only the
// client_id is printed; the registration_access_token is never printed or kept.
import { writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import scopes from '../web/src/smart/scopes.json' with { type: 'json' };

export const MODULE_PUBLIC = '/interface/modules/custom_modules/oe-module-copilot/public';
/** The app's own scope list (shared file), so registration and requests always agree. */
export const SCOPES = scopes;

/** The app URL: both the SMART launch URI and the OAuth redirect URI (one page handles both). */
export function dashboardUrl(origin, webroot = '') {
  const o = new URL(origin);
  if (o.protocol !== 'https:' && o.protocol !== 'http:') throw new Error('--openemr must be an http(s) origin');
  const root = webroot.replace(/\/+$/, '');
  if (root !== '' && !/^(\/[A-Za-z0-9._~-]+)+$/.test(root)) throw new Error('--webroot must look like /path');
  return `${o.origin}${root}${MODULE_PUBLIC}/dashboard/`;
}

export function registrationPayload({ origin, webroot = '', contact = 'admin@example.org', name = 'Patient Dashboard (React, SMART)' }) {
  const url = dashboardUrl(origin, webroot);
  return {
    application_type: 'public',
    client_name: name,
    redirect_uris: [url],
    initiate_login_uri: url,
    contacts: [contact],
    scope: SCOPES.join(' '),
  };
}

function parseArgs(argv) {
  const out = { openemr: 'https://localhost:9300', webroot: '', contact: 'admin@example.org', writeConfig: undefined, dryRun: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--dry-run') out.dryRun = true;
    else if (a === '--openemr') out.openemr = argv[++i];
    else if (a === '--webroot') out.webroot = argv[++i] ?? '';
    else if (a === '--contact') out.contact = argv[++i];
    else if (a === '--write-config') out.writeConfig = argv[++i];
    else throw new Error(`Unknown argument: ${a}`);
  }
  return out;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const payload = registrationPayload({ origin: args.openemr, webroot: args.webroot, contact: args.contact });
  if (args.dryRun) {
    console.log(JSON.stringify(payload, null, 2));
    return;
  }
  const origin = new URL(args.openemr).origin;
  const res = await fetch(`${origin}${args.webroot.replace(/\/+$/, '')}/oauth2/default/registration`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json' },
    body: JSON.stringify(payload),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok || typeof body.client_id !== 'string') {
    // OpenEMR's error fields are fixed codes and short texts, no secrets.
    console.error(`Registration failed: HTTP ${res.status} ${body.error ?? ''} ${body.error_description ?? ''}`.trim());
    process.exitCode = 1;
    return;
  }
  console.log(`Registered "${payload.client_name}"`);
  console.log(`client_id: ${body.client_id}`);
  console.log(`launch URI = redirect URI: ${payload.initiate_login_uri}`);
  if (args.writeConfig) {
    writeFileSync(args.writeConfig, `${JSON.stringify({ clientId: body.client_id })}\n`);
    console.log(`Wrote ${args.writeConfig}`);
  }
  console.log('Next: Administration > System > API Clients > Edit > Enable Client (the client starts disabled because it has the launch scope).');
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((e) => {
    console.error(e instanceof Error ? e.message : String(e));
    process.exitCode = 1;
  });
}
