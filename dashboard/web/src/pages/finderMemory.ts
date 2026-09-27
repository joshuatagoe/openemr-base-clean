import { createContext, useContext } from 'react';

/**
 * The Finder values that must never reach the URL (browser history, the
 * address bar, Referer): the SSN and Home Phone filters and the global search
 * text (which can be an SSN or a phone number). FinderMemoryProvider keeps them
 * in memory only, for this tab, so Back from a chart returns to the same
 * results. They are never written to storage and are dropped on sign-out or a
 * change of user. `urlFilters` ties them to the name / DOB / External ID
 * filters that were in the URL when they were set: a different URL starts with
 * them empty.
 */
export interface FinderSensitive {
  ssn: string;
  phone: string;
  search: string;
}

export const EMPTY_SENSITIVE: FinderSensitive = { ssn: '', phone: '', search: '' };

export interface FinderMemoryValue {
  read(urlFilters: string): FinderSensitive;
  write(urlFilters: string, values: FinderSensitive): void;
}

export const FinderMemoryContext = createContext<FinderMemoryValue | null>(null);

const FORGETFUL: FinderMemoryValue = { read: () => EMPTY_SENSITIVE, write: () => undefined };

/** Outside the provider nothing is remembered. */
export function useFinderMemory(): FinderMemoryValue {
  return useContext(FinderMemoryContext) ?? FORGETFUL;
}
