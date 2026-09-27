import { createContext, useContext } from 'react';

export interface RecentPatientsValue {
  /** FHIR Patient ids, most recent first (at most 10). */
  ids: readonly string[];
  /** A patient was opened. */
  add(id: string): void;
  /** Drop one (not found or no longer accessible). */
  remove(id: string): void;
  clear(): void;
}

export const RecentPatientsContext = createContext<RecentPatientsValue | null>(null);

/** null outside the provider (modes B/C have no recent list). */
export function useRecentPatients(): RecentPatientsValue | null {
  return useContext(RecentPatientsContext);
}
