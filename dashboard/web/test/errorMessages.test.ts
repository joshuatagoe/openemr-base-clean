import { describe, expect, it } from 'vitest';
import { cardErrorMessage, DataSourceError, isRetryable, patientErrorMessage, searchErrorMessage, type DataErrorKind } from '../src/data/errors';

const err = (kind: DataErrorKind) => new DataSourceError(kind, 'internal detail');
const ALL: DataErrorKind[] = ['unauthenticated', 'session_expired', 'forbidden', 'not_accessible', 'not_found', 'bad_request', 'timeout', 'upstream', 'network', 'not_implemented'];

describe('user-facing error messages (plan §6, M8)', () => {
  it('cards: one message per kind, naming what failed and what to do', () => {
    expect(cardErrorMessage(err('forbidden'), 'allergies')).toBe("Your OpenEMR role can't view allergies. If you need it, ask your OpenEMR administrator for access.");
    expect(cardErrorMessage(err('not_accessible'), 'lab data')).toMatch(/^Your OpenEMR role can't view lab data\./);
    expect(cardErrorMessage(err('network'), 'allergies')).toBe("Couldn't load allergies: the server couldn't be reached. Check your connection, then try again.");
    expect(cardErrorMessage(err('timeout'), 'the care team')).toBe("Couldn't load the care team: OpenEMR took too long to answer. Try again.");
    expect(cardErrorMessage(err('upstream'), 'lab data')).toBe(
      "Couldn't load lab data: OpenEMR returned an error. Try again; if it keeps happening, tell your OpenEMR administrator.",
    );
    expect(cardErrorMessage(err('bad_request'), 'prescriptions')).toMatch(/^Couldn't load prescriptions: OpenEMR didn't accept the request\./);
  });

  it('the session ending is reported by the auth layer, not by each card', () => {
    for (const kind of ['session_expired', 'unauthenticated'] as const) {
      expect(cardErrorMessage(err(kind), 'allergies')).toBeNull();
      expect(patientErrorMessage(err(kind))).toBeNull();
      expect(searchErrorMessage(err(kind))).toBeNull();
    }
  });

  it('never shows the internal error text, never apologises, uses contractions', () => {
    for (const kind of ALL) {
      for (const text of [cardErrorMessage(err(kind), 'allergies'), patientErrorMessage(err(kind)), searchErrorMessage(err(kind))]) {
        if (text === null) continue;
        expect(text).not.toContain('internal detail');
        expect(text).not.toMatch(/sorry|please|could not|did not|do not|cannot/i);
        expect(text.endsWith('.')).toBe(true);
      }
    }
  });

  it('patient page: access, missing and transient failures', () => {
    expect(patientErrorMessage(err('forbidden'))).toBe("Your OpenEMR account doesn't have access to this patient's chart.");
    expect(patientErrorMessage(err('not_found'))).toBe('No patient matches this link. It may have been removed, or the link is incomplete.');
    expect(patientErrorMessage(err('timeout'))).toBe("Couldn't load this patient: OpenEMR took too long to answer. Try again.");
  });

  it('search: the plan copy for network, timeout, OpenEMR error and refused terms', () => {
    expect(searchErrorMessage(err('network'))).toBe("Couldn't reach OpenEMR, so the search didn't run. Try again.");
    // The unfiltered list is not a search the user ran.
    expect(searchErrorMessage(err('network'), { filtered: false })).toBe("Couldn't reach OpenEMR, so the patient list didn't load. Try again.");
    expect(searchErrorMessage(err('timeout'))).toBe('OpenEMR took too long to answer. Try again.');
    expect(searchErrorMessage(err('upstream'))).toBe('OpenEMR returned an error. Try again; if it keeps happening, tell your OpenEMR administrator.');
    expect(searchErrorMessage(err('bad_request'))).toBe("OpenEMR didn't accept these search terms. Check the name, phone number, SSN, date of birth and External ID.");
  });

  it('only transient failures offer "Try again"', () => {
    expect(ALL.filter((k) => isRetryable(err(k)))).toEqual(['timeout', 'upstream', 'network']);
  });
});
