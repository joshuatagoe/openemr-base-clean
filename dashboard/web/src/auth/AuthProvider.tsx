import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { describeAuthError, fetchMe, postLogout } from './authApi';
import { AuthContext, type AuthContextValue, type AuthState, type Notice } from './authContext';

/** Reads and removes `?auth_error=` (set by the BFF after a failed callback). */
function takeAuthErrorFromUrl(): Notice | undefined {
  const url = new URL(window.location.href);
  const code = url.searchParams.get('auth_error');
  if (code === null) return undefined;
  url.searchParams.delete('auth_error');
  window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`);
  return { kind: 'auth_error', message: describeAuthError(code) };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: 'loading' });
  const [notice, setNotice] = useState<Notice | undefined>(takeAuthErrorFromUrl);

  useEffect(() => {
    const ctrl = new AbortController();
    fetchMe(ctrl.signal)
      .then((me) => {
        if (me.kind === 'signedIn') {
          setState({ status: 'signedIn', user: me.user, expiresAt: me.expiresAt });
        } else {
          setState({ status: 'signedOut' });
          if (me.reason === 'session_expired') setNotice({ kind: 'session_expired' });
        }
      })
      .catch(() => {
        if (ctrl.signal.aborted) return;
        setState({ status: 'signedOut' });
        setNotice({ kind: 'error', message: 'The dashboard server could not be reached.' });
      });
    return () => ctrl.abort();
  }, []);

  const signOut = useCallback(async () => {
    try {
      await postLogout();
    } finally {
      setState({ status: 'signedOut' });
      setNotice({ kind: 'signed_out' });
    }
  }, []);

  const markSessionExpired = useCallback(() => {
    setState({ status: 'signedOut' });
    setNotice({ kind: 'session_expired' });
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ state, notice, signOut, markSessionExpired }),
    [state, notice, signOut, markSessionExpired],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
