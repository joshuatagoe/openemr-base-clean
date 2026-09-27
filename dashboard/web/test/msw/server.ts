import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';

// jsdom's origin is http://localhost:3000 by default; relative URLs resolve there.
export const signedIn = http.get('*/auth/me', () =>
  HttpResponse.json({ authenticated: true, user: { displayName: 'Dana Testdoctor', fhirUser: null }, expiresAt: '2030-01-01T00:00:00.000Z' }),
);
export const signedOut = http.get('*/auth/me', () => HttpResponse.json({ error: 'unauthenticated' }, { status: 401 }));
export const expired = http.get('*/auth/me', () => HttpResponse.json({ error: 'session_expired' }, { status: 401 }));

const emptyBundle = () => HttpResponse.json({ resourceType: 'Bundle', type: 'collection', total: 0, entry: [] });

/** Clinical-card routes answering "nothing recorded", so pages that render the cards stay quiet by default. */
export const emptyClinical = [
  http.get('*/api/fhir/AllergyIntolerance', emptyBundle),
  http.get('*/api/fhir/Condition', emptyBundle),
  http.get('*/api/fhir/MedicationRequest', emptyBundle),
  http.get('*/api/fhir/CareTeam', emptyBundle),
  http.get('*/api/fhir/Observation', emptyBundle),
  http.get('*/api/patient/:pid/medication', () => new HttpResponse(null, { status: 404 })),
  http.get('*/api/patient/:puuid', ({ params }) => HttpResponse.json({ pid: '42', uuid: String(params.puuid) })),
];

export const server = setupServer(signedOut, ...emptyClinical);
