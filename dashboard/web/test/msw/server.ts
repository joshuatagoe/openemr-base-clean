import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';

// jsdom's origin is http://localhost:3000 by default; relative URLs resolve there.
export const signedIn = http.get('*/auth/me', () =>
  HttpResponse.json({ authenticated: true, user: { displayName: 'Dana Testdoctor', fhirUser: null }, expiresAt: '2030-01-01T00:00:00.000Z' }),
);
export const signedOut = http.get('*/auth/me', () => HttpResponse.json({ error: 'unauthenticated' }, { status: 401 }));
export const expired = http.get('*/auth/me', () => HttpResponse.json({ error: 'session_expired' }, { status: 401 }));

export const server = setupServer(signedOut);
