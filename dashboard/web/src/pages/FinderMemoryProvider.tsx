import { useMemo, type ReactNode } from 'react';
import { useAuth } from '../auth/authContext';
import { EMPTY_SENSITIVE, FinderMemoryContext, type FinderMemoryValue, type FinderSensitive } from './finderMemory';

/** A fresh in-memory store: nothing persists beyond this object. */
function createFinderMemory(): FinderMemoryValue {
  let saved: { urlFilters: string; values: FinderSensitive } = { urlFilters: '', values: EMPTY_SENSITIVE };
  return {
    read: (urlFilters) => (saved.urlFilters === urlFilters ? saved.values : EMPTY_SENSITIVE),
    write: (urlFilters, values) => {
      saved = { urlFilters, values };
    },
  };
}

/**
 * In-memory home of the Finder's SSN / Home Phone / Search values (see
 * finderMemory.ts). A new, empty store for each signed-in user: sign-out or
 * another user forgets everything.
 */
export function FinderMemoryProvider({ children }: { children: ReactNode }) {
  const { state: auth } = useAuth();
  const owner = auth.status === 'signedIn' ? (auth.user.userId ?? '') : undefined;

  // A new store whenever the user changes (or signs out): `owner` is the reason to rebuild it.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const value = useMemo(() => createFinderMemory(), [owner]);
  return <FinderMemoryContext.Provider value={value}>{children}</FinderMemoryContext.Provider>;
}
