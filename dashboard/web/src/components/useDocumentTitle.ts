import { useEffect } from 'react';

// Tab titles name the page, never the patient: tab strips and browser history
// are visible to anyone at the screen (PHI). Plan L4.
export const CHART_TITLE = 'Chart – Patient Dashboard';
export const FIND_PATIENT_TITLE = 'Find a patient – Patient Dashboard';
export const SIGN_IN_TITLE = 'Sign in – Patient Dashboard';

/** Sets the tab title while the page is shown, and restores the previous one after. */
export function useDocumentTitle(title: string): void {
  useEffect(() => {
    const previous = document.title;
    document.title = title;
    return () => {
      document.title = previous;
    };
  }, [title]);
}
