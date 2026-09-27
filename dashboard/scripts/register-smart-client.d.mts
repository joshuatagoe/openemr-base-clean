// Types for the registration script, so the web tests can check its payload.
export declare const MODULE_PUBLIC: string;
export declare const SCOPES: string[];
export declare function dashboardUrl(origin: string, webroot?: string): string;
export declare function registrationPayload(input: { origin: string; webroot?: string; contact?: string; name?: string }): {
  application_type: 'public';
  client_name: string;
  redirect_uris: string[];
  initiate_login_uri: string;
  contacts: string[];
  scope: string;
};
