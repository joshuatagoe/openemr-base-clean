import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useAuth } from '../auth/authContext';
import { RecentPatientsContext, type RecentPatientsValue } from './recentContext';
import { loadRecent, recentStorageKey, saveRecent, withRecent } from './recentPatients';

interface RecentState {
  /** The user the list belongs to (undefined when signed out). */
  owner: string | undefined;
  /** Storage key (hash of the user id), or null: in memory only. */
  key: string | null;
  ids: string[];
}

/**
 * Mode A's recent-patients list, per signed-in user (see recentPatients.ts).
 * Patients opened before the storage key is ready are kept and merged in.
 */
export function RecentPatientsProvider({ children }: { children: ReactNode }) {
  const { state: auth } = useAuth();
  const signedIn = auth.status === 'signedIn';
  // '' = signed in without a user id: the list is kept in memory only.
  const owner = signedIn ? (auth.user.userId ?? '') : undefined;
  const [state, setState] = useState<RecentState>({ owner, key: null, ids: [] });

  // A different user (or sign-out): start from an empty list.
  if (state.owner !== owner) setState({ owner, key: null, ids: [] });

  useEffect(() => {
    if (!owner) return;
    let cancelled = false;
    void recentStorageKey(owner).then((key) => {
      if (cancelled || key === null) return;
      setState((s) => {
        if (s.owner !== owner) return s;
        const stored = loadRecent(key);
        const ids = s.ids.reduceRight((list, id) => withRecent(list, id), stored);
        return { owner, key, ids };
      });
    });
    return () => {
      cancelled = true;
    };
  }, [owner]);

  useEffect(() => {
    if (state.key) saveRecent(state.key, state.ids);
  }, [state.key, state.ids]);

  const add = useCallback((id: string) => setState((s) => (s.ids[0] === id ? s : { ...s, ids: withRecent(s.ids, id) })), []);
  const remove = useCallback((id: string) => setState((s) => (s.ids.includes(id) ? { ...s, ids: s.ids.filter((x) => x !== id) } : s)), []);
  const clear = useCallback(() => setState((s) => ({ ...s, ids: [] })), []);

  const value = useMemo<RecentPatientsValue>(() => ({ ids: state.ids, add, remove, clear }), [state.ids, add, remove, clear]);
  return <RecentPatientsContext.Provider value={value}>{children}</RecentPatientsContext.Provider>;
}
