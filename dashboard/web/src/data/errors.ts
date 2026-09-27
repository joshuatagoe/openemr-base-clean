/** Why a data call failed, in terms the UI can act on. */
export type DataErrorKind =
  | 'unauthenticated'
  | 'session_expired'
  | 'forbidden'
  | 'not_accessible'
  | 'not_found'
  | 'bad_request'
  | 'timeout'
  | 'upstream'
  | 'network'
  | 'not_implemented';

export class DataSourceError extends Error {
  readonly kind: DataErrorKind;
  readonly status: number | undefined;

  constructor(kind: DataErrorKind, message: string, status?: number) {
    super(message);
    this.name = 'DataSourceError';
    this.kind = kind;
    this.status = status;
  }
}

// User-facing messages. Each one says what happened and what to do next, in
// the interface's voice (DASHBOARD_UX_PLAN.md §6, M8). A `null` message means
// the auth layer already says what happened (the session ended).

const ADMIN = 'tell your OpenEMR administrator';

/** Failures worth a "Try again" button: the same request may work a moment later. */
export function isRetryable(error: DataSourceError): boolean {
  return error.kind === 'upstream' || error.kind === 'timeout' || error.kind === 'network';
}

function endsSession(error: DataSourceError): boolean {
  return error.kind === 'session_expired' || error.kind === 'unauthenticated';
}

/** Why a load failed and what to do, as the tail of "Couldn't load X: …". */
function loadFailure(kind: DataErrorKind): string {
  switch (kind) {
    case 'network':
      return "the server couldn't be reached. Check your connection, then try again.";
    case 'timeout':
      return 'OpenEMR took too long to answer. Try again.';
    case 'upstream':
      return `OpenEMR returned an error. Try again; if it keeps happening, ${ADMIN}.`;
    case 'bad_request':
      return `OpenEMR didn't accept the request. Reload the page; if it keeps happening, ${ADMIN}.`;
    case 'not_found':
      return `OpenEMR couldn't find the record. Reload the page; if it keeps happening, ${ADMIN}.`;
    default:
      return `the request failed unexpectedly. Reload the page; if it keeps happening, ${ADMIN}.`;
  }
}

/** A card's error line. `noun` is lower case: "allergies", "the care team". */
export function cardErrorMessage(error: DataSourceError, noun: string): string | null {
  if (endsSession(error)) return null;
  if (error.kind === 'forbidden' || error.kind === 'not_accessible') {
    return `Your OpenEMR role can't view ${noun}. If you need it, ask your OpenEMR administrator for access.`;
  }
  return `Couldn't load ${noun}: ${loadFailure(error.kind)}`;
}

/** The patient page (mode A) and the launched patient (modes B/C). */
export function patientErrorMessage(error: DataSourceError): string | null {
  if (endsSession(error)) return null;
  switch (error.kind) {
    case 'forbidden':
    case 'not_accessible':
      return "Your OpenEMR account doesn't have access to this patient's chart.";
    case 'not_found':
      return 'No patient matches this link. It may have been removed, or the link is incomplete.';
    default:
      return `Couldn't load this patient: ${loadFailure(error.kind)}`;
  }
}

/**
 * The patient list and search (mode A landing page). `filtered: false` is the
 * whole list, which the user didn't ask for as a search.
 */
export function searchErrorMessage(error: DataSourceError, { filtered = true }: { filtered?: boolean } = {}): string | null {
  if (endsSession(error)) return null;
  switch (error.kind) {
    case 'network':
      return filtered ? "Couldn't reach OpenEMR, so the search didn't run. Try again." : "Couldn't reach OpenEMR, so the patient list didn't load. Try again.";
    case 'timeout':
      return 'OpenEMR took too long to answer. Try again.';
    case 'bad_request':
      return "OpenEMR didn't accept these search terms. Check the name, phone number, SSN, date of birth and External ID.";
    case 'forbidden':
    case 'not_accessible':
      return "Your OpenEMR role can't search patients. If you need to, ask your OpenEMR administrator for access.";
    default:
      return `OpenEMR returned an error. Try again; if it keeps happening, ${ADMIN}.`;
  }
}
