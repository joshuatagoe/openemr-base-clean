import { describe, expect, it } from 'vitest';
import { dashboardUrl, registrationPayload } from '../../scripts/register-smart-client.mjs';
import { SMART_SCOPES } from '../src/smart/launch';

describe('SMART client registration script', () => {
  it('registers a public client whose launch and redirect URI are the same-origin app, with exactly the scopes the app asks for', () => {
    const p = registrationPayload({ origin: 'https://localhost:9300' });
    const app = 'https://localhost:9300/interface/modules/custom_modules/oe-module-copilot/public/dashboard/';
    expect(p.application_type).toBe('public');
    expect(p.redirect_uris).toEqual([app]);
    expect(p.initiate_login_uri).toBe(app);
    expect(p.scope).toBe(SMART_SCOPES);
    expect(p.scope.split(' ')).toContain('launch');
    expect(p).not.toHaveProperty('client_secret');
    expect(p).not.toHaveProperty('token_endpoint_auth_method');
  });

  it('handles an OpenEMR webroot and refuses odd input', () => {
    expect(dashboardUrl('https://emr.example/', '/openemr/')).toBe('https://emr.example/openemr/interface/modules/custom_modules/oe-module-copilot/public/dashboard/');
    expect(() => dashboardUrl('javascript:alert(1)')).toThrow();
    expect(() => dashboardUrl('https://emr.example', 'no-slash')).toThrow();
  });
});
