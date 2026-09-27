// Recent patients (mode A). OpenEMR keeps its own list server-side
// (`recent_patients`, PatientService::touchRecentPatientList, shown by the
// Patient Finder), but no REST or FHIR route exposes it, and this app is
// read-only. So the list lives in this browser:
// - key: a SHA-256 hash of the signed-in user's id (`fhirUser`), so users of a
//   shared browser keep separate lists and the key does not name the user;
// - value: FHIR Patient ids ONLY (a JSON array), never a name, DOB, MRN or any
//   other patient data. Names are read live through the BFF when shown.
// Every storage access is wrapped: without storage the list is kept in memory.

export const RECENT_MAX = 10;
const KEY_PREFIX = 'dash.recentPatients.v1.';
const FHIR_ID = /^[A-Za-z0-9.-]{1,64}$/;

function hex(buffer: ArrayBuffer): string {
  return [...new Uint8Array(buffer)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

/** The storage key for a user, or null (no user id, or no WebCrypto: keep the list in memory). */
export async function recentStorageKey(userId: string): Promise<string | null> {
  if (!userId) return null;
  try {
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(`openemr-dashboard:recent-patients:${userId}`));
    return KEY_PREFIX + hex(digest);
  } catch {
    return null;
  }
}

function storage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function clean(values: readonly unknown[]): string[] {
  const out: string[] = [];
  for (const v of values) {
    if (typeof v === 'string' && FHIR_ID.test(v) && !out.includes(v)) out.push(v);
    if (out.length === RECENT_MAX) break;
  }
  return out;
}

/** The opened patient first, no duplicates, at most RECENT_MAX. */
export function withRecent(ids: readonly string[], id: string): string[] {
  if (!FHIR_ID.test(id)) return [...ids];
  return clean([id, ...ids]);
}

export function loadRecent(key: string): string[] {
  try {
    const raw = storage()?.getItem(key);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? clean(parsed) : [];
  } catch {
    return [];
  }
}

export function saveRecent(key: string, ids: readonly string[]): void {
  try {
    const s = storage();
    if (!s) return;
    const list = clean(ids);
    if (list.length === 0) s.removeItem(key);
    else s.setItem(key, JSON.stringify(list));
  } catch {
    // Storage full, blocked or unavailable: the in-memory list still works.
  }
}
