// Calls to the BFF's auth endpoints. The browser never sees a token: the
// session is an HttpOnly cookie the BFF sets after the OAuth callback.

export interface SignedInUser {
  displayName: string;
}

export type MeResult =
  | { kind: 'signedIn'; user: SignedInUser; expiresAt: string }
  | { kind: 'signedOut'; reason: 'unauthenticated' | 'session_expired' };

export const LOGIN_PATH = '/auth/login';

export async function fetchMe(signal?: AbortSignal): Promise<MeResult> {
  const res = await fetch(new URL('/auth/me', window.location.origin), {
    credentials: 'same-origin',
    headers: { accept: 'application/json' },
    ...(signal ? { signal } : {}),
  });
  if (res.status === 401) {
    let reason: 'unauthenticated' | 'session_expired' = 'unauthenticated';
    try {
      const body = (await res.json()) as { error?: unknown };
      if (body.error === 'session_expired') reason = 'session_expired';
    } catch {
      // Keep the default.
    }
    return { kind: 'signedOut', reason };
  }
  if (!res.ok) throw new Error(`Session check failed (${res.status})`);
  const body = (await res.json()) as { user?: { displayName?: unknown }; expiresAt?: unknown };
  const displayName = typeof body.user?.displayName === 'string' ? body.user.displayName : 'Signed-in user';
  return { kind: 'signedIn', user: { displayName }, expiresAt: String(body.expiresAt ?? '') };
}

export async function postLogout(): Promise<void> {
  await fetch(new URL('/auth/logout', window.location.origin), { method: 'POST', credentials: 'same-origin' });
}

const AUTH_ERROR_REASONS: Readonly<Record<string, string>> = {
  access_denied: 'access was denied',
  state_mismatch: 'the sign-in link expired or was already used',
  login_expired: 'the sign-in link expired or was already used',
  token_exchange_failed: 'OpenEMR did not accept the sign-in',
  invalid_id_token: 'the identity token could not be verified',
  missing_code: 'OpenEMR did not return an authorization code',
};

/** Maps the BFF's coarse `auth_error` codes to text; unknown codes get generic text (never echoed). */
export function describeAuthError(code: string): string {
  const reason = Object.hasOwn(AUTH_ERROR_REASONS, code) ? AUTH_ERROR_REASONS[code] : undefined;
  return `Sign-in did not complete: ${reason ?? 'an unexpected error occurred'}.`;
}
